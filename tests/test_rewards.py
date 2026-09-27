import time
from pathlib import Path

import pytest

from bot.db import Database
from bot.rewards import RewardsCollector


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(str(tmp_path / "test.db"))


@pytest.fixture
def collector(db: Database) -> RewardsCollector:
    return RewardsCollector(
        db=db,
        mode="paper",
        asset="USDT",
        paper_apr_pct=10.0,
        profit_skim_pct=30.0,
        min_idle_to_subscribe=50.0,
        keep_free=100.0,
        min_trading_balance=50.0,
    )


def test_skim_profit(collector: RewardsCollector):
    skimmed = collector.skim_profit(10.0)
    assert skimmed == pytest.approx(3.0)
    assert collector.vault_balance() == pytest.approx(3.0)


def test_skim_ignores_losses(collector: RewardsCollector):
    assert collector.skim_profit(-5.0) == 0.0
    assert collector.vault_balance() == 0.0


def test_subscribe_idle_above_threshold(collector: RewardsCollector):
    subscribed = collector.subscribe_idle(free_balance=200.0)
    assert subscribed == pytest.approx(100.0)  # 200 - keep_free(100)
    assert collector.vault_balance() == pytest.approx(100.0)


def test_subscribe_idle_below_threshold(collector: RewardsCollector):
    assert collector.subscribe_idle(free_balance=120.0) == 0.0  # idle 20 < min 50


def test_refill_when_balance_low(collector: RewardsCollector, db: Database):
    db.set_vault("USDT", 500.0, 0.0)
    refilled = collector.refill_if_needed(free_balance=20.0)
    assert refilled == pytest.approx(80.0)  # remonte à keep_free(100)
    assert collector.vault_balance() == pytest.approx(420.0)


def test_refill_not_needed(collector: RewardsCollector, db: Database):
    db.set_vault("USDT", 500.0, 0.0)
    assert collector.refill_if_needed(free_balance=200.0) == 0.0


def test_refill_limited_by_vault(collector: RewardsCollector, db: Database):
    db.set_vault("USDT", 30.0, 0.0)
    refilled = collector.refill_if_needed(free_balance=20.0)
    assert refilled == pytest.approx(30.0)
    assert collector.vault_balance() == pytest.approx(0.0)


def test_paper_interest_accrual(collector: RewardsCollector, db: Database):
    # coffre de 1000 mis à jour il y a 365 jours -> ~10% d'APR simulé
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO vault (asset, amount, total_rewards, updated_at) VALUES (?, ?, ?, ?)",
            ("USDT", 1000.0, 0.0, time.time() - 365 * 86400),
        )
    collector.collect()
    assert collector.vault_balance() == pytest.approx(1100.0, rel=0.01)
    assert collector.total_rewards() == pytest.approx(100.0, rel=0.01)
