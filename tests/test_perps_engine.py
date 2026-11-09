"""Tests du moteur perps papier (app.perps.engine) — aucune dépendance au SDK."""
from __future__ import annotations

import pytest

from app.perps.engine import (
    STRATEGY_CARRY, STRATEGY_TREND, PaperEngine, gross_pnl_usd, liquidation_price,
)
from app.perps.models import PairState, PerpsConfig
from app.perps.storage import PerpsStore

T0 = 1_800_000_000.0
HOUR = 3600.0


def _state(symbol="ETH/USD", price=2000.0, *, funding_short=-0.0012, funding_long=0.0012, spread=0.01,
           open_fee=0.045, close_fee=0.045, rollover=0.0, min_notional=100.0, max_leverage=50.0,
           market_open=True, at=T0) -> PairState:
    return PairState(
        symbol=symbol, price=price, spread_pct=spread, open_fee_pct=open_fee, close_fee_pct=close_fee,
        funding_long_pct_h=funding_long, funding_short_pct_h=funding_short, rollover_pct_h=rollover,
        oi_long_usd=1_000_000, oi_short_usd=1_000_000, min_notional_usd=min_notional, max_leverage=max_leverage,
        market_open=market_open, observed_at=at,
    )


def _config(**overrides) -> PerpsConfig:
    raw = {
        "perps": {
            "enabled": True, "interval_minutes": 5, "markets": ["ETH/USD", "BTC/USD", "SOL/USD"],
            "funding_carry": {"markets": ["ETH/USD"], "notional_usd": 100, "entry_min_apr_pct": 6,
                              "entry_persistence_hours": 0, "entry_min_edge_ratio": 0,
                              "exit_min_apr_pct": 0, "exit_confirm_hours": 0.25, "max_hold_hours": 48,
                              "spot_cost_pct": 0.07, "spot_gas_usd": 0.02},
            "trend": {"markets": ["ETH/USD", "BTC/USD", "SOL/USD"], "notional_usd": 100, "leverage": 2,
                      "lookback_minutes": 240, "confirm_minutes": 60, "entry_pct": 1.0, "stop_loss_pct": 1.5,
                      "take_profit_pct": 3.0, "max_hold_hours": 24, "reentry_cooldown_minutes": 120,
                      "max_open_positions": 2, "daily_loss_limit_usd": 5},
        }
    }
    for section, values in overrides.items():
        raw["perps"][section].update(values)
    return PerpsConfig.from_config(raw)


@pytest.fixture
def engine(tmp_path):
    return PaperEngine(PerpsStore(tmp_path / "perps.db"), _config())


def _seed_trend_history(engine: PaperEngine, symbol: str, prices_by_hours_ago: dict[float, float], now: float) -> None:
    """Observations passées (sans entrées : marchés fermés) pour nourrir le signal."""
    for hours_ago, price in sorted(prices_by_hours_ago.items(), reverse=True):
        engine.store.record_observation(_state(symbol, price, market_open=False, at=now - hours_ago * HOUR))


# ---------------------------------------------------------------- helpers purs

def test_gross_pnl_sign_depends_on_side():
    assert gross_pnl_usd("long", 100.0, 110.0, 50.0) == pytest.approx(5.0)
    assert gross_pnl_usd("short", 100.0, 110.0, 50.0) == pytest.approx(-5.0)


def test_liquidation_price_mirrors_veranta_formula():
    # 2x : distance = prix × (collatéral × 85 % − frais) / notionnel = 42,5 % − frais.
    assert liquidation_price("long", 1000.0, 50.0, 100.0) == pytest.approx(1000 - 1000 * 42.5 / 100)
    assert liquidation_price("short", 1000.0, 50.0, 100.0) == pytest.approx(1000 + 1000 * 42.5 / 100)
    assert liquidation_price("long", 1000.0, 50.0, 100.0, fees_usd=0.5) == pytest.approx(1000 - 1000 * 42.0 / 100)


def test_execution_price_always_moves_against_the_trader():
    state = _state(price=1000.0, spread=0.1)
    assert state.execution_price("long", opening=True) == pytest.approx(1001.0)
    assert state.execution_price("long", opening=False) == pytest.approx(999.0)
    assert state.execution_price("short", opening=True) == pytest.approx(999.0)
    assert state.execution_price("short", opening=False) == pytest.approx(1001.0)


