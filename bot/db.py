"""Persistance SQLite : trades, positions ouvertes, courbe d'equity.

SQLite est utilisé volontairement (pas de serveur DB) pour rester léger sur un Raspberry Pi.
"""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional


class Database:
    def __init__(self, db_path: str = "data/trading_bot.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    price REAL NOT NULL,
                    amount REAL NOT NULL,
                    cost REAL NOT NULL,
                    fee REAL DEFAULT 0,
                    pnl REAL DEFAULT 0,
                    mode TEXT NOT NULL,
                    reason TEXT
                );

                CREATE TABLE IF NOT EXISTS positions (
                    symbol TEXT PRIMARY KEY,
                    side TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    amount REAL NOT NULL,
                    stop_loss REAL,
                    take_profit REAL,
                    trailing_stop REAL,
                    opened_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS equity (
                    timestamp REAL NOT NULL,
                    balance REAL NOT NULL,
                    equity REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS vault (
                    asset TEXT PRIMARY KEY,
                    amount REAL NOT NULL DEFAULT 0,
                    total_rewards REAL NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL
                );
                """
            )

    # --- Positions -----------------------------------------------------
    def get_position(self, symbol: str) -> Optional[sqlite3.Row]:
        with self._connect() as conn:
            cur = conn.execute("SELECT * FROM positions WHERE symbol = ?", (symbol,))
            return cur.fetchone()

    def get_all_positions(self) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute("SELECT * FROM positions").fetchall()

    def open_position(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        amount: float,
        stop_loss: float | None,
        take_profit: float | None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO positions
                   (symbol, side, entry_price, amount, stop_loss, take_profit, trailing_stop, opened_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (symbol, side, entry_price, amount, stop_loss, take_profit, None, time.time()),
            )

    def update_trailing_stop(self, symbol: str, trailing_stop: float) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE positions SET trailing_stop = ? WHERE symbol = ?", (trailing_stop, symbol))

    def close_position(self, symbol: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM positions WHERE symbol = ?", (symbol,))

    # --- Trades ----------------------------------------------------------
    def record_trade(
        self,
        symbol: str,
        side: str,
        price: float,
        amount: float,
        cost: float,
        mode: str,
        fee: float = 0.0,
        pnl: float = 0.0,
        reason: str = "",
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO trades (timestamp, symbol, side, price, amount, cost, fee, pnl, mode, reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (time.time(), symbol, side, price, amount, cost, fee, pnl, mode, reason),
            )

    def get_recent_trades(self, limit: int = 20) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM trades ORDER BY timestamp DESC LIMIT ?", (limit,)
            ).fetchall()

    def get_daily_pnl(self) -> float:
        since = time.time() - 24 * 3600
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(pnl), 0) as total FROM trades WHERE timestamp >= ?", (since,)
            ).fetchone()
            return float(row["total"])

    # --- Equity ------------------------------------------------------------
    def record_equity(self, balance: float, equity: float) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO equity (timestamp, balance, equity) VALUES (?, ?, ?)",
                (time.time(), balance, equity),
            )

    def get_latest_equity(self) -> Optional[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute("SELECT * FROM equity ORDER BY timestamp DESC LIMIT 1").fetchone()

    def get_equity_history(self, limit: int = 500) -> list[sqlite3.Row]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM equity ORDER BY timestamp DESC LIMIT ?", (limit,)
            ).fetchall()
            return list(reversed(rows))

    def get_trade_stats(self) -> dict:
        """Statistiques globales sur les trades clôturés (side = sell)."""
        with self._connect() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS n,
                          COALESCE(SUM(pnl), 0) AS total_pnl,
                          COALESCE(SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END), 0) AS wins,
                          COALESCE(SUM(fee), 0) AS fees
                   FROM trades WHERE side = 'sell'"""
            ).fetchone()
            n = int(row["n"])
            return {
                "closed_trades": n,
                "total_pnl": float(row["total_pnl"]),
                "win_rate_pct": (int(row["wins"]) / n * 100.0) if n else 0.0,
                "total_fees": float(row["fees"]),
            }

    def get_pnl_by_symbol(self) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                """SELECT symbol, COUNT(*) AS n, COALESCE(SUM(pnl), 0) AS pnl
                   FROM trades WHERE side = 'sell' GROUP BY symbol ORDER BY pnl DESC"""
            ).fetchall()

    # --- Vault (coffre de récompenses) ---------------------------------
    def get_vault(self, asset: str) -> tuple[float, float, float]:
        """Retourne (montant, total_récompenses, updated_at) du coffre pour un actif."""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM vault WHERE asset = ?", (asset,)).fetchone()
            if row is None:
                return 0.0, 0.0, time.time()
            return float(row["amount"]), float(row["total_rewards"]), float(row["updated_at"])

    def set_vault(self, asset: str, amount: float, total_rewards: float) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO vault (asset, amount, total_rewards, updated_at) VALUES (?, ?, ?, ?)
                   ON CONFLICT(asset) DO UPDATE SET amount = ?, total_rewards = ?, updated_at = ?""",
                (asset, amount, total_rewards, time.time(), amount, total_rewards, time.time()),
            )
