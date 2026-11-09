"""Base SQLite dédiée au moteur perps papier (séparée de reward_hunter.db)."""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from app.perps.models import PairState

SCHEMA = """
CREATE TABLE IF NOT EXISTS perps_observations (
    observed_at REAL NOT NULL,
    symbol TEXT NOT NULL,
    price REAL NOT NULL,
    spread_pct REAL,
    open_fee_pct REAL,
    close_fee_pct REAL,
    funding_long_pct_h REAL,
    funding_short_pct_h REAL,
    rollover_pct_h REAL,
    oi_long_usd REAL,
    oi_short_usd REAL,
    market_open INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (symbol, observed_at)
);

CREATE TABLE IF NOT EXISTS perps_paper_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    kind TEXT NOT NULL,
    opened_at REAL NOT NULL,
    entry_price REAL NOT NULL,
    index_price_at_open REAL NOT NULL,
    notional_usd REAL NOT NULL,
    leverage REAL NOT NULL,
    collateral_usd REAL NOT NULL,
    open_fee_usd REAL NOT NULL DEFAULT 0,
    spot_qty REAL,
    spot_entry_price REAL,
    spot_costs_usd REAL NOT NULL DEFAULT 0,
    funding_usd REAL NOT NULL DEFAULT 0,
    rollover_usd REAL NOT NULL DEFAULT 0,
    last_accrual_at REAL NOT NULL,
    liquidation_price REAL,
    entry_reason TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    closed_at REAL,
    exit_price REAL,
    close_fee_usd REAL,
    exit_reason TEXT,
    gross_pnl_usd REAL,
    net_pnl_usd REAL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_perps_one_open_position
ON perps_paper_positions(strategy, symbol) WHERE status = 'open';

CREATE INDEX IF NOT EXISTS idx_perps_positions_closed
ON perps_paper_positions(strategy, closed_at DESC);

CREATE TABLE IF NOT EXISTS perps_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    level TEXT NOT NULL DEFAULT 'info',
    strategy TEXT,
    symbol TEXT,
    message TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS perps_state (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS perps_funding_scan (
    observed_at REAL NOT NULL,
    symbol TEXT NOT NULL,
    funding_long_pct_h REAL,
    funding_short_pct_h REAL,
    rollover_pct_h REAL,
    oi_long_usd REAL,
    oi_short_usd REAL,
    market_open INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (symbol, observed_at)
);

CREATE TABLE IF NOT EXISTS perps_treasury_observations (
    observed_at REAL PRIMARY KEY,
    idle_usdc REAL,
    aave_usdc_apr_pct REAL,
    vault_7d_apr_pct REAL,
    vault_30d_apr_pct REAL,
    vault_12m_apr_pct REAL,
    best_apr_pct REAL,
    best_venue TEXT,
    missed_yield_usd REAL NOT NULL DEFAULT 0,
    deposited_usdc REAL
);
"""

# Colonnes ajoutées après la création initiale d'une table : appliquées par
# `ALTER TABLE` si absentes (les bases existantes sur le Pi ne sont jamais recréées).
_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("perps_treasury_observations", "deposited_usdc", "REAL"),
)


