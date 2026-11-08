import sqlite3
import time

import pytest

from app.database import Database
from app.trading.directional_live import (
    _daily_loss_blocks_entries,
    _entry_candidates,
    _exit_reason,
    _position_token_amount,
    _token_amount_for_storage,
    _utc_day_start,
    run_live_directional_strategy,
)

TOKEN = "0x" + "d" * 40
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


def _groups(price: float) -> dict:
    return {
        "pairs": [{
            "base_address": TOKEN,
            "base_symbol": "TEST",
            "quote_address": USDC,
            "dex_id": "aerodrome",
            "price_usd_base": price,
            "liquidity_usd": 150_000,
            "volume_24h_usd": 600_000,
            "fdv_usd": 20_000_000,
        }]
    }


def _config() -> dict:
    return {
        "trading": {"seed_tokens": [{"symbol": "USDC", "address": USDC}]},
        "paper_research": {
            "liquid_momentum": {
                "lookback_minutes": 15,
                "max_reference_age_minutes": 20,
                "entry_return_pct": 1,
                "max_entry_return_pct": 5,
                "min_liquidity_usd": 100_000,
                "min_volume_24h_usd": 500_000,
            },
            "microcap_momentum": {
                "lookback_minutes": 15,
                "max_reference_age_minutes": 20,
                "entry_return_pct": 1,
                "max_entry_return_pct": 5,
                "min_liquidity_usd": 25_000,
                "min_volume_24h_usd": 100_000,
                "max_fdv_usd": 50_000_000,
            },
        },
    }


def test_live_momentum_requires_fresh_positive_lookback_signal(tmp_path):
    db = Database(tmp_path / "directional.db")
    config = _config()
    start = 1_000_000.0

    assert _entry_candidates(db, config, _groups(1.0), start) == []
    signals = _entry_candidates(db, config, _groups(1.02), start + 16 * 60)

    assert len(signals) == 2
    assert {item[1] for item in signals} == {"liquid_momentum", "microcap_momentum"}
    assert all(item[2]["token_address"] == TOKEN for item in signals)


def test_daily_loss_gate_blocks_unmeasured_or_limit_breaching_live_pnl(tmp_path):
    db = Database(tmp_path / "directional.db")
    now = 1_000_000.0
    start = _utc_day_start(now)
    db.execute(
        "INSERT INTO directional_live_trades "
        "(strategy, token_address, token_symbol, quote_address, dex, entry_at, entry_price_usd, "
        "stake_usd, token_amount, status, exit_at, net_pnl_usd) "
        "VALUES ('liquid_momentum', ?, 'TEST', ?, 'aerodrome', ?, 1, 2, 1, 'closed', ?, -1.01)",
        (TOKEN, USDC, now, now),
    )

    assert _daily_loss_blocks_entries(db, now, 1.0)

    db.execute("DELETE FROM directional_live_trades")
    db.execute(
        "INSERT INTO directional_live_trades "
        "(strategy, token_address, token_symbol, quote_address, dex, entry_at, entry_price_usd, "
        "stake_usd, token_amount, status, exit_at, net_pnl_usd) "
        "VALUES ('liquid_momentum', ?, 'TEST', ?, 'aerodrome', ?, 1, 2, 1, 'closed', ?, NULL)",
        (TOKEN, USDC, now, now),
    )
    assert _daily_loss_blocks_entries(db, now, 1.0)

    assert start <= now


def test_live_position_exit_rules_match_selected_paper_thresholds():
    position = {
        "entry_at": 1_000.0,
        "entry_price_usd": 1.0,
        "stake_usd": 2.0,
        "entry_gas_cost_usd": 0.01,
    }
    rules = {"take_profit_pct": 2, "stop_loss_pct": 1.5, "max_hold_minutes": 60}

    assert _exit_reason(position, 1.02, 1_100, rules, 0, 1) == "take_profit"
    assert _exit_reason(position, 0.985, 1_100, rules, 0, 1) == "stop_loss"
    assert _exit_reason(position, 1.0, 4_600, rules, 0, 1) == "max_hold"
    assert _exit_reason(position, 0.99, 1_100, rules, -0.98, 1) == "daily_loss_limit"
    assert _exit_reason(position, 1.0, 1_100, rules, 0, 1) is None


