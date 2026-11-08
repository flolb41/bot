from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.database import Database


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "test.db")


def test_schema_creates_all_tables(db: Database):
    tables = {row["name"] for row in db.fetch_all(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    expected = {"system_state", "notifications", "arbitrage_signals"}
    assert expected.issubset(tables)


def test_killswitch_state(db: Database):
    from app.killswitch import is_stopped, resume, stop

    assert is_stopped(db) is False
    stop(db)
    assert is_stopped(db) is True
    resume(db)
    assert is_stopped(db) is False


def test_scan_retention_archives_old_telemetry_and_preserves_live_history(db: Database):
    cutoff = 1_000_000.0 - 90 * 24 * 60 * 60
    old = cutoff - 1
    recent = cutoff + 1
    old_simulation = db.add_arbitrage_signal(_sample_signal())
    old_live = db.add_arbitrage_signal(_sample_signal(mode="live"))
    db.execute(
        "UPDATE arbitrage_signals SET detected_at=? WHERE id IN (?, ?)",
        (old, old_simulation, old_live),
    )
    db.execute(
        "INSERT INTO paper_market_observations "
        "(strategy, token_address, token_symbol, quote_address, dex, price_usd, "
        "liquidity_usd, volume_24h_usd, observed_at, minute_bucket) "
        "VALUES ('liquid_momentum', '0x1', 'TEST', '0x2', 'sushiswap', 1, 100000, "
        "500000, ?, 1), ('liquid_momentum', '0x3', 'NEW', '0x2', 'sushiswap', 1, "
        "100000, 500000, ?, 2)",
        (old, recent),
    )
    read_id = db.add_notification("old read")
    unread_id = db.add_notification("old unread")
    db.execute(
        "UPDATE notifications SET created_at=?, is_read=1 WHERE id=?",
        (old, read_id),
    )
    db.execute(
        "UPDATE notifications SET created_at=?, is_read=0 WHERE id=?",
        (old, unread_id),
    )

    archived = db.archive_expired_scan_data(retention_days=90, now=1_000_000.0)

    assert archived["arbitrage_signals"] == 1
    assert archived["paper_market_observations"] == 1
    assert archived["notifications"] == 1
    assert db.fetch_one("SELECT id FROM arbitrage_signals WHERE id=?", (old_simulation,)) is None
    assert db.fetch_one("SELECT id FROM arbitrage_signals WHERE id=?", (old_live,)) is not None
    assert db.fetch_one(
        "SELECT token_address FROM paper_market_observations WHERE token_address='0x3'"
    ) is not None
    assert db.fetch_one("SELECT id FROM notifications WHERE id=?", (unread_id,)) is not None

    archive_path = db.db_path.with_name("reward_hunter_archive.db")
    archived_db = Database(archive_path)
    assert archived_db.fetch_one(
        "SELECT id FROM arbitrage_signals WHERE id=?", (old_simulation,)
    ) is not None
    assert archived_db.fetch_one(
        "SELECT token_address FROM paper_market_observations WHERE token_address='0x1'"
    ) is not None


@pytest.mark.parametrize("retention_days", [0, -1, True, 1.5])
def test_scan_retention_rejects_invalid_policy(db: Database, retention_days):
    with pytest.raises(ValueError, match="positive integer"):
        db.archive_expired_scan_data(retention_days=retention_days)


def _sample_signal(**overrides) -> dict:
    signal = {
        "chain": "base", "token_symbol": "WETH", "token_address": "0xabc",
        "quote_symbol": "USDC", "quote_address": "0xdef",
        "buy_dex": "uniswap", "buy_price_usd": 1.0, "buy_liquidity_usd": 50000,
        "sell_dex": "aerodrome", "sell_price_usd": 1.01, "sell_liquidity_usd": 50000,
        "spread_pct": 1.0, "trade_size_usd": 3.0, "gross_profit_usd": 0.03,
        "fees_usd": 0.01, "slippage_buffer_usd": 0.005, "gas_cost_usd": 0.002,
        "net_profit_usd": 0.013, "would_execute": 1, "mode": "simulation",
        "executed": 0, "tx_hash": None,
    }
    signal.update(overrides)
    return signal


def test_arbitrage_signal_lifecycle(db: Database):
    signal_id = db.add_arbitrage_signal(_sample_signal())

    signals = db.list_arbitrage_signals(limit=10)
    assert len(signals) == 1
    assert signals[0]["token_symbol"] == "WETH"

    db.mark_arbitrage_signal_executed(
        signal_id, tx_hash_buy="0x123", tx_hash_sell="0x456",
        realized_pnl_usd=0.01, actual_profit_quote=0.02,
        actual_gas_cost_wei=100, actual_gas_cost_usd=0.01,
    )
    updated = db.fetch_one("SELECT * FROM arbitrage_signals WHERE id = ?", (signal_id,))
    assert updated["executed"] == 1
    assert updated["tx_hash_buy"] == "0x123"
    assert updated["tx_hash_sell"] == "0x456"
    assert updated["mode"] == "live"
    assert updated["realized_pnl_usd"] == 0.01
    assert updated["actual_profit_quote"] == 0.02
    assert updated["actual_gas_cost_wei"] == 100
    assert updated["actual_gas_cost_usd"] == 0.01
    db.mark_arbitrage_signal_executed(signal_id, tx_hash_buy="0x123", tx_hash_sell="0x456")
    updated_after_recovery = db.fetch_one("SELECT * FROM arbitrage_signals WHERE id = ?", (signal_id,))
    assert updated_after_recovery["realized_pnl_usd"] == 0.01
    assert updated_after_recovery["actual_gas_cost_wei"] == 100


def test_live_trading_stats_separate_attempts_from_success_and_realized_pnl(db: Database):
    pair_id = db.add_arbitrage_signal(_sample_signal())
    failed_pair_id = db.add_arbitrage_signal(_sample_signal())
    tri_id = db.add_triangular_signal(_sample_triangular_signal())

    db.mark_arbitrage_signal_executed(
        pair_id, tx_hash_buy="0xbuy", tx_hash_sell="0xsell",
        realized_pnl_usd=0.12, actual_profit_quote=0.13,
        actual_gas_cost_wei=100, actual_gas_cost_usd=0.01,
    )
    db.mark_arbitrage_signal_executed(
        failed_pair_id, tx_hash_buy="0xrevert", tx_hash_sell="0xrevert",
        execution_error="reverted", realized_pnl_usd=-0.02,
        actual_profit_quote=0.0, actual_gas_cost_wei=200, actual_gas_cost_usd=0.02,
    )
    db.mark_triangular_signal_executed(
        tri_id, tx_hash="0xtri", realized_pnl_usd=0.08,
        actual_profit_quote=0.1, actual_gas_cost_wei=100, actual_gas_cost_usd=0.02,
    )

    stats = db.live_trading_stats()
    assert stats["total_live"] == 3
    assert stats["completed"] == 2
    assert stats["open_count"] == 0
    assert stats["measured_trades"] == 3
    assert stats["realized_pnl_usd"] == pytest.approx(0.18)
    assert stats["actual_gas_cost_usd"] == pytest.approx(0.05)


def test_cross_dex_execution_history_counts_toward_live_stats_and_daily_limits(db: Database):
    signal_id = db.add_cross_dex_triangular_signal({
        "chain": "base",
        "token_a_symbol": "USDC", "token_a_address": "0x" + "1" * 40,
        "token_b_symbol": "WETH", "token_b_address": "0x" + "2" * 40,
        "token_c_symbol": "AERO", "token_c_address": "0x" + "3" * 40,
        "dex_ab": "sushiswap", "dex_bc": "baseswap", "dex_ca": "swapbased",
        "cycle_multiplier": 1.02, "spread_pct": 2.0, "min_liquidity_usd": 50_000,
        "token_b_usd_price": 2_000, "token_c_usd_price": 0.75,
    })
    db.mark_cross_dex_signal_executed(
        signal_id,
        executed=True,
        tx_hash="0xcross",
        execution_error=None,
        realized_pnl_usd=0.12,
        actual_profit_quote=0.13,
        actual_gas_cost_wei=100,
        actual_gas_cost_usd=0.01,
    )

    stats = db.live_trading_stats()
    assert stats["total_live"] == 1
    assert stats["completed"] == 1
    assert stats["measured_trades"] == 1
    assert stats["realized_pnl_usd"] == pytest.approx(0.12)
    assert stats["actual_gas_cost_usd"] == pytest.approx(0.01)
    assert db.today_executed_trades_count() == 1


def test_cross_dex_schema_migrates_existing_information_only_database(tmp_path: Path):
    import sqlite3

    path = tmp_path / "legacy-cross-dex.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE cross_dex_triangular_signals ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, detected_at REAL, chain TEXT NOT NULL, "
            "token_a_symbol TEXT NOT NULL, token_a_address TEXT NOT NULL, "
            "token_b_symbol TEXT NOT NULL, token_b_address TEXT NOT NULL, "
            "token_c_symbol TEXT NOT NULL, token_c_address TEXT NOT NULL, "
            "dex_ab TEXT NOT NULL, dex_bc TEXT NOT NULL, dex_ca TEXT NOT NULL, "
            "cycle_multiplier REAL NOT NULL, spread_pct REAL NOT NULL, min_liquidity_usd REAL)"
        )

    db = Database(path)
    columns = {
        row["name"] for row in db.fetch_all("PRAGMA table_info(cross_dex_triangular_signals)")
    }
    assert {
        "execution_status", "execution_attempted_at", "execution_tx_hash",
        "execution_error", "realized_pnl_usd", "actual_gas_cost_usd",
        "token_b_usd_price", "token_c_usd_price",
        "flashloan_notional_usd", "flashloan_net_profit_usd",
    }.issubset(columns)
    assert db.list_cross_dex_triangular_signals() == []


def test_arbitrage_stats_summary(db: Database):
    db.add_arbitrage_signal(_sample_signal(would_execute=1))
    db.add_arbitrage_signal(_sample_signal(would_execute=0, spread_pct=0.05))

    summary = db.arbitrage_stats_summary()
    assert summary["total"] == 2
    assert summary["would_execute_count"] == 1


def test_dashboard_stats_reset_preserves_history_and_only_counts_new_rows(db: Database):
    historical_id = db.add_arbitrage_signal(_sample_signal(would_execute=1))
    db.set_state("dashboard_stats_reset_at", "2000000000")

    reset_summary = db.arbitrage_stats_summary()
    assert reset_summary["total"] == 0
    assert reset_summary["would_execute_count"] == 0
    assert reset_summary["cumulative_net_profit_usd"] == 0
    triangular_summary = db.triangular_stats_summary()
    assert triangular_summary["total"] == 0
    assert triangular_summary["would_execute_count"] == 0
    assert triangular_summary["cumulative_net_profit_usd"] == 0
    assert db.fetch_one("SELECT id FROM arbitrage_signals WHERE id = ?", (historical_id,)) is not None

    new_id = db.add_arbitrage_signal(_sample_signal(would_execute=1))
    db.execute("UPDATE arbitrage_signals SET detected_at = 2000000001 WHERE id = ?", (new_id,))
    updated_summary = db.arbitrage_stats_summary()
    assert updated_summary["total"] == 1
    assert updated_summary["would_execute_count"] == 1


def test_pair_execution_history_filters_by_pair_and_orders_recent_first(db: Database):
    id1 = db.add_arbitrage_signal(_sample_signal(buy_dex="uniswap", sell_dex="aerodrome"))
    id2 = db.add_arbitrage_signal(_sample_signal(buy_dex="uniswap", sell_dex="aerodrome"))
    # Paire différente -> ne doit jamais apparaître dans l'historique ci-dessous.
    db.add_arbitrage_signal(_sample_signal(buy_dex="sushiswap", sell_dex="baseswap"))

    db.mark_arbitrage_signal_executed(id1, tx_hash_buy="0x1", tx_hash_sell="0x2")
    db.mark_arbitrage_signal_executed(id2, tx_hash_buy="0x3", execution_error="revert")

    history = db.pair_execution_history(token_symbol="WETH", buy_dex="uniswap", sell_dex="aerodrome", limit=20)
    assert len(history) == 2
    # Le plus récent (id2, en échec) doit être en premier.
    assert history[0]["execution_error"] == "revert"
    assert history[1]["tx_hash_sell"] == "0x2"


def test_pair_execution_history_empty_without_executed_attempts(db: Database):
    db.add_arbitrage_signal(_sample_signal(buy_dex="uniswap", sell_dex="aerodrome"))
    history = db.pair_execution_history(token_symbol="WETH", buy_dex="uniswap", sell_dex="aerodrome", limit=20)
    assert history == []


def test_daily_trade_limit_counts_broadcast_transactions_not_preflight_refusals(db: Database):
    start_of_day = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0,
    ).timestamp()
    preflight_pair = db.add_arbitrage_signal(_sample_signal())
    broadcast_pair = db.add_arbitrage_signal(_sample_signal())
    reverted_pair = db.add_arbitrage_signal(_sample_signal())
    preflight_triangle = db.add_triangular_signal(_sample_triangular_signal())
    broadcast_triangle = db.add_triangular_signal(_sample_triangular_signal())
    previous_day_pair = db.add_arbitrage_signal(_sample_signal())

    for signal_id in (preflight_pair, broadcast_pair, reverted_pair):
        db.execute(
            "UPDATE arbitrage_signals SET detected_at = ? WHERE id = ?",
            (start_of_day + 1, signal_id),
        )

    for signal_id in (preflight_triangle, broadcast_triangle):
        db.execute(
            "UPDATE triangular_signals SET detected_at = ? WHERE id = ?",
            (start_of_day + 1, signal_id),
        )
    db.execute(
        "UPDATE arbitrage_signals SET detected_at = ? WHERE id = ?",
        (start_of_day - 1, previous_day_pair),
    )

    db.mark_arbitrage_signal_executed(preflight_pair, execution_error="simulation refused")
    db.mark_arbitrage_signal_executed(
        broadcast_pair, tx_hash_buy="0xbuy", tx_hash_sell="0xsell",
    )
    db.mark_arbitrage_signal_executed(
        reverted_pair, tx_hash_buy="0xreverted", execution_error="reverted",
    )
    db.mark_triangular_signal_executed(preflight_triangle, execution_error="simulation refused")
    db.mark_triangular_signal_executed(
        broadcast_triangle, tx_hash="0xtri-reverted", execution_error="reverted",
    )
    db.mark_arbitrage_signal_executed(previous_day_pair, execution_error="simulation refused")

    assert db.today_executed_trades_count() == 3


