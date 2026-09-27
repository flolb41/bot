"""Interface commune à toutes les stratégies."""
from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum

import pandas as pd


class Signal(str, Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


class Strategy(ABC):
    #: nombre minimum de bougies nécessaires pour calculer un signal fiable
    min_candles: int = 50

    @abstractmethod
    def generate_signal(self, df: pd.DataFrame) -> Signal:
        """Reçoit un DataFrame OHLCV trié par date croissante et retourne un Signal."""
        raise NotImplementedError
