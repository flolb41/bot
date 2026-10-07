"""Accès SQLite pour le Crypto Reward Hunter.

Tables : projects, tasks, wallets, rewards, transactions.
Important : aucune colonne ne doit jamais contenir de seed phrase ou de private key.
"""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    token TEXT,
    network TEXT,
    url TEXT,
    official_url TEXT,
    type TEXT,
    status TEXT DEFAULT 'actif',
    score INTEGER DEFAULT 0,
    start_date TEXT,
    deadline TEXT,
    requires_kyc INTEGER DEFAULT 0,
    requires_capital INTEGER DEFAULT 0,
    estimated_cost REAL DEFAULT 0,
    notes TEXT,
    last_content_hash TEXT,
    last_checked_at REAL,
    created_at REAL DEFAULT (strftime('%s','now')),
    updated_at REAL DEFAULT (strftime('%s','now'))
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    description TEXT,
    url TEXT,
    difficulty TEXT,
    estimated_time TEXT,
    reward_type TEXT,
    reward_estimate TEXT,
    status TEXT DEFAULT 'pending',
    deadline TEXT,
    created_at REAL DEFAULT (strftime('%s','now')),
    updated_at REAL DEFAULT (strftime('%s','now'))
);

-- Ne jamais stocker de private key ou seed phrase dans cette table.
CREATE TABLE IF NOT EXISTS wallets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    address TEXT NOT NULL,
    network TEXT NOT NULL,
    purpose TEXT,
    created_at REAL DEFAULT (strftime('%s','now'))
);

CREATE TABLE IF NOT EXISTS rewards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT REFERENCES projects(id) ON DELETE SET NULL,
    token TEXT NOT NULL,
    amount REAL NOT NULL,
    date REAL DEFAULT (strftime('%s','now')),
    wallet TEXT,
    status TEXT DEFAULT 'pending',
    tx_hash TEXT
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    network TEXT NOT NULL,
    tx_hash TEXT NOT NULL,
    wallet TEXT,
    action TEXT,
    gas_cost REAL DEFAULT 0,
    token TEXT,
    amount REAL,
    timestamp REAL DEFAULT (strftime('%s','now'))
);

-- Historique des points/XP par projet (pour calculer les deltas).
CREATE TABLE IF NOT EXISTS points_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    points REAL NOT NULL,
    recorded_at REAL DEFAULT (strftime('%s','now'))
);

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

-- Candidats de projets détectés automatiquement (section "découverte de nouveaux
-- projets") à partir de sources publiques (flux RSS airdrops.io, r/airdrops...).
-- Jamais promu automatiquement vers `projects` : validation humaine obligatoire
-- avant tout suivi actif (golden rule anti-scam du projet).
CREATE TABLE IF NOT EXISTS discovered_projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE,
    summary TEXT,
    published_at TEXT,
    status TEXT DEFAULT 'pending_review',
    first_seen_at REAL DEFAULT (strftime('%s','now'))
);

CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id);
CREATE INDEX IF NOT EXISTS idx_rewards_project ON rewards(project_id);
CREATE INDEX IF NOT EXISTS idx_points_history_project ON points_history(project_id);
CREATE INDEX IF NOT EXISTS idx_notifications_created ON notifications(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_discovered_status ON discovered_projects(status);

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

    # --- projects -------------------------------------------------------
    def upsert_project(self, project: dict[str, Any]) -> None:
        existing = self.fetch_one("SELECT id FROM projects WHERE id = ?", (project["id"],))
        now = time.time()
        if existing:
            fields = {k: v for k, v in project.items() if k != "id"}
            fields["updated_at"] = now
            set_clause = ", ".join(f"{k} = ?" for k in fields)
            self.execute(
                f"UPDATE projects SET {set_clause} WHERE id = ?",
                (*fields.values(), project["id"]),
            )
        else:
            project.setdefault("created_at", now)
            project.setdefault("updated_at", now)
            cols = ", ".join(project.keys())
            placeholders = ", ".join("?" for _ in project)
            self.execute(
                f"INSERT INTO projects ({cols}) VALUES ({placeholders})",
                tuple(project.values()),
            )

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        return self.fetch_one("SELECT * FROM projects WHERE id = ?", (project_id,))

    def list_projects(self, order_by: str = "score DESC") -> list[dict[str, Any]]:
        return self.fetch_all(f"SELECT * FROM projects ORDER BY {order_by}")

    def update_project_score(self, project_id: str, score: int) -> None:
        self.execute(
            "UPDATE projects SET score = ?, updated_at = ? WHERE id = ?",
            (score, time.time(), project_id),
        )

    def update_project_scan_result(self, project_id: str, content_hash: str | None) -> bool:
        """Met à jour le hash de contenu suivi, retourne True si changement détecté."""
        project = self.get_project(project_id)
        changed = bool(project and project.get("last_content_hash") and content_hash
                       and project["last_content_hash"] != content_hash)
        self.execute(
            "UPDATE projects SET last_content_hash = ?, last_checked_at = ? WHERE id = ?",
            (content_hash, time.time(), project_id),
        )
        return changed

    # --- tasks ------------------------------------------------------------
    def add_task(self, task: dict[str, Any]) -> int:
        cols = ", ".join(task.keys())
        placeholders = ", ".join("?" for _ in task)
        return self.execute(f"INSERT INTO tasks ({cols}) VALUES ({placeholders})", tuple(task.values()))

    def list_tasks(self, project_id: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM tasks WHERE 1=1"
        params: list[Any] = []
        if project_id:
            query += " AND project_id = ?"
            params.append(project_id)
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY deadline IS NULL, deadline ASC"
        return self.fetch_all(query, tuple(params))

    # --- wallets ------------------------------------------------------------
    def upsert_wallet(self, name: str, address: str, network: str, purpose: str = "") -> None:
        existing = self.fetch_one("SELECT id FROM wallets WHERE name = ?", (name,))
        if existing:
            self.execute(
                "UPDATE wallets SET address = ?, network = ?, purpose = ? WHERE name = ?",
                (address, network, purpose, name),
            )
        else:
            self.execute(
                "INSERT INTO wallets (name, address, network, purpose) VALUES (?, ?, ?, ?)",
                (name, address, network, purpose),
            )

    def list_wallets(self) -> list[dict[str, Any]]:
        return self.fetch_all("SELECT * FROM wallets ORDER BY name")

    # --- rewards ------------------------------------------------------------
    def add_reward(self, reward: dict[str, Any]) -> int:
        cols = ", ".join(reward.keys())
        placeholders = ", ".join("?" for _ in reward)
        return self.execute(f"INSERT INTO rewards ({cols}) VALUES ({placeholders})", tuple(reward.values()))

    def list_rewards(self, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            return self.fetch_all("SELECT * FROM rewards WHERE status = ? ORDER BY date DESC", (status,))
        return self.fetch_all("SELECT * FROM rewards ORDER BY date DESC")

    # --- transactions ---------------------------------------------------
    def add_transaction(self, tx: dict[str, Any]) -> int:
        cols = ", ".join(tx.keys())
        placeholders = ", ".join("?" for _ in tx)
        return self.execute(f"INSERT INTO transactions ({cols}) VALUES ({placeholders})", tuple(tx.values()))

    def list_transactions(self) -> list[dict[str, Any]]:
        return self.fetch_all("SELECT * FROM transactions ORDER BY timestamp DESC")

    # --- points history -------------------------------------------------
    def record_points(self, project_id: str, points: float) -> None:
        self.execute(
            "INSERT INTO points_history (project_id, points) VALUES (?, ?)",
            (project_id, points),
        )

    def last_points(self, project_id: str) -> dict[str, Any] | None:
        return self.fetch_one(
            "SELECT * FROM points_history WHERE project_id = ? ORDER BY recorded_at DESC LIMIT 1",
            (project_id,),
        )

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

    # --- découverte automatique de nouveaux projets (sources publiques) -
    def discovered_project_exists(self, url: str) -> bool:
        return self.fetch_one("SELECT 1 FROM discovered_projects WHERE url = ?", (url,)) is not None

    def add_discovered_project(self, source: str, title: str, url: str,
                                summary: str = "", published_at: str | None = None,
                                status: str = "pending_review") -> int | None:
        """Insère un candidat détecté. Ne fait rien s'il existe déjà (URL unique).
        `status='auto_approved'` signifie qu'il a passé les garde-fous objectifs
        de app.discovery.scanner (source fiable + pas de mot-clé à risque + lien
        actif) — jamais une garantie de légitimité totale, et jamais une
        autorisation d'automatisation financière (claim/autosign)."""
        if self.discovered_project_exists(url):
            return None
        return self.execute(
            "INSERT INTO discovered_projects (source, title, url, summary, published_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (source, title, url, summary, published_at, status),
        )

    def list_discovered_projects(self, status: str | None = "pending_review",
                                  limit: int = 100) -> list[dict[str, Any]]:
        query = "SELECT * FROM discovered_projects"
        params: tuple = ()
        if status:
            query += " WHERE status = ?"
            params = (status,)
        query += " ORDER BY first_seen_at DESC LIMIT ?"
        return self.fetch_all(query, params + (limit,))

    def set_discovered_project_status(self, discovered_id: int, status: str) -> None:
        self.execute(
            "UPDATE discovered_projects SET status = ? WHERE id = ?",
            (status, discovered_id),
        )

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
