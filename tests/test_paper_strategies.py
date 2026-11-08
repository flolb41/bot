from app.database import Database
from app.trading import paper_strategies


TOKEN = "0x" + "d" * 40
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


def _groups(*, price: float, fdv: float = 20_000_000, liquidity: float = 150_000,
            volume: float = 600_000) -> dict:
    return {
        "pair": [{
            "base_address": TOKEN,
            "base_symbol": "TEST",
            "quote_address": USDC,
            "dex_id": "aerodrome",
            "price_usd_base": price,
            "liquidity_usd": liquidity,
            "volume_24h_usd": volume,
            "fdv_usd": fdv,
        }]
    }


def _config() -> dict:
    return {
        "paper_research": {
            "enabled": True,
            "roundtrip_gas_units": 300_000,
            "liquid_momentum": {
                "lookback_minutes": 15,
                "max_reference_age_minutes": 20,
                "entry_return_pct": 1,
                "max_entry_return_pct": 5,
                "take_profit_pct": 2,
                "stop_loss_pct": 1.5,
                "max_hold_minutes": 60,
                "stake_usd": 5,
                "min_liquidity_usd": 100_000,
                "min_volume_24h_usd": 500_000,
            },
            "microcap_momentum": {
                "lookback_minutes": 15,
                "max_reference_age_minutes": 20,
                "entry_return_pct": 1,
                "max_entry_return_pct": 5,
                "take_profit_pct": 2,
                "stop_loss_pct": 1.5,
                "max_hold_minutes": 60,
                "stake_usd": 5,
                "min_liquidity_usd": 25_000,
                "min_volume_24h_usd": 100_000,
                "max_fdv_usd": 50_000_000,
            },
        }
    }


def test_paper_momentum_records_net_results_without_live_trades(tmp_path, monkeypatch):
    db = Database(tmp_path / "paper.db")
    now = [1_000_000.0]
    monkeypatch.setattr(paper_strategies.time, "time", lambda: now[0])
    monkeypatch.setattr("app.wallet.pricing.get_price_usd", lambda _asset: 2_000.0)
    monkeypatch.setattr(paper_strategies.fees, "estimate_gas_cost_usd", lambda *_args, **_kwargs: 0.01)
    config = _config()

    paper_strategies.run_paper_experiments(db, config, _groups(price=1.0))
    now[0] += 16 * 60
    paper_strategies.run_paper_experiments(db, config, _groups(price=1.02))

    assert db.fetch_one("SELECT COUNT(*) AS n FROM paper_strategy_trades WHERE status='open'")["n"] == 2
    assert db.live_trading_stats()["total_live"] == 0

    now[0] += 60
    paper_strategies.run_paper_experiments(db, config, _groups(price=1.05))
    stats = {row["strategy"]: row for row in paper_strategies.paper_strategy_stats(db)}

    assert stats["liquid_momentum"]["closed_trades"] == 1
    assert stats["microcap_momentum"]["closed_trades"] == 1
    assert stats["liquid_momentum"]["realized_pnl_usd"] > 0
    assert stats["microcap_momentum"]["realized_pnl_usd"] > 0
    assert db.live_trading_stats()["total_live"] == 0


def test_microcap_experiment_requires_fdv_and_liquidity_filters(tmp_path, monkeypatch):
    db = Database(tmp_path / "paper.db")
    monkeypatch.setattr(paper_strategies.time, "time", lambda: 1_000_000.0)
    config = _config()

    paper_strategies.run_paper_experiments(
        db, config, _groups(price=1.0, fdv=60_000_000, liquidity=50_000, volume=120_000),
    )

    assert db.fetch_one(
        "SELECT COUNT(*) AS n FROM paper_market_observations WHERE strategy='microcap_momentum'"
    )["n"] == 0
    assert db.fetch_one(
        "SELECT COUNT(*) AS n FROM paper_market_observations WHERE strategy='liquid_momentum'"
    )["n"] == 0


def test_paper_experiments_can_be_disabled_without_creating_observations(tmp_path, monkeypatch):
    db = Database(tmp_path / "paper.db")
    monkeypatch.setattr(paper_strategies.time, "time", lambda: 1_000_000.0)

    paper_strategies.run_paper_experiments(
        db, {"paper_research": {"enabled": False}}, _groups(price=1.0),
    )

    assert db.fetch_one("SELECT COUNT(*) AS n FROM paper_market_observations")["n"] == 0


def test_concurrent_paper_trade_opening_is_ignored_instead_of_raising(tmp_path, monkeypatch, caplog):
    import logging

    db = Database(tmp_path / "paper-race.db")
    now = [1_000_000.0]
    monkeypatch.setattr(paper_strategies.time, "time", lambda: now[0])
    monkeypatch.setattr("app.wallet.pricing.get_price_usd", lambda _asset: 2_000.0)
    monkeypatch.setattr(paper_strategies.fees, "estimate_gas_cost_usd", lambda *_args, **_kwargs: 0.01)
    config = _config()
    paper_strategies.run_paper_experiments(db, config, _groups(price=1.0))
    now[0] += 16 * 60

    # Course entre deux cycles de scan : le trade est ouvert par l'autre cycle
    # ENTRE le test « déjà ouvert ? » et l'INSERT de celui-ci.
    original_fetch_one = Database.fetch_one

    def racing_fetch_one(self, query, params=()):
        if "FROM paper_strategy_trades" in query and "status='open'" in query:
            return None
        return original_fetch_one(self, query, params)

    monkeypatch.setattr(Database, "fetch_one", racing_fetch_one)
    paper_strategies.run_paper_experiments(db, config, _groups(price=1.02))
    with caplog.at_level(logging.INFO, logger="app.trading.paper_strategies"):
        paper_strategies.run_paper_experiments(db, config, _groups(price=1.02))
    monkeypatch.setattr(Database, "fetch_one", original_fetch_one)

    assert db.fetch_one(
        "SELECT COUNT(*) AS n FROM paper_strategy_trades WHERE strategy='liquid_momentum' AND status='open'"
    )["n"] == 1
    assert not any(record.levelno >= logging.ERROR for record in caplog.records)
    assert any("ouverture concurrente ignorée" in record.getMessage() for record in caplog.records)
