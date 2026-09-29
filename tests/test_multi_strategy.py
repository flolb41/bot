import sqlite3
import time
from pathlib import Path

import pytest

from bot.config import Config
from bot.db import Database
from bot.engine import build_strategy_slots


def _base_config(**overrides) -> Config:
    data = {
        "trading": {"symbols": ["BTC/USDT", "ETH/USDT"], "timeframe": "1h", "allocation_pct": 10},
        "risk": {"max_open_positions": 3, "stop_loss_pct": 2, "take_profit_pct": 4, "trailing_stop_pct": 1.5, "max_daily_loss_pct": 5},
        "strategy": {"name": "ema_rsi", "params": {}},
    }
    data.update(overrides)
    return Config(data)


def test_positions_are_independent_per_strategy(tmp_path: Path):
    db = Database(str(tmp_path / "t.db"))
    db.open_position("BTC/USDT", "long", 100, 1, 98, 104, strategy="ema_rsi")
    db.open_position("BTC/USDT", "long", 101, 2, 99, 105, strategy="bollinger_rsi")

    assert len(db.get_all_positions()) == 2
    assert db.get_position("BTC/USDT", "ema_rsi")["amount"] == 1
    assert db.get_position("BTC/USDT", "bollinger_rsi")["amount"] == 2

    db.close_position("BTC/USDT", strategy="ema_rsi")
    assert db.get_position("BTC/USDT", "ema_rsi") is None
    assert db.get_position("BTC/USDT", "bollinger_rsi") is not None


def test_pnl_by_strategy(tmp_path: Path):
    db = Database(str(tmp_path / "t.db"))
    db.record_trade("BTC/USDT", "sell", 100, 1, 100, "paper", pnl=2.0, strategy="ema_rsi")
    db.record_trade("BTC/USDT", "sell", 100, 1, 100, "paper", pnl=-1.0, strategy="ema_rsi")
    db.record_trade("ETH/USDT", "sell", 100, 1, 100, "paper", pnl=0.5, strategy="bollinger_rsi")
    rows = {r["strategy"]: r for r in db.get_pnl_by_strategy()}
    assert rows["ema_rsi"]["n"] == 2 and rows["ema_rsi"]["wins"] == 1 and rows["ema_rsi"]["pnl"] == pytest.approx(1.0)
    assert rows["bollinger_rsi"]["n"] == 1 and rows["bollinger_rsi"]["pnl"] == pytest.approx(0.5)


def test_migration_from_legacy_schema(tmp_path: Path):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE trades (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp REAL, symbol TEXT, side TEXT,
            price REAL, amount REAL, cost REAL, fee REAL, pnl REAL, mode TEXT, reason TEXT);
        CREATE TABLE positions (symbol TEXT PRIMARY KEY, side TEXT, entry_price REAL, amount REAL,
            stop_loss REAL, take_profit REAL, trailing_stop REAL, opened_at REAL);
        INSERT INTO positions VALUES ('ETH/USDT', 'long', 2500, 0.01, 2450, 2600, NULL, 0);
        INSERT INTO trades (timestamp, symbol, side, price, amount, cost, fee, pnl, mode, reason)
            VALUES (0, 'ETH/USDT', 'buy', 2500, 0.01, 25, 0.025, 0, 'live', 'signal');
        """
    )
    conn.commit()
    conn.close()

    db = Database(str(path))
    pos = db.get_position("ETH/USDT", "ema_rsi")
    assert pos is not None and pos["entry_price"] == 2500
    assert db.get_recent_trades(1)[0]["strategy"] == "ema_rsi"
    # la migration doit être idempotente
    Database(str(path))


def test_build_slots_from_single_strategy(tmp_path: Path):
    db = Database(str(tmp_path / "t.db"))
    slots = build_strategy_slots(_base_config(), db)
    assert [s.name for s in slots] == ["ema_rsi"]
    assert slots[0].timeframe == "1h"
    assert slots[0].risk.params.trailing_stop_pct == 1.5


def test_build_slots_from_list_with_overrides(tmp_path: Path):
    db = Database(str(tmp_path / "t.db"))
    cfg = _base_config(
        strategies=[
            {"name": "ema_rsi", "params": {}},
            {"name": "bollinger_rsi", "params": {}, "risk": {"trailing_stop_pct": 0, "stop_loss_pct": 3},
             "timeframe": "4h", "symbols": ["BTC/USDT"], "allocation_pct": 5},
            {"name": "ema_rsi", "enabled": False},
        ]
    )
    slots = build_strategy_slots(cfg, db)
    assert [s.name for s in slots] == ["ema_rsi", "bollinger_rsi"]
    boll = slots[1]
    assert boll.risk.params.trailing_stop_pct == 0
    assert boll.risk.params.stop_loss_pct == 3
    assert boll.risk.params.take_profit_pct == 4  # hérité de risk:
    assert boll.timeframe == "4h"
    assert boll.symbols == ["BTC/USDT"]
    assert boll.allocation_pct == 5


def test_build_slots_rejects_duplicate_names(tmp_path: Path):
    db = Database(str(tmp_path / "t.db"))
    cfg = _base_config(strategies=[{"name": "ema_rsi"}, {"name": "ema_rsi"}])
    with pytest.raises(ValueError):
        build_strategy_slots(cfg, db)
