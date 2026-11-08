import pytest

from app.trading.fees import (
    actual_gas_cost_usd,
    is_net_profit_above_threshold,
    realized_net_profit_usd,
)


@pytest.mark.parametrize(
    ("profit", "threshold", "expected"),
    [
        (0.001, 0.0, True),
        (0.0, 0.0, False),
        (-0.001, 0.0, False),
        (0.01, 0.01, False),
        (0.011, 0.01, True),
        (None, 0.0, False),
        (0.001, -1.0, True),
    ],
)
def test_net_profit_threshold_requires_strictly_positive_profit(profit, threshold, expected):
    assert is_net_profit_above_threshold(profit, threshold) is expected


def test_actual_gas_cost_is_converted_from_receipt_wei():
    assert actual_gas_cost_usd(10**18, 2000.0) == 2000.0
    assert actual_gas_cost_usd(10**18, None) is None


def test_realized_profit_requires_both_quote_and_gas_prices():
    assert realized_net_profit_usd(0.5, 1.0, 0.1) == pytest.approx(0.4)
    assert realized_net_profit_usd(0.5, None, 0.1) is None
    assert realized_net_profit_usd(0.5, 1.0, None) is None
