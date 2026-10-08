from pathlib import Path
from unittest.mock import patch

import pytest

from app.database import Database
from app.trading.triangular import run_cross_dex_triangular_scan

_USDC = "0x" + "1" * 40
_WETH = "0x" + "2" * 40
_AERO = "0x" + "3" * 40

_SEED_TOKENS = [
    {"symbol": "USDC", "address": _USDC},
    {"symbol": "WETH", "address": _WETH},
    {"symbol": "AERO", "address": _AERO},
]


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "test.db")


def _pair(dex_id: str, base_address: str, quote_address: str, price_native: float,
          liquidity_usd: float = 50_000) -> dict:
    return {
        "dex_id": dex_id, "base_address": base_address, "quote_address": quote_address,
        "price_native": price_native, "liquidity_usd": liquidity_usd,
    }


def _config(**overrides) -> dict:
    trading = {
        "chain": "base", "dex_whitelist": ["uniswap", "aerodrome", "sushiswap"],
        "seed_tokens": _SEED_TOKENS, "min_liquidity_usd": 10_000,
    }
    trading.update(overrides)
    return {"trading": trading}


def test_cross_dex_scan_detects_profitable_cycle_across_different_dexes(db: Database):
    # USDC->WETH la mieux cotée sur uniswap, WETH->AERO sur aerodrome,
    # AERO->USDC sur sushiswap : 3 DEX différents, jamais vu par
    # run_triangular_scan (une seule paire de pools par couple de tokens par
    # DEX dans ce groupe, donc le cycle ne peut QUE être cross-dex ici).
    groups = {
        "USDC/WETH": [_pair("uniswap", _USDC, _WETH, 2.0)],
        "WETH/AERO": [_pair("aerodrome", _WETH, _AERO, 3.0)],
        "AERO/USDC": [_pair("sushiswap", _AERO, _USDC, 0.2)],
    }
    with patch("app.trading.triangular._collect_pairs", return_value=groups):
        signals = run_cross_dex_triangular_scan(db, _config(cross_dex_min_spread_pct_to_log=0.1))

    assert len(signals) == 1
    signal = signals[0]
    assert signal["dex_ab"] == "uniswap"
    assert signal["dex_bc"] == "aerodrome"
    assert signal["dex_ca"] == "sushiswap"
    assert signal["cycle_multiplier"] == pytest.approx(2.0 * 3.0 * 0.2)
    # Persisté en base, consultable via list_cross_dex_triangular_signals.
    stored = db.list_cross_dex_triangular_signals(limit=10)
    assert len(stored) == 1
    assert stored[0]["dex_ab"] == "uniswap"


def test_cross_dex_scan_skips_cycle_fully_on_a_single_dex(db: Database):
    # Les 3 jambes sur le même DEX : déjà couvert par run_triangular_scan, le
    # scan cross-DEX ne doit pas le dupliquer.
    groups = {
        "USDC/WETH": [_pair("uniswap", _USDC, _WETH, 2.0)],
        "WETH/AERO": [_pair("uniswap", _WETH, _AERO, 3.0)],
        "AERO/USDC": [_pair("uniswap", _AERO, _USDC, 0.2)],
    }
    with patch("app.trading.triangular._collect_pairs", return_value=groups):
        signals = run_cross_dex_triangular_scan(db, _config(cross_dex_min_spread_pct_to_log=0.1))

    assert signals == []


def test_cross_dex_scan_picks_best_rate_per_leg_across_dexes(db: Database):
    # Deux pools concurrentes pour la jambe USDC->WETH : le scan doit retenir
    # la mieux cotée (celle qui rend le cycle le plus profitable).
    groups = {
        "USDC/WETH": [
            _pair("uniswap", _USDC, _WETH, 1.5),
            _pair("sushiswap", _USDC, _WETH, 2.0),  # meilleure
        ],
        "WETH/AERO": [_pair("aerodrome", _WETH, _AERO, 3.0)],
        "AERO/USDC": [_pair("baseswap", _AERO, _USDC, 0.2)],
    }
    with patch("app.trading.triangular._collect_pairs", return_value=groups):
        signals = run_cross_dex_triangular_scan(db, _config(cross_dex_min_spread_pct_to_log=0.1))

    # Les 2 sens de parcours du cycle (USDC->WETH->AERO->USDC et
    # USDC->AERO->WETH->USDC) sont tous deux profitables avec ces taux : on
    # vérifie seulement que la jambe USDC->WETH, quel que soit le sens où elle
    # apparaît, retient bien la meilleure pool (sushiswap, taux 2.0 > 1.5).
    forward = next(s for s in signals if s["token_b_symbol"] == "WETH")
    assert forward["dex_ab"] == "sushiswap"


def test_cross_dex_scan_respects_min_spread_threshold(db: Database):
    # Cycle à peine au-dessus de 1.0 (0.01% de spread) : sous le seuil
    # configuré (0.5%), ne doit pas être journalisé.
    groups = {
        "USDC/WETH": [_pair("uniswap", _USDC, _WETH, 2.0)],
        "WETH/AERO": [_pair("aerodrome", _WETH, _AERO, 3.0)],
        "AERO/USDC": [_pair("sushiswap", _AERO, _USDC, 1.0 / 6.0001)],
    }
    with patch("app.trading.triangular._collect_pairs", return_value=groups):
        signals = run_cross_dex_triangular_scan(db, _config(cross_dex_min_spread_pct_to_log=0.5))

    assert signals == []


def test_cross_dex_scan_returns_empty_without_dex_whitelist(db: Database):
    signals = run_cross_dex_triangular_scan(db, _config(dex_whitelist=[]))
    assert signals == []
