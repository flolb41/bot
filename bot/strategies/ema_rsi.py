"""Stratégie EMA cross (rapide/lente) filtrée par RSI, long-only (achat spot).

Achat : la EMA rapide croise au-dessus de la EMA lente ET le RSI n'est pas en zone de surachat.
Vente : la EMA rapide croise en-dessous de la EMA lente ET le RSI n'est pas en zone de survente.
Les sorties sur stop-loss/take-profit sont gérées séparément par le RiskManager.
"""
from __future__ import annotations

import pandas as pd

from bot.indicators import ema, rsi
from bot.strategies.base import Signal, Strategy


class EmaRsiStrategy(Strategy):
    def __init__(
        self,
        ema_fast: int = 9,
        ema_slow: int = 21,
        rsi_period: int = 14,
        rsi_overbought: float = 70,
        rsi_oversold: float = 30,
        trend_ema: int = 0,
    ):
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.rsi_period = rsi_period
        self.rsi_overbought = rsi_overbought
        self.rsi_oversold = rsi_oversold
        # Filtre de régime : n'achète que si le prix est au-dessus de cette EMA longue (0 = désactivé)
        self.trend_ema = trend_ema
        self.min_candles = max(ema_slow, rsi_period, trend_ema) + 5

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        if len(df) < self.min_candles:
            return Signal.HOLD

        close = df["close"]
        fast = ema(close, self.ema_fast)
        slow = ema(close, self.ema_slow)
        r = rsi(close, self.rsi_period)

        prev_fast, prev_slow = fast.iloc[-2], slow.iloc[-2]
        curr_fast, curr_slow = fast.iloc[-1], slow.iloc[-1]
        curr_rsi = r.iloc[-1]

        crossed_up = prev_fast <= prev_slow and curr_fast > curr_slow
        crossed_down = prev_fast >= prev_slow and curr_fast < curr_slow

        uptrend = True
        if self.trend_ema > 0:
            uptrend = close.iloc[-1] > ema(close, self.trend_ema).iloc[-1]

        if crossed_up and uptrend and curr_rsi < self.rsi_overbought:
            return Signal.BUY
        if crossed_down and curr_rsi > self.rsi_oversold:
            return Signal.SELL
        return Signal.HOLD
