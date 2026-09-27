"""Suivi du portefeuille en mode paper trading (solde virtuel persistant en base)."""
from __future__ import annotations

from bot.db import Database


class PaperPortfolio:
    """Gère un solde virtuel en mode simulation. En mode live, le solde vient de l'exchange."""

    def __init__(self, db: Database, starting_balance: float):
        self.db = db
        latest = db.get_latest_equity()
        self.balance: float = float(latest["balance"]) if latest is not None else starting_balance

    def apply_buy(self, cost: float) -> None:
        self.balance -= cost

    def apply_sell(self, proceeds: float) -> None:
        self.balance += proceeds

    def equity(self, open_positions_value: float) -> float:
        return self.balance + open_positions_value

    def snapshot(self, open_positions_value: float) -> None:
        self.db.record_equity(self.balance, self.equity(open_positions_value))
