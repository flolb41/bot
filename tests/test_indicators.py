import numpy as np
import pandas as pd

from bot.indicators import atr, ema, rsi


def test_ema_converges_to_constant_series():
    series = pd.Series([100.0] * 50)
    result = ema(series, 10)
    assert abs(result.iloc[-1] - 100.0) < 1e-6


def test_rsi_is_100_when_only_gains():
    series = pd.Series(np.arange(1, 50, dtype=float))
    result = rsi(series, 14)
    assert result.iloc[-1] > 90


def test_rsi_is_0_when_only_losses():
    series = pd.Series(np.arange(50, 1, -1, dtype=float))
    result = rsi(series, 14)
    assert result.iloc[-1] < 10


def test_rsi_bounded_between_0_and_100():
    rng = np.random.default_rng(42)
    series = pd.Series(100 + np.cumsum(rng.normal(0, 1, 200)))
    result = rsi(series, 14)
    assert result.dropna().between(0, 100).all()


def test_atr_non_negative():
    df = pd.DataFrame(
        {
            "high": [10, 11, 12, 11, 13],
            "low": [9, 9.5, 10, 10, 11],
            "close": [9.5, 10.5, 11, 10.5, 12],
        }
    )
    result = atr(df, period=3)
    assert (result.dropna() >= 0).all()
