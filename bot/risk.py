"""Gestion du risque : sizing, stop-loss / take-profit / trailing stop, limite de perte journalière."""
from __future__ import annotations

from dataclasses import dataclass

from bot.db import Database


@dataclass
class RiskParams:
    max_open_positions: int = 3
    stop_loss_pct: float = 2.0
    take_profit_pct: float = 4.0
    trailing_stop_pct: float = 1.5
    max_daily_loss_pct: float = 5.0


class RiskManager:
    def __init__(self, params: RiskParams, db: Database):
        self.params = params
        self.db = db

    def position_size(self, balance: float, price: float, allocation_pct: float) -> float:
        """Calcule la quantité à acheter en fonction du % du solde alloué au trade."""
        if price <= 0:
            return 0.0
        allocated_quote = balance * (allocation_pct / 100.0)
        return allocated_quote / price

    def can_open_new_position(self, open_positions_count: int, balance: float) -> bool:
        if open_positions_count >= self.params.max_open_positions:
            return False
        daily_pnl = self.db.get_daily_pnl()
        if balance > 0 and (daily_pnl / balance) * 100.0 <= -self.params.max_daily_loss_pct:
            return False
        return True

    def compute_stop_and_target(self, entry_price: float) -> tuple[float, float]:
        stop_loss = entry_price * (1 - self.params.stop_loss_pct / 100.0)
        take_profit = entry_price * (1 + self.params.take_profit_pct / 100.0)
        return stop_loss, take_profit

    def should_exit(self, entry_price: float, current_price: float, stop_loss: float, take_profit: float, trailing_stop: float | None) -> str | None:
        """Retourne la raison de sortie ('stop_loss', 'take_profit', 'trailing_stop') ou None."""
        if trailing_stop is not None and current_price <= trailing_stop:
            return "trailing_stop"
        if current_price <= stop_loss:
            return "stop_loss"
        if current_price >= take_profit:
            return "take_profit"
        return None

    def updated_trailing_stop(self, current_price: float, highest_price: float, existing_trailing_stop: float | None) -> float | None:
        if self.params.trailing_stop_pct <= 0:
            return existing_trailing_stop
        candidate = highest_price * (1 - self.params.trailing_stop_pct / 100.0)
        if existing_trailing_stop is None:
            return candidate
        return max(existing_trailing_stop, candidate)
