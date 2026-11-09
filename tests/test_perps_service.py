"""Tests du service, du dashboard, du stockage et des modèles perps papier."""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.perps.dashboard import build_report, create_perps_app
from app.perps.engine import STRATEGY_CARRY, PaperEngine
from app.perps.market_data import pair_state_from_sdk
from app.perps.models import PairState, PerpsConfig
from app.perps.service import fetch_states, run_tick
from app.perps.storage import PerpsStore

T0 = 1_700_000_000.0  # passé : le health check doit juger le dernier tick « stale »


def _state(symbol: str, price: float, funding_short: float = -0.0012, at: float = T0) -> PairState:
    return PairState(
        symbol=symbol, price=price, spread_pct=0.01, open_fee_pct=0.045, close_fee_pct=0.045,
        funding_long_pct_h=0.0012, funding_short_pct_h=funding_short, rollover_pct_h=0.0,
        oi_long_usd=1e6, oi_short_usd=1e6, min_notional_usd=100.0, max_leverage=50.0, market_open=True, observed_at=at,
    )


class FakeProvider:
    def __init__(self, states: dict[str, PairState], failing: set[str] = frozenset()):
        self.states = states
        self.failing = set(failing)
        self.closed = False

    async def pair_state(self, symbol: str) -> PairState:
        if symbol in self.failing:
            raise RuntimeError("API indisponible")
        return self.states[symbol]

    async def close(self) -> None:
        self.closed = True


def _config() -> PerpsConfig:
    return PerpsConfig.from_config({"perps": {
        "enabled": True, "interval_minutes": 5, "markets": ["ETH/USD", "BTC/USD"],
        "funding_carry": {"markets": ["ETH/USD"], "entry_persistence_hours": 0}, "trend": {"markets": ["BTC/USD"]},
    }})


# ---------------------------------------------------------------- modèles

def test_perps_config_defaults_and_market_union():
    config = PerpsConfig.from_config({"perps": {"markets": ["ETH/USD"], "trend": {"markets": ["SOL/USD"], "leverage": "3"}}})
    assert config.enabled is False
    assert config.mode == "paper"
    assert config.markets == ("ETH/USD", "BTC/USD", "SOL/USD")   # + marchés des stratégies (carry par défaut : ETH, BTC)
    assert config.trend.leverage == 3.0
    assert config.carry.exit_confirm_hours == 12.0 and config.carry.entry_persistence_hours == 6.0
    assert PerpsConfig.from_config({}).enabled is False


def test_perps_config_rejects_invalid_numbers_with_defaults():
    config = PerpsConfig.from_config({"perps": {"interval_minutes": "abc", "trend": {"stop_loss_pct": -1, "max_open_positions": 0}}})
    assert config.interval_minutes == 5.0
    assert config.trend.stop_loss_pct == 1.5
    assert config.trend.max_open_positions == 3   # valeur invalide -> défaut


def test_pair_state_from_sdk_maps_veranta_objects():
    pair = SimpleNamespace(
        leverages=SimpleNamespace(min_leverage=1.0, max_leverage=50.0),
        funding_rate=SimpleNamespace(long=0.00120006, short=-0.0012000600929),
        margin_fee=SimpleNamespace(long=0.00057, short=0.00057),
        open_interest=SimpleNamespace(long=348167.52, short=357805.28),   # en $ ; coin_oi (164 ETH) ne doit pas être pris
        coin_oi=SimpleNamespace(long=164.389, short=164.389),
        spread_p=0.01, open_fee_p=0.045, close_fee_p=0.045, min_lev_pos_usdc=100.0, is_market_open=True,
    )
    state = pair_state_from_sdk("ETH/USD", pair, "2235.4995", 0.0125, observed_at=T0)

    assert state.price == pytest.approx(2235.4995)
    assert state.spread_pct == 0.0125                       # spread dynamique prioritaire
    assert state.funding_short_pct_h == pytest.approx(-0.0012000600929)
    assert state.rollover_pct_h == pytest.approx(0.00057)
    assert state.oi_long_usd == 348167.52 and state.oi_short_usd == 357805.28
    assert state.min_notional_usd == 100.0 and state.max_leverage == 50.0 and state.market_open is True
    assert pair_state_from_sdk("ETH/USD", pair, 1.0, None).spread_pct == 0.01   # repli sur le spread statique
    # Sans open_interest : coin_oi converti au prix.
    coins_only = SimpleNamespace(funding_rate=pair.funding_rate, coin_oi=SimpleNamespace(long=2.0, short=1.0))
    assert pair_state_from_sdk("ETH/USD", coins_only, 2000.0, None).oi_long_usd == 4000.0


