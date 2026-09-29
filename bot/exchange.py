"""Wrapper autour de ccxt : données publiques + exécution d'ordres (mode live)."""
from __future__ import annotations

import logging
from typing import Any

import ccxt
import pandas as pd


class ExchangeClient:
    def __init__(self, name: str, api_key: str = "", api_secret: str = "", sandbox: bool = True):
        self.logger = logging.getLogger("bot.exchange")
        exchange_class = getattr(ccxt, name)
        self.exchange: ccxt.Exchange = exchange_class(
            {
                "apiKey": api_key or None,
                "secret": api_secret or None,
                "enableRateLimit": True,
            }
        )
        if sandbox:
            try:
                self.exchange.set_sandbox_mode(True)
            except Exception:
                self.logger.warning("Le sandbox/testnet n'est pas supporté par %s, mode réel utilisé pour les données publiques", name)

        self.exchange.load_markets()

    def fetch_ohlcv_df(self, symbol: str, timeframe: str, limit: int = 200) -> pd.DataFrame:
        raw = self.exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        return df

    def fetch_ticker_price(self, symbol: str) -> float:
        ticker = self.exchange.fetch_ticker(symbol)
        return float(ticker["last"])

    def fetch_balance(self, currency: str) -> float:
        balance = self.exchange.fetch_balance()
        return float(balance.get(currency, {}).get("free", 0.0))

    def create_market_order(self, symbol: str, side: str, amount: float) -> dict[str, Any]:
        """Passe un ordre marché réel. Utilisé uniquement en mode 'live'."""
        return self.exchange.create_order(symbol, "market", side, amount)

    def create_guarded_order(self, symbol: str, side: str, amount: float, ref_price: float, max_slippage_pct: float) -> dict[str, Any]:
        """Ordre limite IOC borné à ±max_slippage_pct du prix de référence.

        Exécute immédiatement ce qui est disponible dans la limite de prix, annule le reste :
        protège contre un carnet d'ordres vide (testnet) ou un pic de volatilité.
        """
        factor = 1 + max_slippage_pct / 100.0 if side == "buy" else 1 - max_slippage_pct / 100.0
        limit_price = float(self.exchange.price_to_precision(symbol, ref_price * factor))
        order = self.exchange.create_order(symbol, "limit", side, amount, limit_price, {"timeInForce": "IOC"})
        # Certains exchanges renvoient un ordre incomplet : on recharge pour avoir filled/average/fee
        if order.get("filled") is None or order.get("average") is None:
            try:
                order = self.exchange.fetch_order(order["id"], symbol)
            except Exception as exc:
                self.logger.debug("fetch_order impossible: %s", exc)
        return order

    def withdraw(self, currency: str, amount: float, address: str, network: str) -> dict[str, Any]:
        """Retire des fonds vers une adresse on-chain (nécessite le droit 'withdraw' sur la clé API)."""
        return self.exchange.withdraw(currency, amount, address, params={"network": network})

    def amount_to_precision(self, symbol: str, amount: float) -> float:
        return float(self.exchange.amount_to_precision(symbol, amount))