def test_live_directional_trades_stay_blocked_until_global_live_guardrails_pass(
    tmp_path, monkeypatch,
):
    db = Database(tmp_path / "directional.db")
    monkeypatch.setattr("app.trading.guardrails.is_trading_live", lambda _config: False)
    monkeypatch.setattr("app.trading.guardrails.is_stopped", lambda _db: False)

    run_live_directional_strategy(
        db, {"directional_live": {"enabled": True}},
        _groups(1.0),
    )

    assert db.fetch_one("SELECT COUNT(*) AS n FROM directional_live_trades")["n"] == 0


def test_one_open_position_and_daily_attempt_quota_are_persisted(tmp_path):
    db = Database(tmp_path / "directional.db")
    now = time.time()
    values = ("liquid_momentum", TOKEN, "TEST", USDC, "aerodrome", now, 1.0, 2.0, 0, "pending")
    db.execute(
        "INSERT INTO directional_live_trades "
        "(strategy, token_address, token_symbol, quote_address, dex, entry_at, entry_price_usd, "
        "stake_usd, token_amount, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        values,
    )

    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        db.execute(
            "INSERT INTO directional_live_trades "
            "(strategy, token_address, token_symbol, quote_address, dex, entry_at, entry_price_usd, "
            "stake_usd, token_amount, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            values,
        )

    db.execute(
        "UPDATE directional_live_trades SET attempt_tx_hash='0xentry' WHERE token_address=?",
        (TOKEN,),
    )
    assert db.today_executed_trades_count() == 1


def test_directional_stats_return_numeric_zeros_for_empty_history(tmp_path):
    db = Database(tmp_path / "directional.db")

    stats = db.directional_live_stats()

    assert stats == {
        "total": 0,
        "completed": 0,
        "open_count": 0,
        "realized_pnl_usd": 0,
        "unmeasured_closed": 0,
    }


def test_large_erc20_amount_round_trips_through_text_storage(tmp_path):
    db = Database(tmp_path / "directional.db")
    amount = 122_314_475_493_825_053_832
    trade_id = db.execute(
        "INSERT INTO directional_live_trades "
        "(strategy, token_address, token_symbol, quote_address, dex, entry_at, "
        "entry_price_usd, stake_usd, token_amount_raw, status) "
        "VALUES ('liquid_momentum', ?, 'TEST', ?, 'aerodrome', 1, 1, 2, ?, 'open')",
        (TOKEN, USDC, _token_amount_for_storage(amount)),
    )

    position = db.fetch_one(
        "SELECT token_amount_raw FROM directional_live_trades WHERE id=?",
        (trade_id,),
    )

    assert position["token_amount_raw"] == str(amount)
    assert int(position["token_amount_raw"]) == amount


def test_token_amount_storage_rejects_values_outside_uint256():
    with pytest.raises(ValueError, match="unsigned 256-bit"):
        _token_amount_for_storage(-1)
    with pytest.raises(ValueError, match="unsigned 256-bit"):
        _token_amount_for_storage(2**256)


def test_position_reads_exact_amount_and_supports_legacy_rows():
    amount = 122_314_475_493_825_053_832

    assert _position_token_amount({
        "token_amount": 0,
        "token_amount_raw": str(amount),
    }) == amount
    assert _position_token_amount({"token_amount": 123}) == 123


def test_directional_amount_column_migrates_existing_database(tmp_path):
    path = tmp_path / "directional.db"
    Database(path)
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE directional_live_trades DROP COLUMN token_amount_raw")

    migrated = Database(path)

    columns = {
        row["name"]
        for row in migrated.fetch_all("PRAGMA table_info(directional_live_trades)")
    }
    assert "token_amount_raw" in columns
