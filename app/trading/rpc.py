"""Accès RPC Base résilient, partagé par tout `app.trading`.

Problème résolu (confirmé en production les 08-09/10/2026) : le bot dépendait
d'UN SEUL endpoint RPC (`RPC_BASE`), gratuit et rate-limité. Sous la charge
d'un cycle de scan (résolution de routes multi-DEX, prix on-chain, soldes),
cet endpoint répondait 429 en quasi-continu, et chaque 429 était interprété
ailleurs comme "RPC injoignable" ou — bien pire — comme "pool inexistante" /
"réserve nulle" (voir `executor._best_v3_style_pool` & co) : jusqu'à router
un flashloan vers une pool quasi vide (simulation à -62 %, bloquée par le
garde-fou `InsufficientProfit` du contrat, aucun fonds perdu, mais aucun
trade possible non plus).

Ce module fournit :
- `rpc_urls()` : liste d'endpoints (`RPC_BASE`, séparés par des virgules, puis
  des endpoints publics par défaut) ;
- `RotatingHTTPProvider` (via `make_w3`) : provider web3 qui bascule
  d'endpoint sur 429 / erreur réseau / erreur JSON-RPC de rate-limit, avec
  mise en quarantaine temporaire de l'endpoint fautif — chaque endpoint public
  ayant son propre quota, la capacité totale est ~N× celle d'un seul ;
- `call_with_rate_limit_retry` / `is_rate_limit_error` : retry applicatif
  (seconde ligne de défense, si TOUS les endpoints sont rate-limités) ;
- `is_connected_with_retry` : test de connexion basé sur `eth_chainId`
  (universellement supporté, contrairement à `web3_clientVersion` qu'utilise
  `w3.is_connected()`, et qui vérifie en plus qu'on parle bien à Base).
"""
from __future__ import annotations

import functools
import itertools
import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

BASE_CHAIN_ID = 8453

# Endpoints publics gratuits testés depuis le Pi le 08/10/2026 (20 requêtes
# en rafale chacun, 0 erreur) — l'endpoint Infura free tier précédemment
# configuré échouait 15/15 sur le même test.
DEFAULT_RPC_URLS: tuple[str, ...] = (
    "https://base-rpc.publicnode.com",
    "https://base.drpc.org",
    "https://mainnet.base.org",
)

# Retry applicatif (au-dessus de la rotation d'endpoints) : backoff
# exponentiel 2s/4s/8s/16s — n'est atteint que si TOUS les endpoints sont
# rate-limités simultanément.
_RATE_LIMIT_MAX_ATTEMPTS = 5
_RATE_LIMIT_RETRY_DELAY_SECONDS = 2.0

# Quarantaine d'un endpoint après un 429 (court : les fenêtres de rate-limit
# des endpoints publics sont de l'ordre de quelques secondes) ou après une
# erreur réseau / 5xx (plus long).
_RATE_LIMIT_COOLDOWN_SECONDS = 15.0
_FAILURE_COOLDOWN_SECONDS = 30.0

# Certains endpoints répondent HTTP 200 avec une erreur JSON-RPC de rate-limit
# plutôt qu'un HTTP 429 : marqueurs reconnus dans le corps (en minuscules).
_RATE_LIMIT_BODY_MARKERS = (b"rate limit", b"rate-limit", b"ratelimit", b"too many requests", b"-32005", b"-32016")


def rpc_urls() -> list[str]:
    """Endpoints RPC Base à utiliser, dans l'ordre de préférence : ceux de
    `RPC_BASE` (un ou plusieurs, séparés par des virgules) puis
    `DEFAULT_RPC_URLS`, sans doublon."""
    raw = os.environ.get("RPC_BASE") or ""
    urls: list[str] = []
    for candidate in [part.strip() for part in raw.split(",")] + list(DEFAULT_RPC_URLS):
        if candidate and candidate not in urls:
            urls.append(candidate)
    return urls


def is_rate_limit_error(exc: Exception) -> bool:
    """Détecte un 429 Too Many Requests (ou équivalent) renvoyé par le RPC,
    par opposition à un VRAI revert de simulation (manque de rentabilité,
    slippage, etc.) — seule l'erreur réseau transitoire doit être retentée."""
    message = str(exc)
    return "429" in message or "too many requests" in message.lower()


def call_with_rate_limit_retry(fn_call):
    """Exécute `fn_call` (ex. `fn.call(...)`, `fn.estimate_gas(...)`,
    `w3.eth.chain_id`) en retentant jusqu'à `_RATE_LIMIT_MAX_ATTEMPTS` fois
    avec un délai qui DOUBLE à chaque tentative UNIQUEMENT si l'échec est un
    rate-limit RPC (429) — toute autre exception (revert réel) est
    immédiatement propagée sans retry."""
    last_exc: Exception | None = None
    delay = _RATE_LIMIT_RETRY_DELAY_SECONDS
    for attempt in range(1, _RATE_LIMIT_MAX_ATTEMPTS + 1):
        try:
            return fn_call()
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if not is_rate_limit_error(exc) or attempt == _RATE_LIMIT_MAX_ATTEMPTS:
                raise
            logger.warning(
                "RPC rate-limited (429) sur tous les endpoints, nouvelle tentative %d/%d dans %.1fs : %s",
                attempt + 1, _RATE_LIMIT_MAX_ATTEMPTS, delay, exc,
            )
            time.sleep(delay)
            delay *= 2
    raise last_exc  # pragma: no cover - inatteignable (la boucle raise ou return toujours)