def _sample_triangular_signal(**overrides) -> dict:
    signal = {
        "chain": "base", "dex": "aerodrome",
        "token_a_symbol": "USDC", "token_a_address": "0xusdc",
        "token_b_symbol": "WETH", "token_b_address": "0xweth",
        "token_c_symbol": "AERO", "token_c_address": "0xaero",
        "cycle_multiplier": 1.01, "spread_pct": 1.0, "min_liquidity_usd": 100000,
        "trade_size_usd": 3.0, "gross_profit_usd": 0.03, "fees_usd": 0.01,
        "slippage_buffer_usd": 0.005, "gas_cost_usd": 0.002, "net_profit_usd": 0.013,
        "would_execute": 1, "mode": "simulation", "executed": 0, "tx_hash": None,
    }
    signal.update(overrides)
    return signal


def test_triangular_execution_history_filters_by_cycle_and_orders_recent_first(db: Database):
    id1 = db.add_triangular_signal(_sample_triangular_signal())
    id2 = db.add_triangular_signal(_sample_triangular_signal())
    # Cycle différent (autre DEX) -> ne doit jamais apparaître ci-dessous.
    db.add_triangular_signal(_sample_triangular_signal(dex="sushiswap"))

    db.mark_triangular_signal_executed(id1, tx_hash="0x1")
    db.mark_triangular_signal_executed(id2, execution_error="revert")

    history = db.triangular_execution_history(
        dex="aerodrome", token_a_symbol="USDC", token_b_symbol="WETH", token_c_symbol="AERO", limit=20
    )
    assert len(history) == 2
    assert history[0]["execution_error"] == "revert"
    assert history[1]["tx_hash"] == "0x1"
