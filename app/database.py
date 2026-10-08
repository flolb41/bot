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

-- Signaux d'arbitrage TRIANGULAIRE (un seul DEX, cycle à 3 jambes
-- token_a -> token_b -> token_c -> token_a, exécuté en UNE SEULE transaction
-- multi-hop — voir app/trading/triangular.py). `token_a` est toujours un
-- quote token exécutable (USDC/WETH, voir EXECUTABLE_QUOTE_TOKENS) : c'est le
-- capital qui part et revient dans le wallet. Contrairement à
-- arbitrage_signals (2 transactions séparées), un cycle triangulaire est
-- atomique par construction (si une jambe échoue, toute la transaction
-- revert) : pas de risque de position résiduelle ouverte.
CREATE TABLE IF NOT EXISTS triangular_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_at REAL DEFAULT (strftime('%s','now')),
    chain TEXT NOT NULL,
    dex TEXT NOT NULL,
    token_a_symbol TEXT NOT NULL,
    token_a_address TEXT NOT NULL,
    token_b_symbol TEXT NOT NULL,
    token_b_address TEXT NOT NULL,
    token_c_symbol TEXT NOT NULL,
    token_c_address TEXT NOT NULL,
    cycle_multiplier REAL NOT NULL,
    spread_pct REAL NOT NULL,
    min_liquidity_usd REAL,
    trade_size_usd REAL NOT NULL,
    gross_profit_usd REAL NOT NULL,
    fees_usd REAL NOT NULL,
    slippage_buffer_usd REAL NOT NULL,
    gas_cost_usd REAL,
    net_profit_usd REAL,
    would_execute INTEGER DEFAULT 0,
    mode TEXT DEFAULT 'simulation',
    executed INTEGER DEFAULT 0,
    tx_hash TEXT,
    execution_error TEXT
);

CREATE INDEX IF NOT EXISTS idx_triangular_detected ON triangular_signals(detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_triangular_would_execute ON triangular_signals(would_execute);

-- Cycles triangulaires détectés à CHEVAL sur plusieurs DEX (chaque jambe
-- potentiellement sur un DEX différent, voir app.trading.triangular.
-- run_cross_dex_triangular_scan) : purement INFORMATIONNEL, jamais exécuté
-- automatiquement (`would_execute` toujours 0) car aucune exécution atomique
-- en une seule transaction n'est possible sans redéployer FlashArbitrage.sol
-- pour router vers 3 DEX distincts dans la même tx.
CREATE TABLE IF NOT EXISTS cross_dex_triangular_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_at REAL DEFAULT (strftime('%s','now')),
    chain TEXT NOT NULL,
    token_a_symbol TEXT NOT NULL,
    token_a_address TEXT NOT NULL,
    token_b_symbol TEXT NOT NULL,
    token_b_address TEXT NOT NULL,
    token_c_symbol TEXT NOT NULL,
    token_c_address TEXT NOT NULL,
    dex_ab TEXT NOT NULL,
    dex_bc TEXT NOT NULL,
    dex_ca TEXT NOT NULL,
    cycle_multiplier REAL NOT NULL,
    spread_pct REAL NOT NULL,
    min_liquidity_usd REAL
);