def _looks_rate_limited(raw: bytes) -> bool:
    if b'"error"' not in raw:
        return False
    lowered = raw.lower()
    return any(marker in lowered for marker in _RATE_LIMIT_BODY_MARKERS)


@functools.lru_cache(maxsize=1)
def _rotating_provider_class():
    # Import local : web3/requests sont des dépendances lourdes, chargées à la
    # demande (même convention que le reste de `app.trading`).
    import requests
    from web3.providers.rpc import HTTPProvider

    class RotatingHTTPProvider(HTTPProvider):
        """`HTTPProvider` web3 multi-endpoints : chaque requête JSON-RPC est
        envoyée au premier endpoint disponible ; sur 429 / 403 / 5xx / erreur
        réseau / erreur JSON-RPC de rate-limit, l'endpoint fautif est mis en
        quarantaine et la requête est rejouée immédiatement sur le suivant.
        L'ordre de départ tourne (round-robin) d'une instance à l'autre pour
        répartir la charge entre endpoints même en l'absence d'erreur.
        La quarantaine est partagée entre toutes les instances du processus."""

        _round_robin = itertools.count()
        _cooldown_until: dict[str, float] = {}
        _lock = threading.Lock()

        def __init__(self, urls, request_kwargs=None):
            urls = list(urls)
            if not urls:
                raise ValueError("Aucun endpoint RPC configuré (RPC_BASE vide et aucun défaut).")
            start = next(self._round_robin) % len(urls)
            self._urls = urls[start:] + urls[:start]
            super().__init__(endpoint_uri=self._urls[0], request_kwargs=request_kwargs)

        def _ordered_candidates(self) -> list[str]:
            now = time.monotonic()
            with self._lock:
                available = [u for u in self._urls if self._cooldown_until.get(u, 0.0) <= now]
                if available:
                    return available
                # Tous en quarantaine : on tente quand même, du moins récemment puni au plus récent.
                return sorted(self._urls, key=lambda u: self._cooldown_until.get(u, 0.0))

        def _quarantine(self, uri: str, seconds: float, reason: str) -> None:
            with self._lock:
                self._cooldown_until[uri] = time.monotonic() + seconds
            logger.warning("RPC %s mis en quarantaine %.0fs (%s) — bascule sur l'endpoint suivant.", uri, seconds, reason)

        def _make_request(self, method, request_data):  # noqa: ARG002 - signature imposée par web3
            last_exc: Exception | None = None
            for uri in self._ordered_candidates():
                try:
                    raw = self._request_session_manager.make_post_request(
                        uri, request_data, **self.get_request_kwargs()
                    )
                except requests.HTTPError as exc:
                    status = getattr(exc.response, "status_code", None)
                    if status == 429:
                        self._quarantine(uri, _RATE_LIMIT_COOLDOWN_SECONDS, "HTTP 429")
                    elif status == 403 or (status is not None and status >= 500):
                        self._quarantine(uri, _FAILURE_COOLDOWN_SECONDS, f"HTTP {status}")
                    else:
                        raise
                    last_exc = exc
                    continue
                except (requests.ConnectionError, requests.Timeout) as exc:
                    self._quarantine(uri, _FAILURE_COOLDOWN_SECONDS, type(exc).__name__)
                    last_exc = exc
                    continue
                if _looks_rate_limited(raw):
                    self._quarantine(uri, _RATE_LIMIT_COOLDOWN_SECONDS, "rate-limit JSON-RPC")
                    last_exc = requests.HTTPError(f"429 Too Many Requests (rate-limit JSON-RPC) for url: {uri}")
                    continue
                return raw
            assert last_exc is not None
            raise last_exc

    return RotatingHTTPProvider


def make_w3(timeout: float = 15.0):
    """Instance `Web3` standard du bot : `RotatingHTTPProvider` sur
    `rpc_urls()`. À utiliser PARTOUT à la place de
    `Web3(Web3.HTTPProvider(url, ...))` pour bénéficier de la rotation."""
    from web3 import Web3

    provider_class = _rotating_provider_class()
    return Web3(provider_class(rpc_urls(), request_kwargs={"timeout": timeout}))


def is_connected_with_retry(w3) -> bool:
    """Vrai si `w3` répond ET parle bien à Base (`eth_chainId == 8453`).
    Remplace `w3.is_connected()` : celui-ci interroge `web3_clientVersion`,
    méthode que certains endpoints publics n'exposent pas (faux négatif
    "RPC injoignable"), et ne vérifie pas la chaîne. Un échec réseau
    persistant (après rotation + retry) reste traité comme "injoignable"."""
    try:
        return int(call_with_rate_limit_retry(lambda: w3.eth.chain_id)) == BASE_CHAIN_ID
    except Exception as exc:  # noqa: BLE001
        logger.warning("RPC Base injoignable après rotation/retry : %s", exc)
        return False
