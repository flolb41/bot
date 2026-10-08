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

    db.mark_arbitrage_signal_executed(signal_id, tx_hash_buy="0x123", tx_hash_sell="0x456")
    updated = db.fetch_one("SELECT * FROM arbitrage_signals WHERE id = ?", (signal_id,))
    assert updated["executed"] == 1
    assert updated["tx_hash_buy"] == "0x123"
    assert updated["tx_hash_sell"] == "0x456"
    assert updated["mode"] == "live"


def test_arbitrage_stats_summary(db: Database):
    db.add_arbitrage_signal(_sample_signal(would_execute=1))
    db.add_arbitrage_signal(_sample_signal(would_execute=0, spread_pct=0.05))

    summary = db.arbitrage_stats_summary()
    assert summary["total"] == 2
    assert summary["would_execute_count"] == 1


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

