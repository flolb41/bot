"""Mémoire courte des pools jugées trop peu profondes on-chain.

Partagée par le scan inter-DEX (`app.trading.arbitrage`), le scan triangulaire
cross-DEX (`app.trading.triangular`) et les exécuteurs (`app.trading.flashloan`,
`app.trading.executor`) : une pool dont la profondeur réelle est sous le
plancher est ignorée pendant `SHALLOW_POOL_TTL_SECONDS` par TOUS les chemins,
au lieu d'être relue (appels RPC) et re-refusée à chaque scan. Nuit du
08 au 09/11/2026 : 410 cycles BRETT « à +740 % » construits sur deux pools de
0,39 $ et 24,98 $ de profondeur ont été proposés puis refusés à l'exécution,
une fois par minute, pendant sept heures.

La clé est symétrique (une pool ne dépend pas du sens du swap)."""
from __future__ import annotations

import time

SHALLOW_POOL_TTL_SECONDS = 600.0

_rejections: dict[tuple[str, str, str], tuple[float, float]] = {}


def _key(dex: str, token_a: str, token_b: str) -> tuple[str, str, str]:
    first, second = sorted((token_a.lower(), token_b.lower()))
    return (dex, first, second)


def recent_shallow_pool_depth(dex: str, token_a: str, token_b: str) -> float | None:
    """Profondeur mesurée lors d'un rejet récent de cette pool, ou None si
    aucun rejet n'est en cours de validité."""
    key = _key(dex, token_a, token_b)
    entry = _rejections.get(key)
    if entry is None:
        return None
    expires_at, depth_usd = entry
    if time.monotonic() > expires_at:
        _rejections.pop(key, None)
        return None
    return depth_usd


def remember_shallow_pool(dex: str, token_a: str, token_b: str, depth_usd: float,
                          ttl_seconds: float = SHALLOW_POOL_TTL_SECONDS) -> None:
    _rejections[_key(dex, token_a, token_b)] = (time.monotonic() + ttl_seconds, depth_usd)


def is_pool_recently_shallow(dex: str, token_a: str, token_b: str) -> bool:
    return recent_shallow_pool_depth(dex, token_a, token_b) is not None


def clear() -> None:
    _rejections.clear()