CREATE INDEX IF NOT EXISTS idx_cross_dex_triangular_detected ON cross_dex_triangular_signals(detected_at DESC);
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
            "ALTER TABLE arbitrage_signals ADD COLUMN flashloan_notional_usd REAL",
            "ALTER TABLE arbitrage_signals ADD COLUMN flashloan_net_profit_usd REAL",
            "ALTER TABLE triangular_signals ADD COLUMN flashloan_notional_usd REAL",
            "ALTER TABLE triangular_signals ADD COLUMN flashloan_net_profit_usd REAL",
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
            "gas_cost_usd, net_profit_usd, would_execute, mode, executed, tx_hash, "
            "flashloan_notional_usd, flashloan_net_profit_usd"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                signal.get("flashloan_notional_usd"), signal.get("flashloan_net_profit_usd"),
            ),
        )

    def list_arbitrage_signals(self, limit: int = 50, only_would_execute: bool = False) -> list[dict[str, Any]]:
        query = "SELECT * FROM arbitrage_signals"
        if only_would_execute:
            query += " WHERE would_execute = 1"
        query += " ORDER BY detected_at DESC LIMIT ?"
        return self.fetch_all(query, (limit,))

    def pair_execution_history(self, *, token_symbol: str, buy_dex: str, sell_dex: str,
                                limit: int = 20) -> list[dict[str, Any]]:
        """Historique récent (le plus récent en premier) des tentatives
        d'exécution RÉELLES (`executed = 1`) pour une paire buy/sell donnée —
        utilisé par `app.trading.pair_health` pour le seuil de profit adaptatif
        et la désactivation automatique après échecs consécutifs."""
        return self.fetch_all(
            "SELECT detected_at, tx_hash_sell, execution_error FROM arbitrage_signals "
            "WHERE executed = 1 AND token_symbol = ? AND buy_dex = ? AND sell_dex = ? "
            "ORDER BY detected_at DESC, id DESC LIMIT ?",
            (token_symbol, buy_dex, sell_dex, limit),
        )

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
        (garde-fou de plafond quotidien, voir app/trading/guardrails.py).
        Compte à la fois les round-trips classiques (2 jambes) ET les cycles
        triangulaires (1 transaction) : le plafond TRADING_MAX_TX_PER_DAY
        s'applique globalement, pas par type de stratégie."""
        start_of_day = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        row = self.fetch_one(
            "SELECT "
            "(SELECT COUNT(*) FROM arbitrage_signals WHERE executed = 1 AND detected_at >= ?) + "
            "(SELECT COUNT(*) FROM triangular_signals WHERE executed = 1 AND detected_at >= ?) AS n",
            (start_of_day, start_of_day),
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

    # --- bot de trading : signaux d'arbitrage TRIANGULAIRE (1 seul DEX, 1 seule tx) ---
    def add_triangular_signal(self, signal: dict[str, Any]) -> int:
        return self.execute(
            "INSERT INTO triangular_signals ("
            "chain, dex, token_a_symbol, token_a_address, token_b_symbol, token_b_address, "
            "token_c_symbol, token_c_address, cycle_multiplier, spread_pct, min_liquidity_usd, "
            "trade_size_usd, gross_profit_usd, fees_usd, slippage_buffer_usd, gas_cost_usd, "
            "net_profit_usd, would_execute, mode, executed, tx_hash, "
            "flashloan_notional_usd, flashloan_net_profit_usd"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                signal["chain"], signal["dex"],
                signal["token_a_symbol"], signal["token_a_address"],
                signal["token_b_symbol"], signal["token_b_address"],
                signal["token_c_symbol"], signal["token_c_address"],
                signal["cycle_multiplier"], signal["spread_pct"], signal.get("min_liquidity_usd"),
                signal["trade_size_usd"], signal["gross_profit_usd"], signal["fees_usd"],
                signal["slippage_buffer_usd"], signal.get("gas_cost_usd"),
                signal.get("net_profit_usd"), int(bool(signal.get("would_execute"))),
                signal.get("mode", "simulation"), int(bool(signal.get("executed"))),
                signal.get("tx_hash"),
                signal.get("flashloan_notional_usd"), signal.get("flashloan_net_profit_usd"),
            ),
        )

    def list_triangular_signals(self, limit: int = 50, only_would_execute: bool = False) -> list[dict[str, Any]]:
        query = "SELECT * FROM triangular_signals"
        if only_would_execute:
            query += " WHERE would_execute = 1"
        query += " ORDER BY detected_at DESC LIMIT ?"
        return self.fetch_all(query, (limit,))

    def add_cross_dex_triangular_signal(self, signal: dict[str, Any]) -> int:
        """Journalise un cycle triangulaire détecté à cheval sur plusieurs DEX —
        voir `app.trading.triangular.run_cross_dex_triangular_scan`. Purement
        informationnel (pas de colonnes would_execute/executed : jamais
        exécuté automatiquement, voir docstring du schéma)."""
        return self.execute(
            "INSERT INTO cross_dex_triangular_signals ("
            "chain, token_a_symbol, token_a_address, token_b_symbol, token_b_address, "
            "token_c_symbol, token_c_address, dex_ab, dex_bc, dex_ca, "
            "cycle_multiplier, spread_pct, min_liquidity_usd"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                signal["chain"],
                signal["token_a_symbol"], signal["token_a_address"],
                signal["token_b_symbol"], signal["token_b_address"],
                signal["token_c_symbol"], signal["token_c_address"],
                signal["dex_ab"], signal["dex_bc"], signal["dex_ca"],
                signal["cycle_multiplier"], signal["spread_pct"], signal.get("min_liquidity_usd"),
            ),
        )

    def list_cross_dex_triangular_signals(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.fetch_all(
            "SELECT * FROM cross_dex_triangular_signals ORDER BY detected_at DESC LIMIT ?",
            (limit,),
        )

    def triangular_execution_history(self, *, dex: str, token_a_symbol: str, token_b_symbol: str,
                                      token_c_symbol: str, limit: int = 20) -> list[dict[str, Any]]:
        """Équivalent triangulaire de `pair_execution_history`, voir
        `app.trading.pair_health`."""
        return self.fetch_all(
            "SELECT detected_at, tx_hash, execution_error FROM triangular_signals "
            "WHERE executed = 1 AND dex = ? AND token_a_symbol = ? AND token_b_symbol = ? AND token_c_symbol = ? "
            "ORDER BY detected_at DESC, id DESC LIMIT ?",
            (dex, token_a_symbol, token_b_symbol, token_c_symbol, limit),
        )

    def mark_triangular_signal_executed(self, signal_id: int, *, tx_hash: str | None = None,
                                         execution_error: str | None = None) -> None:
        """Enregistre le résultat réel d'une tentative d'exécution d'un cycle
        triangulaire. Contrairement à `mark_arbitrage_signal_executed`, une
        seule transaction (atomique) : soit elle passe entièrement (tx_hash
        renseigné, execution_error=None), soit elle revert entièrement
        (execution_error renseigné, tx_hash=None) — jamais d'état intermédiaire."""
        self.execute(
            "UPDATE triangular_signals SET executed = 1, tx_hash = ?, execution_error = ?, "
            "mode = 'live' WHERE id = ?",
            (tx_hash, execution_error, signal_id),
        )

    def triangular_stats_summary(self) -> dict[str, Any]:
        """Agrégat utile pour le dashboard, équivalent de `arbitrage_stats_summary`
        pour les cycles triangulaires."""
        row = self.fetch_one(
            "SELECT COUNT(*) AS total, "
            "SUM(would_execute) AS would_execute_count, "
            "SUM(CASE WHEN would_execute = 1 THEN net_profit_usd ELSE 0 END) AS cumulative_net_profit_usd, "
            "MAX(detected_at) AS last_scan_at "
            "FROM triangular_signals"
        )
        return row if row else {
            "total": 0, "would_execute_count": 0, "cumulative_net_profit_usd": 0.0, "last_scan_at": None,
        }