def test_funding_income_apr_uses_hourly_rate():
    state = _state(funding_short=-0.0012, funding_long=0.0012)
    assert state.funding_income_apr_pct("short") == pytest.approx(0.0012 * 24 * 365)
    assert state.funding_income_apr_pct("long") == pytest.approx(-0.0012 * 24 * 365)


# ---------------------------------------------------------------- carry

def test_carry_opens_short_perp_plus_modelled_spot_when_funding_income_is_high(engine):
    summary = engine.process_tick({"ETH/USD": _state()}, T0)

    positions = engine.store.open_positions(STRATEGY_CARRY)
    assert len(positions) == 1 and len(summary["actions"]) == 1
    position = positions[0]
    assert position["side"] == "short" and position["kind"] == "carry"
    assert position["notional_usd"] == 100.0
    assert position["entry_price"] == pytest.approx(2000 * (1 - 0.0001))   # vend avec le spread contre soi
    assert position["open_fee_usd"] == pytest.approx(0.045)
    assert position["spot_costs_usd"] == pytest.approx(0.07 + 0.02)
    assert position["spot_qty"] == pytest.approx((100 - 0.07) / 2000)
    assert position["collateral_usd"] == 200.0                            # capital employé : perp + spot


def test_carry_is_not_opened_when_funding_income_is_below_threshold(engine):
    engine.process_tick({"ETH/USD": _state(funding_short=-0.0005)}, T0)   # 4,4 %/an < 6 %
    assert engine.store.open_positions(STRATEGY_CARRY) == []


def test_carry_accrues_funding_income_then_closes_after_confirmed_low_funding(engine):
    engine.process_tick({"ETH/USD": _state()}, T0)
    engine.process_tick({"ETH/USD": _state(at=T0 + HOUR)}, T0 + HOUR)

    position = engine.store.open_positions(STRATEGY_CARRY)[0]
    assert position["funding_usd"] == pytest.approx(100 * -0.0012 / 100 * 1)  # revenu : négatif = encaissé

    # Le funding devient un coût pour le short : l'hystérésis (15 min ici) retient la
    # sortie tant que la condition n'a pas tenu assez longtemps, puis ferme.
    for i in range(1, 4):
        engine.process_tick({"ETH/USD": _state(funding_short=0.001, at=T0 + HOUR + i * 300)}, T0 + HOUR + i * 300)
        assert engine.store.open_positions(STRATEGY_CARRY)
    engine.process_tick({"ETH/USD": _state(funding_short=0.001, at=T0 + HOUR + 1200)}, T0 + HOUR + 1200)

    assert engine.store.open_positions(STRATEGY_CARRY) == []
    closed = engine.store.closed_positions(STRATEGY_CARRY)[0]
    assert closed["exit_reason"].startswith("funding_faible")
    # Prix inchangé : base nulle, P&L net = funding net − frais perp − coûts spot.
    funding_total = closed["funding_usd"]
    fees = closed["open_fee_usd"] + closed["close_fee_usd"] + closed["spot_costs_usd"]
    expected_perp_gross = gross_pnl_usd("short", closed["entry_price"], closed["exit_price"], 100.0)
    assert closed["gross_pnl_usd"] == pytest.approx(expected_perp_gross + closed["spot_qty"] * 0.0)
    assert closed["net_pnl_usd"] == pytest.approx(closed["gross_pnl_usd"] - funding_total - fees)
    assert closed["net_pnl_usd"] < 0  # une heure de funding ne couvre pas spread + frais


def test_carry_closes_after_max_hold(engine):
    engine.process_tick({"ETH/USD": _state()}, T0)
    engine.process_tick({"ETH/USD": _state(at=T0 + 49 * HOUR)}, T0 + 49 * HOUR)
    closed = engine.store.closed_positions(STRATEGY_CARRY)
    assert len(closed) == 1 and closed[0]["exit_reason"] == "duree_max"
    # 49 h de funding encaissé à 0,0012 %/h sur 100 $.
    assert closed[0]["funding_usd"] == pytest.approx(-100 * 0.0012 / 100 * 49)


def test_carry_waits_when_market_is_closed(engine):
    engine.process_tick({"ETH/USD": _state(market_open=False)}, T0)
    assert engine.store.open_positions(STRATEGY_CARRY) == []


# ---------------------------------------------------------------- trend