# ---------------------------------------------------------------- stockage

def test_store_prunes_observations_and_ignores_duplicate_open_positions(tmp_path):
    store = PerpsStore(tmp_path / "store.db")
    store.record_observation(_state("ETH/USD", 1.0, at=T0 - 100))
    store.record_observation(_state("ETH/USD", 2.0, at=T0))
    assert store.prune_observations(T0 - 50) == 1
    assert [row["price"] for row in store.latest_observations()] == [2.0]

    position = {
        "strategy": STRATEGY_CARRY, "symbol": "ETH/USD", "side": "short", "kind": "carry", "opened_at": T0,
        "entry_price": 1.0, "index_price_at_open": 1.0, "notional_usd": 100.0, "leverage": 1.0, "collateral_usd": 200.0,
        "open_fee_usd": 0.0, "spot_costs_usd": 0.0, "funding_usd": 0.0, "rollover_usd": 0.0, "last_accrual_at": T0,
    }
    assert store.insert_position(position) == 1
    assert store.insert_position(position) is None
    assert store.strategy_stats(STRATEGY_CARRY)["open_positions"] == 1
    assert store.strategy_stats(STRATEGY_CARRY)["win_rate_pct"] is None


# ---------------------------------------------------------------- service

def test_fetch_states_isolates_failing_markets(caplog):
    provider = FakeProvider({"ETH/USD": _state("ETH/USD", 2000.0), "BTC/USD": _state("BTC/USD", 70000.0)}, failing={"BTC/USD"})
    with caplog.at_level(logging.WARNING, logger="app.perps.service"):
        states = asyncio.run(fetch_states(provider, ("ETH/USD", "BTC/USD")))
    assert list(states) == ["ETH/USD"]
    assert any("BTC/USD" in record.getMessage() for record in caplog.records)


def test_run_tick_records_observations_and_opens_paper_positions(tmp_path):
    config = _config()
    store = PerpsStore(tmp_path / "svc.db")
    engine = PaperEngine(store, config)
    provider = FakeProvider({"ETH/USD": _state("ETH/USD", 2000.0), "BTC/USD": _state("BTC/USD", 70000.0)})

    summary = asyncio.run(run_tick(engine, provider, config, now=T0))

    assert summary["markets"] == 2
    assert summary["open_positions"] == 1          # carry ETH ; pas d'historique pour la tendance BTC
    assert len(store.latest_observations()) == 2
    assert store.get_state("last_tick_at") == str(T0)


def test_run_tick_without_any_market_is_skipped(tmp_path):
    config = _config()
    store = PerpsStore(tmp_path / "empty.db")
    provider = FakeProvider({}, failing={"ETH/USD", "BTC/USD"})
    summary = asyncio.run(run_tick(PaperEngine(store, config), provider, config, now=T0))
    assert summary == {"actions": [], "markets": 0, "open_positions": None}
    assert store.get_state("last_tick_at") is None


# ---------------------------------------------------------------- dashboard

def test_dashboard_reports_markets_positions_and_health(tmp_path):
    config = _config()
    store = PerpsStore(tmp_path / "dash.db")
    engine = PaperEngine(store, config)
    engine.process_tick({"ETH/USD": _state("ETH/USD", 2000.0)}, T0)

    report = build_report(store, engine, config, now=T0 + 3600)
    assert report["mode"] == "paper"
    assert report["markets"][0]["funding_income_apr_short_pct"] == pytest.approx(0.0012 * 8760)
    assert report["open_positions"][0]["strategy"] == STRATEGY_CARRY
    assert report["open_positions"][0]["unrealized_net_usd"] is not None
    assert report["strategies"]["funding_carry"]["open_positions"] == 1

    client = TestClient(create_perps_app(store, engine, config))
    health = client.get("/api/health").json()
    assert health["mode"] == "paper" and health["open_positions"] == 1 and health["status"] == "stale"
    payload = client.get("/api/perps").json()
    assert payload["markets"][0]["symbol"] == "ETH/USD"
    html = client.get("/").text
    assert "aucune transaction réelle" in html and "funding_carry" in html
