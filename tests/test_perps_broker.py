"""Tests des prérequis live perps : validation d'ordre, porte à trois verrous, brokers."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.perps import broker as br
from app.perps.models import LiveConfig, PairState, PerpsConfig

# Clé de test connue (compte Hardhat #0) : jamais utilisée ailleurs.
HARDHAT_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
HARDHAT_ADDRESS = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"


def _state(price=2000.0, market_open=True, min_notional=100.0, max_leverage=50.0):
    return PairState(
        symbol="ETH/USD", price=price, spread_pct=0.03, open_fee_pct=0.045, close_fee_pct=0.045, funding_long_pct_h=0.0012,
        funding_short_pct_h=-0.0012, rollover_pct_h=0.0, oi_long_usd=1e6, oi_short_usd=1e6, min_notional_usd=min_notional,
        max_leverage=max_leverage, market_open=market_open, observed_at=0.0,
    )


def test_validate_order_accepts_a_sane_order():
    live = LiveConfig(max_collateral_usd=60, max_total_collateral_usd=120, max_leverage=3)
    order = br.OrderRequest("ETH/USD", "long", 100, 2, take_profit_price=2060.0, stop_loss_price=1970.0)
    assert br.validate_order(order, _state(), live) == []
    assert order.collateral_usd == 50


def test_validate_order_lists_every_problem():
    live = LiveConfig(max_collateral_usd=60, max_total_collateral_usd=100, max_leverage=3)
    order = br.OrderRequest("ETH/USD", "long", 80, 4, take_profit_price=1900.0, stop_loss_price=2100.0, slippage_pct=9)
    reasons = br.validate_order(order, _state(market_open=False), live, open_collateral_usd=90)
    joined = " | ".join(reasons)
    for expected in ("fermé", "< minimum venue", "plafond configuré 3", "plafond global", "glissement", "stop du mauvais côté",
                     "objectif du mauvais côté"):
        assert expected in joined, joined


def test_validate_order_blocks_when_caps_are_zero_and_stop_beyond_liquidation():
    reasons = br.validate_order(br.OrderRequest("ETH/USD", "short", 100, 2), _state(), LiveConfig())
    assert any("max_collateral_usd non défini" in r for r in reasons)
    assert any("max_total_collateral_usd non défini" in r for r in reasons)
    live = LiveConfig(max_collateral_usd=100, max_total_collateral_usd=100, max_leverage=10)
    # Short 10x : liquidation vers +8,5 % ; un stop à +20 % ne protège rien.
    order = br.OrderRequest("ETH/USD", "short", 100, 10, stop_loss_price=2400.0)
    assert any("au-delà du prix de liquidation" in r for r in br.validate_order(order, _state(), live))
    # Stop à +5 % : sous la liquidation, accepté.
    assert br.validate_order(br.OrderRequest("ETH/USD", "short", 100, 10, stop_loss_price=2100.0), _state(), live) == []


def test_live_gate_is_closed_by_default_and_each_lock_is_independent():
    config = PerpsConfig.from_config({"perps": {"mode": "paper"}})
    checks = br.live_gate(config, env={})
    assert not br.gate_open(checks)
    by_name = {c.name: c for c in checks}
    assert set(by_name) == {"mode", "confirmation", "cle_deleguee", "plafonds", "coherence_strategies"}
    assert not by_name["mode"].ok and not by_name["confirmation"].ok and not by_name["plafonds"].ok
    assert "VERANTA_PRIVATE_KEY absente" in by_name["cle_deleguee"].detail

    live_cfg = {"perps": {"mode": "live", "live": {"max_collateral_usd": 100, "max_total_collateral_usd": 200, "max_leverage": 3},
                          "trend": {"notional_usd": 100, "leverage": 2}}}   # carry (défaut, 100 $ à 1x) + trend (50 $ à 2x)
    config = PerpsConfig.from_config(live_cfg)
    env = {br.LIVE_CONFIRMATION_ENV: br.LIVE_CONFIRMATION_PHRASE, "VERANTA_PRIVATE_KEY": HARDHAT_KEY,
           "VERANTA_TRADER_ADDRESS": "0x032d16Ec1F4382287cBBFbB87Dd8163934A01803"}
    assert br.gate_open(br.live_gate(config, env=env))

    # La clé du Pi qui EST le trader (peut retirer les fonds) ferme la porte.
    same = dict(env, VERANTA_TRADER_ADDRESS=HARDHAT_ADDRESS)
    assert "EST le trader" in {c.name: c for c in br.live_gate(config, env=same)}["cle_deleguee"].detail
    # Phrase approximative refusée.
    typo = dict(env, **{br.LIVE_CONFIRMATION_ENV: br.LIVE_CONFIRMATION_PHRASE.lower()})
    assert not {c.name: c for c in br.live_gate(config, env=typo)}["confirmation"].ok
    # Plafond par position < collatéral de la stratégie : incohérent.
    small = PerpsConfig.from_config({"perps": {"mode": "live", "live": {"max_collateral_usd": 20, "max_total_collateral_usd": 100},
                                               "trend": {"notional_usd": 100, "leverage": 2}}})
    assert not {c.name: c for c in br.live_gate(small, env=env)}["coherence_strategies"].ok
    # Le carry (activé par défaut) tient son short à 1x : il lui faut 100 $ de collatéral, pas 50.
    carry_only = PerpsConfig.from_config({"perps": {"mode": "live", "live": {"max_collateral_usd": 60, "max_total_collateral_usd": 200},
                                                    "funding_carry": {"notional_usd": 100}, "trend": {"enabled": False}}})
    coherence = {c.name: c for c in br.live_gate(carry_only, env=env)}["coherence_strategies"]
    assert not coherence.ok and "carry : collatéral 100.00 $ à 1x" in coherence.detail and "trend" not in coherence.detail
    # Aucune stratégie activée : rien à autoriser.
    none = PerpsConfig.from_config({"perps": {"mode": "live", "live": {"max_collateral_usd": 200, "max_total_collateral_usd": 200},
                                              "funding_carry": {"enabled": False}, "trend": {"enabled": False}}})
    assert not {c.name: c for c in br.live_gate(none, env=env)}["coherence_strategies"].ok
    # Sans adresse trader : la clé serait le wallet lui-même.
    assert "absente" in {c.name: c for c in br.live_gate(config, env={**env, "VERANTA_TRADER_ADDRESS": ""})}["cle_deleguee"].detail


def test_signer_address_derivation():
    assert br.signer_address(HARDHAT_KEY) == HARDHAT_ADDRESS
    assert br.signer_address("0xnotakey") is None


def test_paper_broker_is_inert():
    broker = br.PaperBroker()
    receipt = asyncio.run(broker.open(br.OrderRequest("ETH/USD", "long", 100, 2)))
    assert receipt["route"] == "paper" and receipt["accepted"]
    assert asyncio.run(broker.positions()) == [] and asyncio.run(broker.usdc_balance()) is None


def test_venue_position_from_sdk_uses_properties_then_raw_fallbacks():
    pos = SimpleNamespace(
        pair_index=0, index=3, buy=False, collateral=50.0, leverage=2.0, open_price=2000.0, tp=1940.0, sl=2030.0,
        liquidation_price=2800.0, rollover_fee=0.01, unrealised_funding_fee=-0.02, opened_at=1_700_000_000, base_symbol="ETH",
    )
    venue = br.venue_position_from_sdk(pos)
    assert venue.symbol == "ETH/USD" and venue.side == "short" and venue.trade_index == 3 and venue.notional_usd == 100.0
    assert venue.take_profit == 1940.0 and venue.stop_loss == 2030.0 and venue.funding_fee_usd == -0.02
    raw_only = SimpleNamespace(pair_index=1, index=0, buy=True, collateral_raw="25000000", leverage_raw="30000000000",
                               open_price_raw="700000000000000", tp_raw="0", sl_raw="0", liquidation_price_raw="0",
                               rollover_fee_raw="0", unrealised_funding_fee_raw="0", opened_at=0)
    venue = br.venue_position_from_sdk(raw_only, {1: "BTC/USD"})
    assert venue.symbol == "BTC/USD" and venue.collateral_usd == 25.0 and venue.leverage == 3.0 and venue.entry_price == 70000.0
    assert venue.take_profit is None and venue.stop_loss is None and venue.liquidation_price is None


def test_live_config_defaults_close_the_gate():
    live = LiveConfig.from_dict({"max_leverage": "abc", "slippage_pct": -1})
    assert live.max_collateral_usd == 0 and live.max_total_collateral_usd == 0
    assert live.max_leverage == 3.0 and live.slippage_pct == 1.0
    config = PerpsConfig.from_config({"perps": {"live": {"max_collateral_usd": 40, "max_total_collateral_usd": 80}}})
    assert config.live.max_collateral_usd == 40 and config.mode == "paper"
