"""Estimation de valeur EUR — utilisée pour juger de la rentabilité d'un claim.

Utilise l'API publique CoinGecko (gratuite, sans clé). Principe de prudence :
si un token n'est pas coté (très fréquent pour un token de testnet/pré-TGE),
on retourne `None` plutôt qu'une valeur devinée — mieux vaut ne PAS réclamer
une récompense dont on ne peut pas vérifier la valeur que de risquer de payer
plus de gas que ce que ça rapporte (ou pire, d'interagir avec un contrat
frauduleux qui "offre" un faux token sans valeur).
"""
from __future__ import annotations

import logging
import os
import re
import time

import httpx

logger = logging.getLogger("app.wallet.pricing")

_CACHE: dict[str, tuple[float, float | None]] = {}
_CACHE_TTL_SECONDS = 600  # 10 min : évite de marteler l'API publique CoinGecko
_COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"


def _sanitize_env_suffix(network: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", network.upper()).strip("_")


def native_coingecko_id(network: str) -> str | None:
    """ID CoinGecko du token natif (gas) d'un réseau, via PRICE_NATIVE_<NETWORK>.

    Non configuré => prix inconnu => aucun calcul de rentabilité possible sur
    ce réseau (comportement volontairement conservateur).
    """
    raw = os.environ.get(f"PRICE_NATIVE_{_sanitize_env_suffix(network)}", "")
    return raw.strip() or None


def get_price_eur(coingecko_id: str | None) -> float | None:
    """Prix EUR d'un token via CoinGecko (cache 10 min). `None` si inconnu/échec."""
    if not coingecko_id:
        return None

    now = time.time()
    cached = _CACHE.get(coingecko_id)
    if cached and now - cached[0] < _CACHE_TTL_SECONDS:
        return cached[1]

    price: float | None = None
    try:
        resp = httpx.get(
            _COINGECKO_URL,
            params={"ids": coingecko_id, "vs_currencies": "eur"},
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()
        price = (data.get(coingecko_id) or {}).get("eur")
    except httpx.HTTPError as exc:
        logger.warning("Prix CoinGecko indisponible pour %s: %s", coingecko_id, exc)
        price = None

    _CACHE[coingecko_id] = (now, price)
    return price
