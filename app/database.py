"""Accès SQLite pour le bot d'arbitrage inter-DEX.

Tables : system_state (killswitch), notifications (alertes), arbitrage_signals
(signaux détectés + exécutions réelles).
"""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
-- État global du système (killswitch, dernière exécution, etc.).
CREATE TABLE IF NOT EXISTS system_state (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- Journal des notifications (alternative/complément à Telegram), consulté par le dashboard web.
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    level TEXT DEFAULT 'info',
    title TEXT NOT NULL,
    message TEXT,
    project_id TEXT,
    is_read INTEGER DEFAULT 0,
    created_at REAL DEFAULT (strftime('%s','now'))
);

CREATE INDEX IF NOT EXISTS idx_notifications_created ON notifications(created_at DESC);

-- Signaux d'arbitrage inter-DEX détectés (bot de trading, phase simulation).
-- `mode` reste 'simulation' tant que `trading.enabled` est false en config :
-- aucune transaction réelle n'est jamais envoyée pour ces lignes-là, seul le
-- profit net est ESTIMÉ pour valider la stratégie avant d'y risquer du capital.
CREATE TABLE IF NOT EXISTS arbitrage_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_at REAL DEFAULT (strftime('%s','now')),
    chain TEXT NOT NULL,
    token_symbol TEXT NOT NULL,
    token_address TEXT NOT NULL,
    quote_symbol TEXT NOT NULL,
    quote_address TEXT NOT NULL,
    buy_dex TEXT NOT NULL,
    buy_price_usd REAL NOT NULL,
    buy_liquidity_usd REAL,
    sell_dex TEXT NOT NULL,
    sell_price_usd REAL NOT NULL,
    sell_liquidity_usd REAL,
    spread_pct REAL NOT NULL,
    trade_size_usd REAL NOT NULL,
    gross_profit_usd REAL NOT NULL,
    fees_usd REAL NOT NULL,
    slippage_buffer_usd REAL NOT NULL,
    gas_cost_usd REAL,
    net_profit_usd REAL,
    would_execute INTEGER DEFAULT 0,
    mode TEXT DEFAULT 'simulation',
    executed INTEGER DEFAULT 0,
    tx_hash TEXT
);

