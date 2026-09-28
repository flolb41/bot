"""Cotation PancakeSwap V2 en lecture seule via JSON-RPC BSC (aucune clé, aucun gas).

Appelle `getAmountsOut` sur le router pour obtenir le prix DEX d'un actif en USDT,
puis compare avec le prix CEX. Encodage ABI fait à la main pour éviter web3.py.
"""
from __future__ import annotations

import logging

import requests

# keccak256("getAmountsOut(uint256,address[])")[:4]
_GET_AMOUNTS_OUT = "d06ca61f"

PANCAKE_V2_ROUTER = "0x10ED43C718714eb63d5aA57B78B54704E256024E"

# Tokens BSC (BEP-20) courants — tous en 18 décimales sur BSC
DEFAULT_TOKENS = {
    "USDT": "0x55d398326f99059fF775485246999027B3197955",
    "BTC": "0x7130d2A12B9BCbFAe4f2634d864A1Ee1Ce3Ead9c",  # BTCB
    "ETH": "0x2170Ed0880ac9A755fd29B2688956BD959F933F8",
    "BNB": "0xbb4CdB9CBd36B01bD1cBaEbF2De08d9173bc095c",  # WBNB
    "BUSD": "0xe9e7CEA3DedcA5984780Bafc599bD69ADd087D56",
}


class DexError(Exception):
    pass


def _pad_address(addr: str) -> str:
    return addr[2:].lower().zfill(64)


def _pad_uint(value: int) -> str:
    return hex(value)[2:].zfill(64)


def encode_get_amounts_out(amount_in: int, path: list[str]) -> str:
    """Encode l'appel getAmountsOut(uint256 amountIn, address[] path)."""
    head = _pad_uint(amount_in) + _pad_uint(0x40)  # offset du tableau dynamique
    body = _pad_uint(len(path)) + "".join(_pad_address(a) for a in path)
    return "0x" + _GET_AMOUNTS_OUT + head + body


def decode_uint_array(result_hex: str) -> list[int]:
    """Décode un retour ABI de type uint256[]."""
    data = result_hex[2:] if result_hex.startswith("0x") else result_hex
    if len(data) < 128:
        raise DexError("Réponse ABI trop courte")
    offset = int(data[0:64], 16) * 2
    length = int(data[offset : offset + 64], 16)
    values = []
    pos = offset + 64
    for _ in range(length):
        values.append(int(data[pos : pos + 64], 16))
        pos += 64
    return values


class PancakeQuoter:
    def __init__(
        self,
        rpc_url: str,
        router: str = PANCAKE_V2_ROUTER,
        tokens: dict[str, str] | None = None,
        decimals: dict[str, int] | None = None,
        quote_currency: str = "USDT",
    ):
        self.rpc_url = rpc_url
        self.router = router
        self.tokens = {**DEFAULT_TOKENS, **(tokens or {})}
        self.decimals = decimals or {}
        self.quote_currency = quote_currency
        self.logger = logging.getLogger("bot.dex")

    def _decimals(self, symbol: str) -> int:
        return int(self.decimals.get(symbol, 18))

    def _eth_call(self, data: str) -> str:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_call",
            "params": [{"to": self.router, "data": data}, "latest"],
        }
        resp = requests.post(self.rpc_url, json=payload, timeout=15)
        resp.raise_for_status()
        body = resp.json()
        if "error" in body:
            raise DexError(f"eth_call: {body['error']}")
        return body["result"]

    def amounts_out(self, amount_in: int, path: list[str]) -> list[int]:
        return decode_uint_array(self._eth_call(encode_get_amounts_out(amount_in, path)))

    def quote_price(self, symbol: str, notional: float = 100.0) -> float:
        """Prix DEX effectif pour acheter `notional` de quote (ex: 100 USDT) en base.

        Coter un montant réaliste plutôt que 1 unité entière évite un slippage artificiel
        sur les paires peu liquides. Essaie la paire directe et le routage via WBNB,
        et retient la meilleure exécution.
        """
        base, quote = symbol.split("/")
        if base not in self.tokens or quote not in self.tokens:
            raise DexError(f"Token inconnu pour {symbol}. Ajoute-le dans dex.tokens.")

        amount_in = int(notional * 10 ** self._decimals(quote))
        paths = [[self.tokens[quote], self.tokens[base]]]
        if base != "BNB" and quote != "BNB":
            paths.append([self.tokens[quote], self.tokens["BNB"], self.tokens[base]])

        best_out = 0
        last_error: Exception | None = None
        for path in paths:
            try:
                out = self.amounts_out(amount_in, path)
                if out and out[-1] > best_out:
                    best_out = out[-1]
            except Exception as exc:  # paire inexistante -> revert
                last_error = exc
        if best_out <= 0:
            raise DexError(f"Aucune route PancakeSwap pour {symbol}: {last_error}")
        base_received = best_out / 10 ** self._decimals(base)
        return notional / base_received

    @staticmethod
    def spread_pct(cex_price: float, dex_price: float) -> float:
        """Écart DEX vs CEX en % (positif = DEX plus cher que CEX)."""
        if cex_price <= 0:
            return 0.0
        return (dex_price - cex_price) / cex_price * 100.0
