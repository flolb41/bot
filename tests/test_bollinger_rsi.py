import numpy as np
import pandas as pd

from bot.indicators import bollinger
from bot.strategies import Signal, get_strategy
from bot.strategies.bollinger_rsi import BollingerRsiStrategy


def _df(closes) -> pd.DataFrame:
    closes = np.asarray(closes, dtype=float)
    return pd.DataFrame({"close": closes, "high": closes + 1, "low": closes - 1, "open": closes, "volume": 1.0})


def test_bollinger_bands_order():
    rng = np.random.default_rng(1)
    s = pd.Series(100 + np.cumsum(rng.normal(0, 1, 100)))
    lower, middle, upper = bollinger(s, 20, 2.0)
    valid = ~lower.isna()
    assert (lower[valid] <= middle[valid]).all()
    assert (middle[valid] <= upper[valid]).all()


def test_registered():
    strat = get_strategy("bollinger_rsi", {"bb_period": 20})
    assert isinstance(strat, BollingerRsiStrategy)


def test_hold_when_not_enough_data():
    strat = BollingerRsiStrategy()
    assert strat.generate_signal(_df([100] * 10)) == Signal.HOLD


def test_buy_on_bounce_from_lower_band():
    # 40 bougies stables, chute brutale sous la bande basse, puis rebond au-dessus
    closes = [100.0] * 40 + [90.0, 96.0]
    strat = BollingerRsiStrategy(bb_period=20, bb_std=2.0, rsi_period=14, rsi_oversold_max=60)
    assert strat.generate_signal(_df(closes)) == Signal.BUY


def test_no_buy_without_bounce():
    # Chute sous la bande basse mais toujours en dessous : pas de rebond confirmé
    closes = [100.0] * 40 + [90.0, 89.0]
    strat = BollingerRsiStrategy(bb_period=20, rsi_oversold_max=60)
    assert strat.generate_signal(_df(closes)) != Signal.BUY


def test_sell_when_price_reaches_middle():
    # Prix au-dessus de la moyenne mobile -> signal de sortie
    closes = [100.0] * 40 + [90.0, 96.0, 101.0]
    strat = BollingerRsiStrategy(bb_period=20)
    assert strat.generate_signal(_df(closes)) == Signal.SELL


def test_sell_on_overbought_rsi():
    closes = list(np.linspace(100, 150, 60))  # hausse continue -> RSI > 70
    strat = BollingerRsiStrategy(bb_period=20, rsi_overbought=70)
    assert strat.generate_signal(_df(closes)) == Signal.SELL
