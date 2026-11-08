from app.trading.arbitrage import _collect_pairs
from app.trading.flashloan import pair_signal_is_flashloan_eligible
from app.trading.token_risk import (
    notional_under_slippage,
    pool_liquidity_floor,
    quote_depth_floor,
    token_flashloan_allowed,
    token_slippage_pct,
    token_trade_cap,
)

BRETT = "0x532f27101965dd16442e59d40670faf5ebb142e4"
WETH = "0x4200000000000000000000000000000000000006"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
AERO = "0x940181a94a35a4569e4529a3cdfb74e38fd98631"


def _config():
    return {
        "trading": {
            "min_liquidity_usd": 10_000,
            "flashloan_enabled": True,
            "low_liquidity_token_overrides": {
                "brett": {
                    "address": BRETT,
                    "quote_addresses": [WETH, USDC],
                    "min_pool_liquidity_usd": 25,
                    "min_quote_depth_usd": 25,
                    "max_trade_size_usd": 2,
                    "max_slippage_pct": 15,
                    "flashloan_enabled": True,
                },
            },
        },
    }


def test_low_liquidity_exception_is_scoped_to_brett_quote_pairs():
    config = _config()

    assert pool_liquidity_floor(config, BRETT, WETH, 10_000) == 25
    assert pool_liquidity_floor(config, BRETT, WETH, 10) == 25
    assert pool_liquidity_floor(config, WETH, BRETT, 10_000) == 25
    assert pool_liquidity_floor(config, BRETT, AERO, 10_000) == 10_000
    assert pool_liquidity_floor(config, WETH, USDC, 10_000) == 10_000
    assert quote_depth_floor(config, BRETT, WETH, 10_000) == 25


def test_brett_override_caps_size_and_slippage_and_allows_flashloan():
    config = _config()

    assert token_trade_cap(config, BRETT, WETH, 3) == 2
    assert token_trade_cap(config, WETH, BRETT, 3) == 2
    assert token_slippage_pct(config, BRETT, WETH) == 15
    assert token_slippage_pct(config, BRETT, AERO) is None
    assert token_flashloan_allowed(config, BRETT, WETH) is True
    signal = {
        "token_address": BRETT,
        "quote_address": WETH,
        "buy_dex": "aerodrome",
        "sell_dex": "sushiswap",
    }
    assert pair_signal_is_flashloan_eligible(config, signal) is True
    config["trading"]["low_liquidity_token_overrides"]["brett"]["flashloan_enabled"] = False
    assert token_flashloan_allowed(config, BRETT, WETH) is False
    assert pair_signal_is_flashloan_eligible(config, signal) is False


def test_trade_notional_is_bounded_by_configured_slippage():
    assert round(notional_under_slippage(25, 15), 4) == 2.2059
    assert notional_under_slippage(0, 15) == 0


def test_low_liquidity_exception_only_collects_approved_brett_quotes(monkeypatch):
    raw_pairs = [
        {
            "chainId": "base",
            "dexId": "uniswap",
            "baseToken": {"address": BRETT, "symbol": "BRETT"},
            "quoteToken": {"address": WETH, "symbol": "WETH"},
            "priceUsd": "0.05",
            "priceNative": "0.00002",
            "liquidity": {"usd": 100},
        },
        {
            "chainId": "base",
            "dexId": "uniswap",
            "baseToken": {"address": WETH, "symbol": "WETH"},
            "quoteToken": {"address": USDC, "symbol": "USDC"},
            "priceUsd": "3000",
            "priceNative": "3000",
            "liquidity": {"usd": 100},
        },
        {
            "chainId": "base",
            "dexId": "aerodrome",
            "baseToken": {"address": BRETT, "symbol": "BRETT"},
            "quoteToken": {"address": AERO, "symbol": "AERO"},
            "priceUsd": "0.05",
            "priceNative": "0.00002",
            "liquidity": {"usd": 100},
        },
    ]
    monkeypatch.setattr("app.trading.dex_sources.fetch_token_pairs", lambda *_args: raw_pairs)

    groups = _collect_pairs(
        "base",
        [{"address": WETH}, {"address": USDC}],
        {"uniswap", "aerodrome"},
        10_000,
        _config(),
    )

    brett_weth_key = "_".join(sorted((BRETT.lower(), WETH.lower())))
    brett_aero_key = "_".join(sorted((BRETT.lower(), AERO.lower())))
    weth_usdc_key = "_".join(sorted((WETH.lower(), USDC.lower())))
    assert brett_weth_key in groups
    assert brett_aero_key not in groups
    assert weth_usdc_key not in groups