CREATE INDEX IF NOT EXISTS idx_arbitrage_detected ON arbitrage_signals(detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_arbitrage_would_execute ON arbitrage_signals(would_execute);
"""


class Database:
    """Wrapper SQLite minimaliste, adapté aux ressources limitées du Raspberry Pi 3."""

    def __init__(self, db_path: str | Path = "data/reward_hunter.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            self._run_migrations(conn)

    def _run_migrations(self, conn: sqlite3.Connection) -> None:
        """Ajoute des colonnes à des tables existantes sans casser les bases déjà
        déployées (SQLite ne supporte pas `ADD COLUMN IF NOT EXISTS`, donc on
        catch l'erreur "duplicate column" qui signifie juste que c'est déjà fait)."""
        migrations = [
            "ALTER TABLE arbitrage_signals ADD COLUMN tx_hash_buy TEXT",
            "ALTER TABLE arbitrage_signals ADD COLUMN tx_hash_sell TEXT",
            "ALTER TABLE arbitrage_signals ADD COLUMN execution_error TEXT",
        ]
        for stmt in migrations:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # --- helpers génériques -------------------------------------------------
    def execute(self, query: str, params: tuple = ()) -> int:
        with self.connection() as conn:
            cur = conn.execute(query, params)
            return cur.lastrowid

    def fetch_one(self, query: str, params: tuple = ()) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(query, params).fetchone()
            return dict(row) if row else None

    def fetch_all(self, query: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    # --- system state (killswitch, etc.) --------------------------------
    def get_state(self, key: str, default: str | None = None) -> str | None:
        row = self.fetch_one("SELECT value FROM system_state WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_state(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO system_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # --- notifications (alternative/complément web au Telegram) --------
    def add_notification(self, title: str, message: str = "", level: str = "info",
                          project_id: str | None = None) -> int:
        return self.execute(
            "INSERT INTO notifications (title, message, level, project_id) VALUES (?, ?, ?, ?)",
            (title, message, level, project_id),
        )

    def list_notifications(self, limit: int = 50, unread_only: bool = False) -> list[dict[str, Any]]:
        query = "SELECT * FROM notifications"
        if unread_only:
            query += " WHERE is_read = 0"
        query += " ORDER BY created_at DESC LIMIT ?"
        return self.fetch_all(query, (limit,))

    def count_unread_notifications(self) -> int:
        row = self.fetch_one("SELECT COUNT(*) AS n FROM notifications WHERE is_read = 0")
        return row["n"] if row else 0

    def mark_notifications_read(self) -> None:
        self.execute("UPDATE notifications SET is_read = 1 WHERE is_read = 0")

    # --- bot de trading : signaux d'arbitrage inter-DEX (phase simulation) ---
    def add_arbitrage_signal(self, signal: dict[str, Any]) -> int:
        return self.execute(
            "INSERT INTO arbitrage_signals ("
            "chain, token_symbol, token_address, quote_symbol, quote_address, "
            "buy_dex, buy_price_usd, buy_liquidity_usd, sell_dex, sell_price_usd, sell_liquidity_usd, "
            "spread_pct, trade_size_usd, gross_profit_usd, fees_usd, slippage_buffer_usd, "
            "gas_cost_usd, net_profit_usd, would_execute, mode, executed, tx_hash"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                signal["chain"], signal["token_symbol"], signal["token_address"],
                signal["quote_symbol"], signal["quote_address"],
                signal["buy_dex"], signal["buy_price_usd"], signal.get("buy_liquidity_usd"),
                signal["sell_dex"], signal["sell_price_usd"], signal.get("sell_liquidity_usd"),
                signal["spread_pct"], signal["trade_size_usd"], signal["gross_profit_usd"],
                signal["fees_usd"], signal["slippage_buffer_usd"], signal.get("gas_cost_usd"),
                signal.get("net_profit_usd"), int(bool(signal.get("would_execute"))),
                signal.get("mode", "simulation"), int(bool(signal.get("executed"))),
                signal.get("tx_hash"),
            ),
        )

    def list_arbitrage_signals(self, limit: int = 50, only_would_execute: bool = False) -> list[dict[str, Any]]:
        query = "SELECT * FROM arbitrage_signals"
        if only_would_execute:
            query += " WHERE would_execute = 1"
        query += " ORDER BY detected_at DESC LIMIT ?"
        return self.fetch_all(query, (limit,))

    def mark_arbitrage_signal_executed(self, signal_id: int, *, tx_hash_buy: str | None = None,
                                        tx_hash_sell: str | None = None,
                                        execution_error: str | None = None) -> None:
        """Enregistre le résultat réel (Phase 2) d'une tentative d'exécution d'un
        signal d'arbitrage. `execution_error` non-null signifie un round-trip
        incomplet/échoué (voir app/trading/executor.py pour la gestion du risque
        non-atomique : tx_hash_buy peut être renseigné seul si la 2e jambe échoue)."""
        self.execute(
            "UPDATE arbitrage_signals SET executed = 1, tx_hash_buy = ?, tx_hash_sell = ?, "
            "execution_error = ?, mode = 'live' WHERE id = ?",
            (tx_hash_buy, tx_hash_sell, execution_error, signal_id),
        )

    def list_open_positions(self) -> list[dict[str, Any]]:
        """Round-trips dont la jambe d'achat a été confirmée on-chain (tx_hash_buy
        renseigné) mais dont la jambe de vente n'a jamais abouti (tx_hash_sell
        toujours NULL) — un token non-USDC est potentiellement resté dans le
        wallet de trading. Une ligne par `token_address` (la plus récente),
        pour que `app.trading.executor.recover_open_positions` sache quel
        solde on-chain tenter de revendre."""
        return self.fetch_all(
            "SELECT * FROM arbitrage_signals WHERE tx_hash_buy IS NOT NULL AND tx_hash_sell IS NULL "
            "AND id IN ("
            "  SELECT MAX(id) FROM arbitrage_signals "
            "  WHERE tx_hash_buy IS NOT NULL AND tx_hash_sell IS NULL GROUP BY token_address"
            ") ORDER BY detected_at DESC"
        )

    def today_executed_trades_count(self) -> int:
        """Nombre de round-trips d'arbitrage réellement exécutés depuis minuit UTC
        (garde-fou de plafond quotidien, voir app/trading/guardrails.py)."""
        start_of_day = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        row = self.fetch_one(
            "SELECT COUNT(*) AS n FROM arbitrage_signals WHERE executed = 1 AND detected_at >= ?",
            (start_of_day,),
        )
        return int(row["n"]) if row else 0

    def arbitrage_stats_summary(self) -> dict[str, Any]:
        """Agrégat utile pour le dashboard : nombre de signaux, nombre de signaux
        jugés rentables (would_execute), P&L cumulé hypothétique (simulation)."""
        row = self.fetch_one(
            "SELECT COUNT(*) AS total, "
            "SUM(would_execute) AS would_execute_count, "
            "SUM(CASE WHEN would_execute = 1 THEN net_profit_usd ELSE 0 END) AS cumulative_net_profit_usd, "
            "MAX(detected_at) AS last_scan_at "
            "FROM arbitrage_signals"
        )
        return row if row else {
            "total": 0, "would_execute_count": 0, "cumulative_net_profit_usd": 0.0, "last_scan_at": None,
        }

    def live_trading_stats(self) -> dict[str, Any]:
        """Agrégat des round-trips RÉELS (mode='live', tx_hash_buy renseigné) :
        combien complétés (tx_hash_sell renseigné) vs encore ouverts (position
        résiduelle en attente de clôture, voir `list_open_positions`)."""
        row = self.fetch_one(
            "SELECT COUNT(*) AS total_live, "
            "SUM(CASE WHEN tx_hash_sell IS NOT NULL THEN 1 ELSE 0 END) AS completed, "
            "SUM(CASE WHEN tx_hash_sell IS NULL THEN 1 ELSE 0 END) AS open_count "
            "FROM arbitrage_signals WHERE mode = 'live' AND tx_hash_buy IS NOT NULL"
        )
        return row if row else {"total_live": 0, "completed": 0, "open_count": 0}
