"""Stratégie de retour à la moyenne : Bandes de Bollinger + RSI, long-only.

Complémentaire à ema_rsi (suivi de tendance) : gagne dans les marchés en range,
là où les croisements d'EMA produisent des faux signaux.

Achat : la bougie précédente a clôturé SOUS la bande basse et la bougie courante
        re-clôture AU-DESSUS (rebond confirmé), avec RSI < rsi_oversold_max.
Vente : le prix atteint la moyenne mobile (bande médiane) — objectif naturel du retour
        à la moyenne — ou le RSI passe en surachat.
Les stop-loss / take-profit / trailing restent gérés par le RiskManager.
"""
from __future__ import annotations

import pandas as pd

from bot.indicators import bollinger, rsi
from bot.strategies.base import Signal, Strategy


class BollingerRsiStrategy(Strategy):
    def __init__(
        self,
        bb_period: int = 20,
        bb_std: float = 2.0,
        rsi_period: int = 14,
        rsi_oversold_max: float = 35,
        rsi_overbought: float = 70,
        exit_at: str = "middle",  # "middle" (moyenne) ou "upper" (bande haute, plus ambitieux)
    ):
        self.bb_period = bb_period
        self.bb_std = bb_std
        self.rsi_period = rsi_period
        self.rsi_oversold_max = rsi_oversold_max
        self.rsi_overbought = rsi_overbought
        self.exit_at = exit_at
        self.min_candles = max(bb_period, rsi_period) + 5

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        if len(df) < self.min_candles:
            return Signal.HOLD

        close = df["close"]
        lower, middle, upper = bollinger(close, self.bb_period, self.bb_std)
        r = rsi(close, self.rsi_period)

        prev_close, curr_close = close.iloc[-2], close.iloc[-1]
        prev_lower, curr_lower = lower.iloc[-2], lower.iloc[-1]
        curr_rsi = r.iloc[-1]

        if pd.isna(curr_lower) or pd.isna(prev_lower):
            return Signal.HOLD

        # Rebond confirmé : sortie sous la bande basse puis retour au-dessus
        bounced = prev_close < prev_lower and curr_close > curr_lower
        if bounced and curr_rsi < self.rsi_oversold_max:
            return Signal.BUY

        target = middle.iloc[-1] if self.exit_at == "middle" else upper.iloc[-1]
        if curr_close >= target or curr_rsi > self.rsi_overbought:
            return Signal.SELL
        return Signal.HOLD
