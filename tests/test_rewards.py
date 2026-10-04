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


def test_live_earn_product_id_is_cached(db: Database):
    class FakeExchange:
        calls = 0

        class exchange:
            @staticmethod
            def sapi_get_simple_earn_flexible_list(_params):
                FakeExchange.calls += 1
                return {"rows": [{"productId": "prod-abc"}]}

    collector = RewardsCollector(db=db, mode="live", exchange=FakeExchange(), asset="USDT")

    assert collector._earn_product_id() == "prod-abc"
    assert collector._earn_product_id() == "prod-abc"
    assert FakeExchange.calls == 1


def test_live_sync_preserves_vault_when_balance_read_fails(db: Database):
    class FakeExchange:
        class exchange:
            @staticmethod
            def sapi_get_simple_earn_flexible_position(_params):
                raise RuntimeError("temporary API failure")

    db.set_vault("USDT", 125.0, 7.0)
    collector = RewardsCollector(db=db, mode="live", exchange=FakeExchange(), asset="USDT")

    collector._sync_vault_live()

    assert db.get_vault("USDT")[:2] == (125.0, 7.0)


def test_live_sync_skips_write_when_balance_is_unchanged(db: Database, monkeypatch):
    class FakeExchange:
        class exchange:
            @staticmethod
            def sapi_get_simple_earn_flexible_position(_params):
                return {"rows": [{"totalAmount": "125.0"}]}

    db.set_vault("USDT", 125.0, 7.0)
    collector = RewardsCollector(db=db, mode="live", exchange=FakeExchange(), asset="USDT")
    writes = []
    original_set_vault = db.set_vault

    def track_set_vault(asset, amount, total_rewards):
        writes.append((asset, amount, total_rewards))
        original_set_vault(asset, amount, total_rewards)

    monkeypatch.setattr(db, "set_vault", track_set_vault)

    collector._sync_vault_live()

    assert writes == []


def test_live_collect_cli_isolated_from_trading_mode(monkeypatch, capsys):
    import main
    import bot.rewards

    config = {
        "database": {"path": "unused.db"},
        "logging": {"level": "INFO", "file": "unused.log"},
        "trading": {"mode": "paper", "quote_currency": "USDT"},
        "exchange": {"name": "binance", "api_key": "test-key", "api_secret": "test-secret", "sandbox": True},
        "rewards": {"enabled": False},
    }
    calls = {}

    class FakeExchange:
        def __init__(self, **kwargs):
            calls["exchange_config"] = kwargs

        def fetch_balance(self, _asset):
            return 900.0

    class FakeCollector:
        def __init__(self, **kwargs):
            calls["collector_mode"] = kwargs["mode"]

        def validate_live_access(self):
            calls["validated"] = True

        def collect(self):
            calls["collected"] = True

        def subscribe_idle(self, balance):
            calls["free_balance"] = balance
            return 400.0

        def vault_balance(self):
            return 400.0

        def total_rewards(self):
            return 0.0

    monkeypatch.setattr(main, "load_config", lambda _path: config)
    monkeypatch.setattr(main, "setup_logger", lambda **_kwargs: None)
    monkeypatch.setattr(main, "Database", lambda _path: object())
    monkeypatch.setattr(main, "ExchangeClient", FakeExchange)
    monkeypatch.setattr(bot.rewards, "RewardsCollector", FakeCollector)
    monkeypatch.setenv("REWARDS_API_KEY", "real-key")
    monkeypatch.setenv("REWARDS_API_SECRET", "real-secret")

    main.cmd_rewards(type("Args", (), {"config": "config.yaml", "rewards_command": "collect", "live": True})())

    assert calls["collector_mode"] == "live"
    assert calls["exchange_config"]["api_key"] == "real-key"
    assert calls["exchange_config"]["api_secret"] == "real-secret"
    assert calls["exchange_config"]["sandbox"] is False
    assert calls["validated"] and calls["collected"]
    assert calls["free_balance"] == 900.0
    assert "Collecte live terminée" in capsys.readouterr().out


def test_live_collect_cli_respects_trading_sandbox_without_override(monkeypatch):
    import main

    config = {
        "database": {"path": "unused.db"},
        "logging": {"level": "INFO", "file": "unused.log"},
        "trading": {"mode": "live", "quote_currency": "USDT"},
        "exchange": {"name": "binance", "api_key": "", "api_secret": "", "sandbox": True},
        "rewards": {"enabled": True},
    }
    monkeypatch.setattr(main, "load_config", lambda _path: config)
    monkeypatch.setattr(main, "setup_logger", lambda **_kwargs: None)
    monkeypatch.setattr(main, "Database", lambda _path: object())

    with pytest.raises(SystemExit, match="mode sandbox du compte trading"):
        main.cmd_rewards(type("Args", (), {"config": "config.yaml", "rewards_command": "collect", "live": False})())
