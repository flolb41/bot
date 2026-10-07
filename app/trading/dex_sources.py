"""Client HTTP pour l'API publique DEX Screener (gratuite, sans clé API).

Documentation observée en live (section 2025) :
    GET https://api.dexscreener.com/token-pairs/v1/{chainId}/{tokenAddress}
        -> liste de TOUTES les pools (tous DEX confondus) où le token est
           présent (en tant que base OU quote).

Conçu pour être tolérant aux pannes : toute erreur réseau/HTTP est journalisée
et retourne une liste vide plutôt que de lever une exception (même logique de
robustesse que app/discovery/sources.py).
"""
from __future__ import annotations

import logging
import time

import httpx

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.dexscreener.com"
_TIMEOUT = 15
_USER_AGENT = "Mozilla/5.0 (CryptoRewardHunter/1.0; +arbitrage scan, read-only)"

# DEX Screener documente une limite de 60 req/min sur certains endpoints ;
# on reste volontairement bien en-dessous (throttle conservateur), le Pi3
# n'a de toute façon pas besoin de scanner plus souvent que ça.
_MIN_INTERVAL_SECONDS = 1.1
_last_call_at = 0.0


def _throttle() -> None:
    global _last_call_at
    elapsed = time.monotonic() - _last_call_at
    if elapsed < _MIN_INTERVAL_SECONDS:
        time.sleep(_MIN_INTERVAL_SECONDS - elapsed)
    _last_call_at = time.monotonic()


def fetch_token_pairs(chain_id: str, token_address: str) -> list[dict]:
    """Retourne toutes les pools DEX Screener connues pour `token_address` sur
    `chain_id` (ex: "base"). Liste vide si indisponible/erreur (tolérant aux pannes)."""
    _throttle()
    url = f"{_BASE_URL}/token-pairs/v1/{chain_id}/{token_address}"
    try:
        resp = httpx.get(url, timeout=_TIMEOUT, headers={"User-Agent": _USER_AGENT})
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as exc:
        logger.warning("DEX Screener indisponible pour %s/%s : %s", chain_id, token_address, exc)
        return []
    except ValueError as exc:  # JSON invalide
        logger.warning("DEX Screener réponse invalide pour %s/%s : %s", chain_id, token_address, exc)
        return []

    if not isinstance(data, list):
        return []
    return data
