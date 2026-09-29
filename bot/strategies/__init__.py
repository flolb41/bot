"""Package des stratégies de trading."""

from .base import Signal, Strategy
from .bollinger_rsi import BollingerRsiStrategy
from .ema_rsi import EmaRsiStrategy

STRATEGIES = {
    "ema_rsi": EmaRsiStrategy,
    "bollinger_rsi": BollingerRsiStrategy,
}


def get_strategy(name: str, params: dict) -> Strategy:
    if name not in STRATEGIES:
        raise ValueError(f"Stratégie inconnue: {name}. Disponibles: {list(STRATEGIES)}")
    return STRATEGIES[name](**params)


__all__ = ["Signal", "Strategy", "get_strategy"]
