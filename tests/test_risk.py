import tempfile
from pathlib import Path

import pytest

from bot.db import Database
from bot.risk import RiskManager, RiskParams


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(str(tmp_path / "test.db"))


@pytest.fixture
def risk(db: Database) -> RiskManager:
    return RiskManager(
        RiskParams(max_open_positions=2, stop_loss_pct=2.0, take_profit_pct=4.0, trailing_stop_pct=1.5, max_daily_loss_pct=5.0),
        db,
    )


def test_position_size(risk: RiskManager):
    amount = risk.position_size(balance=1000, price=100, allocation_pct=10)
    assert amount == pytest.approx(1.0)


def test_position_size_zero_price(risk: RiskManager):
    assert risk.position_size(balance=1000, price=0, allocation_pct=10) == 0.0


def test_compute_stop_and_target(risk: RiskManager):
    stop_loss, take_profit = risk.compute_stop_and_target(100)
    assert stop_loss == pytest.approx(98.0)
    assert take_profit == pytest.approx(104.0)


def test_should_exit_stop_loss(risk: RiskManager):
    reason = risk.should_exit(entry_price=100, current_price=97, stop_loss=98, take_profit=104, trailing_stop=None)
    assert reason == "stop_loss"


def test_should_exit_take_profit(risk: RiskManager):
    reason = risk.should_exit(entry_price=100, current_price=105, stop_loss=98, take_profit=104, trailing_stop=None)
    assert reason == "take_profit"


def test_should_exit_none(risk: RiskManager):
    reason = risk.should_exit(entry_price=100, current_price=101, stop_loss=98, take_profit=104, trailing_stop=None)
    assert reason is None


def test_can_open_new_position_respects_max(risk: RiskManager):
    assert risk.can_open_new_position(open_positions_count=2, balance=1000) is False
    assert risk.can_open_new_position(open_positions_count=1, balance=1000) is True


def test_trailing_stop_only_increases(risk: RiskManager):
    trailing = risk.updated_trailing_stop(current_price=110, highest_price=110, existing_trailing_stop=105)
    assert trailing >= 105
    trailing2 = risk.updated_trailing_stop(current_price=90, highest_price=110, existing_trailing_stop=trailing)
    assert trailing2 == trailing  # ne redescend jamais