def test_trend_long_entry_needs_lookback_move_and_same_direction_confirmation(engine):
    now = T0 + 10 * HOUR
    _seed_trend_history(engine, "BTC/USD", {4.0: 2000.0, 1.0: 2010.0}, now)
    engine.process_tick({"BTC/USD": _state("BTC/USD", 2025.0, at=now)}, now)  # +1,25 % sur 4 h, dernière heure haussière

    positions = engine.store.open_positions(STRATEGY_TREND)
    assert len(positions) == 1
    position = positions[0]
    assert position["side"] == "long" and position["leverage"] == 2.0 and position["collateral_usd"] == 50.0
    assert position["entry_price"] == pytest.approx(2025 * 1.0001)
    assert position["liquidation_price"] == pytest.approx(
        liquidation_price("long", position["entry_price"], 50.0, 100.0, fees_usd=0.045)
    )


def test_trend_short_entry_on_negative_momentum(engine):
    now = T0 + 10 * HOUR
    _seed_trend_history(engine, "BTC/USD", {4.0: 2000.0, 1.0: 1990.0}, now)
    engine.process_tick({"BTC/USD": _state("BTC/USD", 1975.0, at=now)}, now)
    assert engine.store.open_positions(STRATEGY_TREND)[0]["side"] == "short"


def test_trend_rejects_signal_contradicted_by_last_hour(engine):
    now = T0 + 10 * HOUR
    _seed_trend_history(engine, "BTC/USD", {4.0: 2000.0, 1.0: 2040.0}, now)   # +1,25 % sur 4 h mais repli sur 1 h
    engine.process_tick({"BTC/USD": _state("BTC/USD", 2025.0, at=now)}, now)
    assert engine.store.open_positions(STRATEGY_TREND) == []


def test_trend_without_enough_history_does_nothing(engine):
    engine.process_tick({"BTC/USD": _state("BTC/USD", 2025.0)}, T0)
    assert engine.store.open_positions(STRATEGY_TREND) == []


def _open_trend_long(engine, now, price=2025.0):
    _seed_trend_history(engine, "BTC/USD", {4.0: 2000.0, 1.0: 2010.0}, now)
    engine.process_tick({"BTC/USD": _state("BTC/USD", price, at=now)}, now)
    return engine.store.open_positions(STRATEGY_TREND)[0]


def test_trend_stop_loss_closes_with_all_costs(engine):
    now = T0 + 10 * HOUR
    position = _open_trend_long(engine, now)
    later = now + 2 * HOUR
    engine.process_tick({"BTC/USD": _state("BTC/USD", 2025.0 * 0.983, at=later)}, later)   # −1,7 % < stop 1,5 %

    closed = engine.store.closed_positions(STRATEGY_TREND)[0]
    assert closed["exit_reason"] == "stop_loss"
    assert closed["exit_price"] == pytest.approx(2025.0 * 0.983 * (1 - 0.0001))
    gross = gross_pnl_usd("long", position["entry_price"], closed["exit_price"], 100.0)
    funding = 100 * 0.0012 / 100 * 2   # le long paie le funding pendant 2 h
    assert closed["funding_usd"] == pytest.approx(funding)
    assert closed["net_pnl_usd"] == pytest.approx(gross - 0.045 - 0.045 - funding)
    assert closed["net_pnl_usd"] < -1.5


def test_trend_take_profit(engine):
    now = T0 + 10 * HOUR
    _open_trend_long(engine, now)
    later = now + HOUR
    engine.process_tick({"BTC/USD": _state("BTC/USD", 2025.0 * 1.035, at=later)}, later)
    closed = engine.store.closed_positions(STRATEGY_TREND)[0]
    assert closed["exit_reason"] == "take_profit" and closed["net_pnl_usd"] > 3.0


def test_trend_max_hold(engine):
    now = T0 + 10 * HOUR
    _open_trend_long(engine, now)
    later = now + 25 * HOUR
    engine.process_tick({"BTC/USD": _state("BTC/USD", 2026.0, at=later)}, later)
    assert engine.store.closed_positions(STRATEGY_TREND)[0]["exit_reason"] == "duree_max"


def test_trend_liquidation_loses_the_whole_collateral(tmp_path):
    engine = PaperEngine(PerpsStore(tmp_path / "liq.db"), _config(trend={"stop_loss_pct": 90, "max_hold_hours": 1000}))
    now = T0 + 10 * HOUR
    position = _open_trend_long(engine, now)
    later = now + HOUR
    engine.process_tick({"BTC/USD": _state("BTC/USD", 2025.0 * 0.5, at=later)}, later)   # −50 % > distance de liquidation (~42 %)

    closed = engine.store.closed_positions(STRATEGY_TREND)[0]
    assert closed["exit_reason"] == "liquidation"
    assert closed["exit_price"] == pytest.approx(position["liquidation_price"], rel=1e-3)
    assert closed["net_pnl_usd"] == pytest.approx(-50.0)
    assert closed["close_fee_usd"] == 0.0


