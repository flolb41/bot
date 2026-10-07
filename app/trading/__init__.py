"""Bot d'arbitrage inter-DEX (demande utilisateur : "trading bot avec microtrades").

Phase actuelle : SIMULATION UNIQUE — voir app/trading/arbitrage.py pour le détail
des garde-fous. Tant que `trading.enabled` (config.yaml / TRADING_ENABLED) n'est
pas explicitement passé à true, aucune transaction n'est jamais envoyée : le
scanner détecte les écarts de prix, calcule le profit net ESTIMÉ (frais DEX +
slippage + gas), et journalise ce qu'il aurait fait, pour valider la stratégie
avant d'y risquer le moindre centime.
"""
from __future__ import annotations

from app.trading.arbitrage import run_arbitrage_scan

__all__ = ["run_arbitrage_scan"]