class PerpsStore:
    def __init__(self, db_path: str | Path = "data/perps_paper.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.executescript(SCHEMA)
            for table, column, column_type in _MIGRATIONS:
                existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
                if column not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def fetch_one(self, query: str, params: tuple = ()) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(query, params).fetchone()
            return dict(row) if row else None

    def fetch_all(self, query: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self.connection() as conn:
            return [dict(row) for row in conn.execute(query, params).fetchall()]

    # --- état -----------------------------------------------------------------
    def get_state(self, key: str, default: str | None = None) -> str | None:
        row = self.fetch_one("SELECT value FROM perps_state WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_state(self, key: str, value: str) -> None:
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO perps_state(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    # --- observations -----------------------------------------------------------
    def record_observation(self, state: PairState) -> None:
        with self.connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO perps_observations (observed_at, symbol, price, spread_pct, open_fee_pct, "
                "close_fee_pct, funding_long_pct_h, funding_short_pct_h, rollover_pct_h, oi_long_usd, oi_short_usd, "
                "market_open) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    state.observed_at, state.symbol, state.price, state.spread_pct, state.open_fee_pct,
                    state.close_fee_pct, state.funding_long_pct_h, state.funding_short_pct_h,
                    state.rollover_pct_h, state.oi_long_usd, state.oi_short_usd, int(state.market_open),
                ),
            )

    def price_at_or_before(self, symbol: str, timestamp: float, max_age_seconds: float) -> float | None:
        """Dernier prix observé à `timestamp` ou avant, s'il n'est pas plus vieux
        que `max_age_seconds` (évite d'utiliser une observation d'avant une
        longue coupure comme référence)."""
        row = self.fetch_one(
            "SELECT price FROM perps_observations WHERE symbol = ? AND observed_at <= ? AND observed_at >= ? "
            "ORDER BY observed_at DESC LIMIT 1",
            (symbol, timestamp, timestamp - max_age_seconds),
        )
        return float(row["price"]) if row else None

    def latest_observations(self) -> list[dict[str, Any]]:
        return self.fetch_all(
            "SELECT o.* FROM perps_observations o JOIN ("
            "  SELECT symbol, MAX(observed_at) AS observed_at FROM perps_observations GROUP BY symbol"
            ") latest ON latest.symbol = o.symbol AND latest.observed_at = o.observed_at ORDER BY o.symbol"
        )

    def carry_apr_history(self, symbol: str, since: float) -> list[dict[str, Any]]:
        """Observations (funding short, rollover) d'une paire depuis `since`, par ordre chronologique."""
        return self.fetch_all(
            "SELECT observed_at, funding_short_pct_h, rollover_pct_h FROM perps_observations "
            "WHERE symbol = ? AND observed_at >= ? ORDER BY observed_at",
            (symbol, since),
        )

    def prune_observations(self, older_than: float) -> int:
        with self.connection() as conn:
            return conn.execute("DELETE FROM perps_observations WHERE observed_at < ?", (older_than,)).rowcount

    # --- positions ----------------------------------------------------------------
    def open_positions(self, strategy: str | None = None) -> list[dict[str, Any]]:
        if strategy is None:
            return self.fetch_all("SELECT * FROM perps_paper_positions WHERE status = 'open' ORDER BY opened_at")
        return self.fetch_all(
            "SELECT * FROM perps_paper_positions WHERE status = 'open' AND strategy = ? ORDER BY opened_at",
            (strategy,),
        )

    def insert_position(self, position: dict[str, Any]) -> int | None:
        """Ouvre une position ; renvoie None si une position est déjà ouverte
        pour (stratégie, symbole) — l'index unique partiel porte l'invariant."""
        columns = list(position.keys())
        placeholders = ", ".join("?" for _ in columns)
        with self.connection() as conn:
            cursor = conn.execute(
                f"INSERT OR IGNORE INTO perps_paper_positions ({', '.join(columns)}) VALUES ({placeholders})",
                tuple(position[column] for column in columns),
            )
            return cursor.lastrowid if cursor.rowcount else None

    def update_position(self, position_id: int, **fields: Any) -> None:
        if not fields:
            return
        assignments = ", ".join(f"{column} = ?" for column in fields)
        with self.connection() as conn:
            conn.execute(
                f"UPDATE perps_paper_positions SET {assignments} WHERE id = ?",
                (*fields.values(), position_id),
            )

    def close_position(self, position_id: int, **fields: Any) -> None:
        self.update_position(position_id, status="closed", **fields)

    def closed_positions(self, strategy: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        if strategy is None:
            return self.fetch_all(
                "SELECT * FROM perps_paper_positions WHERE status = 'closed' ORDER BY closed_at DESC LIMIT ?", (limit,),
            )
        return self.fetch_all(
            "SELECT * FROM perps_paper_positions WHERE status = 'closed' AND strategy = ? "
            "ORDER BY closed_at DESC LIMIT ?",
            (strategy, limit),
        )

    def realized_pnl_since(self, strategy: str, since: float) -> float:
        row = self.fetch_one(
            "SELECT COALESCE(SUM(net_pnl_usd), 0) AS pnl FROM perps_paper_positions "
            "WHERE strategy = ? AND status = 'closed' AND closed_at >= ?",
            (strategy, since),
        )
        return float(row["pnl"]) if row else 0.0

    def last_closed_at(self, strategy: str, symbol: str) -> float | None:
        row = self.fetch_one(
            "SELECT MAX(closed_at) AS closed_at FROM perps_paper_positions "
            "WHERE strategy = ? AND symbol = ? AND status = 'closed'",
            (strategy, symbol),
        )
        return float(row["closed_at"]) if row and row["closed_at"] is not None else None

    def strategy_stats(self, strategy: str) -> dict[str, Any]:
        row = self.fetch_one(
            "SELECT COUNT(*) AS closed_trades, "
            "COALESCE(SUM(CASE WHEN net_pnl_usd > 0 THEN 1 ELSE 0 END), 0) AS wins, "
            "COALESCE(SUM(net_pnl_usd), 0) AS realized_pnl_usd, "
            "COALESCE(SUM(gross_pnl_usd), 0) AS gross_pnl_usd, "
            "COALESCE(SUM(funding_usd), 0) AS funding_paid_usd, "
            "COALESCE(SUM(rollover_usd), 0) AS rollover_paid_usd, "
            "COALESCE(SUM(open_fee_usd + COALESCE(close_fee_usd, 0) + spot_costs_usd), 0) AS fees_usd, "
            "MIN(net_pnl_usd) AS worst_trade_usd, MAX(net_pnl_usd) AS best_trade_usd, "
            "COALESCE(SUM(CASE WHEN exit_reason = 'liquidation' THEN 1 ELSE 0 END), 0) AS liquidations "
            "FROM perps_paper_positions WHERE strategy = ? AND status = 'closed'",
            (strategy,),
        ) or {}
        open_rows = self.open_positions(strategy)
        row["open_positions"] = len(open_rows)
        row["open_notional_usd"] = sum(float(p["notional_usd"]) for p in open_rows)
        closed = int(row.get("closed_trades") or 0)
        row["win_rate_pct"] = (float(row.get("wins") or 0) / closed * 100) if closed else None
        return row

    # --- événements ---------------------------------------------------------------
    def add_event(self, message: str, *, level: str = "info", strategy: str | None = None,
                  symbol: str | None = None, created_at: float | None = None) -> None:
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO perps_events (created_at, level, strategy, symbol, message) VALUES (?, ?, ?, ?, ?)",
                (created_at if created_at is not None else time.time(), level, strategy, symbol, message),
            )

    def recent_events(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.fetch_all("SELECT * FROM perps_events ORDER BY created_at DESC, id DESC LIMIT ?", (limit,))

    # --- scan de funding (toutes paires) ----------------------------------------------
    def record_funding_scan(self, rows: list[dict[str, Any]]) -> None:
        with self.connection() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO perps_funding_scan (observed_at, symbol, funding_long_pct_h, funding_short_pct_h, "
                "rollover_pct_h, oi_long_usd, oi_short_usd, market_open) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (r["observed_at"], r["symbol"], r["funding_long_pct_h"], r["funding_short_pct_h"],
                     r["rollover_pct_h"], r["oi_long_usd"], r["oi_short_usd"], int(r["market_open"]))
                    for r in rows
                ],
            )

    def latest_funding_scan(self) -> list[dict[str, Any]]:
        return self.fetch_all(
            "SELECT f.* FROM perps_funding_scan f JOIN (SELECT MAX(observed_at) AS observed_at FROM perps_funding_scan) "
            "latest ON latest.observed_at = f.observed_at ORDER BY f.symbol"
        )

    def prune_funding_scan(self, older_than: float) -> int:
        with self.connection() as conn:
            return conn.execute("DELETE FROM perps_funding_scan WHERE observed_at < ?", (older_than,)).rowcount

    def funding_scan_averages(self, since: float) -> dict[str, dict[str, float]]:
        """Moyennes par paire depuis `since` (funding par côté, rollover, nombre
        de balayages) : le taux instantané surestime — sur les positions réelles
        de plusieurs jours lues le 09/11/2026, le funding accumulé ne faisait
        que 50–90 % de « taux du jour × heures »."""
        rows = self.fetch_all(
            "SELECT symbol, AVG(funding_long_pct_h) AS funding_long_pct_h, AVG(funding_short_pct_h) AS funding_short_pct_h, "
            "AVG(rollover_pct_h) AS rollover_pct_h, COUNT(*) AS samples, MIN(observed_at) AS first_observed_at "
            "FROM perps_funding_scan WHERE observed_at >= ? GROUP BY symbol",
            (since,),
        )
        return {row["symbol"]: row for row in rows}

    # --- trésorerie ---------------------------------------------------------------------
    def record_treasury_observation(self, row: dict[str, Any]) -> None:
        with self.connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO perps_treasury_observations (observed_at, idle_usdc, aave_usdc_apr_pct, "
                "vault_7d_apr_pct, vault_30d_apr_pct, vault_12m_apr_pct, best_apr_pct, best_venue, missed_yield_usd, "
                "deposited_usdc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row["observed_at"], row.get("idle_usdc"), row.get("aave_usdc_apr_pct"), row.get("vault_7d_apr_pct"),
                    row.get("vault_30d_apr_pct"), row.get("vault_12m_apr_pct"), row.get("best_apr_pct"),
                    row.get("best_venue"), float(row.get("missed_yield_usd") or 0.0), row.get("deposited_usdc"),
                ),
            )

    def latest_treasury_observation(self) -> dict[str, Any] | None:
        return self.fetch_one("SELECT * FROM perps_treasury_observations ORDER BY observed_at DESC LIMIT 1")

    def treasury_history(self, since: float) -> list[dict[str, Any]]:
        return self.fetch_all(
            "SELECT * FROM perps_treasury_observations WHERE observed_at >= ? ORDER BY observed_at", (since,),
        )