def test_trend_daily_loss_limit_blocks_new_entries(engine):
    now = T0 + 10 * HOUR
    engine.store.insert_position({
        "strategy": STRATEGY_TREND, "symbol": "SOL/USD", "side": "long", "kind": "perp", "opened_at": now - HOUR,
        "entry_price": 100.0, "index_price_at_open": 100.0, "notional_usd": 100.0, "leverage": 2.0,
        "collateral_usd": 50.0, "open_fee_usd": 0.045, "spot_costs_usd": 0.0, "funding_usd": 0.0, "rollover_usd": 0.0,
        "last_accrual_at": now - HOUR, "liquidation_price": 57.0, "entry_reason": "test",
    })
    engine.store.close_position(1, closed_at=now - 600, exit_price=94.0, close_fee_usd=0.045, exit_reason="stop_loss",
                                gross_pnl_usd=-6.0, net_pnl_usd=-6.09)
    _seed_trend_history(engine, "BTC/USD", {4.0: 2000.0, 1.0: 2010.0}, now)

    summary = engine.process_tick({"BTC/USD": _state("BTC/USD", 2025.0, at=now)}, now)

    assert engine.store.open_positions(STRATEGY_TREND) == []
    assert any("perte journalière" in action for action in summary["actions"])
    # Une seule notification par jour, même si le signal persiste.
    summary2 = engine.process_tick({"BTC/USD": _state("BTC/USD", 2030.0, at=now + 300)}, now + 300)
    assert summary2["actions"] == []


def test_trend_respects_reentry_cooldown_and_max_open_positions(tmp_path):
    engine = PaperEngine(PerpsStore(tmp_path / "limits.db"), _config(trend={"max_open_positions": 1}))
    now = T0 + 10 * HOUR
    for symbol in ("BTC/USD", "SOL/USD"):
        _seed_trend_history(engine, symbol, {4.0: 2000.0, 1.0: 2010.0}, now)
    engine.process_tick({"BTC/USD": _state("BTC/USD", 2025.0, at=now), "SOL/USD": _state("SOL/USD", 2025.0, at=now)}, now)
    assert [p["symbol"] for p in engine.store.open_positions(STRATEGY_TREND)] == ["BTC/USD"]

    # Stop sur BTC, puis signal BTC à nouveau valide 10 min plus tard : cooldown de 2 h.
    later = now + HOUR
    engine.process_tick({"BTC/USD": _state("BTC/USD", 2025.0 * 0.98, at=later)}, later)
    assert engine.store.closed_positions(STRATEGY_TREND)[0]["exit_reason"] == "stop_loss"
    retry = later + 600
    _seed_trend_history(engine, "BTC/USD", {4.0: 1900.0, 1.0: 1910.0}, retry)
    engine.process_tick({"BTC/USD": _state("BTC/USD", 1930.0, at=retry)}, retry)
    assert [p["symbol"] for p in engine.store.open_positions(STRATEGY_TREND)] == []


def test_notional_is_raised_to_the_venue_minimum(tmp_path):
    engine = PaperEngine(PerpsStore(tmp_path / "min.db"), _config(funding_carry={"notional_usd": 20}))
    engine.process_tick({"ETH/USD": _state(min_notional=100.0)}, T0)
    assert engine.store.open_positions(STRATEGY_CARRY)[0]["notional_usd"] == 100.0


def test_position_snapshot_values_open_positions(engine):
    engine.process_tick({"ETH/USD": _state()}, T0)
    position = engine.store.open_positions(STRATEGY_CARRY)[0]
    snapshot = engine.position_snapshot(position, _state(price=2000.0, at=T0 + HOUR), T0 + HOUR)
    assert snapshot["mark_price"] == 2000.0
    assert snapshot["hours_open"] == pytest.approx(1.0)
    assert snapshot["unrealized_net_usd"] < 0   # frais de clôture + coûts spot de sortie non encore couverts
    assert snapshot["funding_income_apr_pct"] == pytest.approx(0.0012 * 8760)


# ---------------------------------------------------------------- carry : économie

