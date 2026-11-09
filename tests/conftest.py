"""Fixtures partagées pour la suite de tests."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _clear_executor_route_cache():
    """Vide le cache de résolution de route/pool de `app.trading.executor`
    avant chaque test. Ce cache (TTL court, voir `_ROUTE_CACHE_TTL_SECONDS`)
    évite des appels RPC redondants en production au sein d'un même cycle de
    scan, mais plusieurs tests réutilisent volontairement les mêmes adresses
    de tokens factices (USDC/WETH...) avec des mocks `w3` différents : sans
    ce nettoyage, un test pollue le cache pour les suivants (faux négatifs/
    positifs selon l'ordre d'exécution)."""
    from app.trading import executor
    executor._route_cache.clear()
    yield
    executor._route_cache.clear()


@pytest.fixture(autouse=True)
def _clear_shallow_pool_rejections():
    """Même principe pour la mémoire des pools écartées pour profondeur
    insuffisante (`app.trading.pool_memory`, partagée par les scans et les
    exécuteurs)."""
    from app.trading import pool_memory
    pool_memory.clear()
    yield
    pool_memory.clear()
