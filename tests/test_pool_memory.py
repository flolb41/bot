"""Tests de la mémoire partagée des pools trop peu profondes."""
from __future__ import annotations

from app.trading import pool_memory


def test_pool_memory_is_symmetric_and_expires(monkeypatch):
    clock = {"now": 1_000.0}
    monkeypatch.setattr(pool_memory.time, "monotonic", lambda: clock["now"])

    pool_memory.remember_shallow_pool("uniswap", "0xAAA", "0xbbb", 3.15)
    assert pool_memory.recent_shallow_pool_depth("uniswap", "0xbbb", "0xaaa") == 3.15
    assert pool_memory.is_pool_recently_shallow("aerodrome", "0xaaa", "0xbbb") is False
    clock["now"] += pool_memory.SHALLOW_POOL_TTL_SECONDS + 1
    assert pool_memory.recent_shallow_pool_depth("uniswap", "0xaaa", "0xbbb") is None
    assert pool_memory._rejections == {}