def test_carry_uses_net_apr_funding_minus_rollover(tmp_path):
    engine = PaperEngine(PerpsStore(tmp_path / "net.db"), _config())
    # AERO-like : funding +11 %/an encaissé mais rollover 15 %/an payé -> net négatif, pas d'entrée.
    state = _state(funding_short=-0.00125, rollover=0.0017)
    assert state.net_carry_apr_pct("short") == pytest.approx((0.00125 - 0.0017) * 8760)
    engine.process_tick({"ETH/USD": state}, T0)
    assert engine.store.open_positions(STRATEGY_CARRY) == []
    # ETH-like : rollover nul -> entrée.
    engine.process_tick({"ETH/USD": _state(rollover=0.0)}, T0 + 300)
    assert len(engine.store.open_positions(STRATEGY_CARRY)) == 1


def test_carry_requires_persistent_funding_before_entering(tmp_path):
    engine = PaperEngine(PerpsStore(tmp_path / "persist.db"), _config(funding_carry={"entry_persistence_hours": 1}))
    # Premier tick : pas d'historique -> pas d'entrée malgré 10,5 %/an.
    engine.process_tick({"ETH/USD": _state(at=T0)}, T0)
    assert engine.store.open_positions(STRATEGY_CARRY) == []
    # 55 min d'observations à funding élevé (fenêtre couverte à > 80 %) -> entrée au tick suivant.
    for minutes in range(5, 60, 5):
        engine.process_tick({"ETH/USD": _state(at=T0 + minutes * 60)}, T0 + minutes * 60)
    assert len(engine.store.open_positions(STRATEGY_CARRY)) == 1

    engine2 = PaperEngine(PerpsStore(tmp_path / "spike.db"), _config(funding_carry={"entry_persistence_hours": 1}))
    # Funding faible pendant 50 min puis pic sur les 10 dernières : médiane sous le seuil -> refus.
    for minutes in range(0, 50, 5):
        engine2.process_tick({"ETH/USD": _state(funding_short=-0.0002, at=T0 + minutes * 60)}, T0 + minutes * 60)
    for minutes in (50, 55, 60):
        engine2.process_tick({"ETH/USD": _state(funding_short=-0.005, at=T0 + minutes * 60)}, T0 + minutes * 60)
    assert engine2.store.open_positions(STRATEGY_CARRY) == []


def test_carry_requires_expected_income_to_cover_costs_with_margin(tmp_path):
    # 48 h de détention max à 10,5 %/an sur 100 $ = 0,058 $ attendus, pour 0,27 $ de rotation -> refus.
    engine = PaperEngine(PerpsStore(tmp_path / "edge.db"), _config(funding_carry={"entry_min_edge_ratio": 2, "max_hold_hours": 48}))
    engine.process_tick({"ETH/USD": _state()}, T0)
    assert engine.store.open_positions(STRATEGY_CARRY) == []
    # 30 jours : 0,86 $ attendus >= 2 × 0,27 $ -> entrée, avec l'amortissement dans le motif.
    engine = PaperEngine(PerpsStore(tmp_path / "edge30.db"), _config(funding_carry={"entry_min_edge_ratio": 2, "max_hold_hours": 720}))
    engine.process_tick({"ETH/USD": _state()}, T0)
    position = engine.store.open_positions(STRATEGY_CARRY)[0]
    assert "amortissement" in position["entry_reason"]
    costs = _config().carry.round_trip_costs_usd(100.0, 0.045, 0.045)
    assert costs == pytest.approx(0.09 + 2 * (0.07 + 0.02))


def test_carry_exit_hysteresis_resets_when_funding_recovers(engine):
    engine.process_tick({"ETH/USD": _state()}, T0)
    # 10 min de funding négatif, puis retour positif : le compteur repart de zéro.
    engine.process_tick({"ETH/USD": _state(funding_short=0.001, at=T0 + 300)}, T0 + 300)
    engine.process_tick({"ETH/USD": _state(funding_short=0.001, at=T0 + 600)}, T0 + 600)
    engine.process_tick({"ETH/USD": _state(at=T0 + 900)}, T0 + 900)
    engine.process_tick({"ETH/USD": _state(funding_short=0.001, at=T0 + 1200)}, T0 + 1200)
    engine.process_tick({"ETH/USD": _state(funding_short=0.001, at=T0 + 1500)}, T0 + 1500)
    assert len(engine.store.open_positions(STRATEGY_CARRY)) == 1          # 5 min < 15 min d'hystérésis
    engine.process_tick({"ETH/USD": _state(funding_short=0.001, at=T0 + 2400)}, T0 + 2400)
    assert engine.store.open_positions(STRATEGY_CARRY) == []
