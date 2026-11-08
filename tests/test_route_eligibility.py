from app.trading.arbitrage import _is_classic_pair_route_executable
from app.trading.triangular import _is_classic_triangular_route_executable

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
BRETT = "0x532f27101965dd16442E59d40670FaF5eBB142E4"
UNSUPPORTED = "0x0000000000000000000000000000000000000001"


def test_pair_profitability_requires_a_supported_execution_route():
    assert _is_classic_pair_route_executable({
        "buy_dex": "aerodrome", "sell_dex": "uniswap", "quote_address": USDC,
    })
    assert not _is_classic_pair_route_executable({
        "buy_dex": "aerodrome", "sell_dex": "uniswap", "quote_address": UNSUPPORTED,
    })
    assert not _is_classic_pair_route_executable({
        "buy_dex": "unsupported", "sell_dex": "uniswap", "quote_address": USDC,
    })


def test_triangular_profitability_requires_supported_dex_and_start_token():
    assert _is_classic_triangular_route_executable({
        "dex": "sushiswap", "token_a_address": USDC,
    })
    assert not _is_classic_triangular_route_executable({
        "dex": "aerodrome", "token_a_address": USDC,
    })
    assert not _is_classic_triangular_route_executable({
        "dex": "aerodrome", "token_a_address": UNSUPPORTED,
    })
