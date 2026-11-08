"""Tests du raffinement on-chain de `app.trading.arbitrage` (sans RPC réel)."""
from __future__ import annotations

import logging

from app.trading import arbitrage, executor

BRETT = "0x532f27101965dd16442e59d40670faf5ebb142e4"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"


def _spread() -> dict:
    return {
        "buy_dex": "uniswap", "sell_dex": "pancakeswap",
        "buy_price_usd": 1.0, "sell_price_usd": 1.05, "spread_pct": 5.0,
        "buy_liquidity_usd": 50_000.0, "sell_liquidity_usd": 50_000.0,
    }


def test_shallow_pool_rejection_is_remembered_and_warned_once(monkeypatch, caplog):
    depth_reads = []
    monkeypatch.setattr(
        executor, "_onchain_quote_depth_usd",
        lambda dex, _token, _quote: depth_reads.append(dex) or 3.0,
    )

    with caplog.at_level(logging.DEBUG, logger="app.trading.arbitrage"):
        first = arbitrage._refine_spread_with_onchain_price(_spread(), BRETT, USDC, min_quote_depth_usd=25)
        second = arbitrage._refine_spread_with_onchain_price(_spread(), BRETT, USDC, min_quote_depth_usd=25)

    assert first["spread_pct"] == 0.0
    assert second["spread_pct"] == 0.0
    # La 2e évaluation est servie par la mémoire des rejets : aucune relecture RPC.
    assert depth_reads == ["uniswap"]
    warnings = [
        record.getMessage() for record in caplog.records
        if record.levelno == logging.WARNING and "trop peu profonde" in record.getMessage()
    ]
    assert len(warnings) == 1
    assert "pool ignorée pendant 10 min" in warnings[0]
    debug = [record.getMessage() for record in caplog.records if record.levelno == logging.DEBUG]
    assert any("déjà jugée trop peu profonde" in message for message in debug)


def test_shallow_pool_rejection_expires_after_ttl(monkeypatch):
    depth_reads = []
    monkeypatch.setattr(
        executor, "_onchain_quote_depth_usd",
        lambda dex, _token, _quote: depth_reads.append(dex) or 3.0,
    )
    clock = {"now": 1_000.0}
    monkeypatch.setattr(arbitrage.time, "monotonic", lambda: clock["now"])

    arbitrage._refine_spread_with_onchain_price(_spread(), BRETT, USDC, min_quote_depth_usd=25)
    clock["now"] += arbitrage._SHALLOW_POOL_TTL_SECONDS + 1
    arbitrage._refine_spread_with_onchain_price(_spread(), BRETT, USDC, min_quote_depth_usd=25)

    assert depth_reads == ["uniswap", "uniswap"]


def test_deep_enough_pool_is_not_remembered_as_shallow(monkeypatch):
    depths = {"uniswap": 500.0, "pancakeswap": 800.0}
    monkeypatch.setattr(executor, "_onchain_quote_depth_usd", lambda dex, _token, _quote: depths[dex])
    monkeypatch.setattr(executor, "_onchain_pool_price_usd", lambda *_args: None)

    refined = arbitrage._refine_spread_with_onchain_price(_spread(), BRETT, USDC, min_quote_depth_usd=25)

    assert refined["spread_pct"] == 5.0
    assert refined["buy_quote_depth_usd"] == 500.0
    assert arbitrage._shallow_pool_rejections == {}
