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
