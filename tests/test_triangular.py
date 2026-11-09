from pathlib import Path
from unittest.mock import patch

import pytest

from app.database import Database
from app.trading.triangular import _is_classic_triangular_route_executable, run_cross_dex_triangular_scan

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


def test_classic_triangular_execution_only_allows_matching_v2_routes():
    executable_usdc = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    assert _is_classic_triangular_route_executable({"dex": "sushiswap", "token_a_address": executable_usdc}) is True
    assert _is_classic_triangular_route_executable({"dex": "aerodrome", "token_a_address": executable_usdc}) is False
    assert _is_classic_triangular_route_executable({"dex": "pancakeswap", "token_a_address": executable_usdc}) is False


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
    with (
        patch("app.trading.triangular._collect_pairs", return_value=groups),
        patch("app.trading.triangular._quote_token_usd_price", return_value=1.0),
    ):
        # Cycle synthétique à +20 % : plafond de plausibilité relevé pour le test.
        signals = run_cross_dex_triangular_scan(
            db, _config(cross_dex_min_spread_pct_to_log=0.1, cross_dex_max_plausible_spread_pct=100),
        )

    assert len(signals) == 1
    signal = signals[0]
    assert signal["dex_ab"] == "uniswap"
    assert signal["dex_bc"] == "aerodrome"
    assert signal["dex_ca"] == "sushiswap"
    assert signal["cycle_multiplier"] == pytest.approx(2.0 * 3.0 * 0.2)
    assert signal["token_b_usd_price"] == pytest.approx(0.5)
    assert signal["token_c_usd_price"] == pytest.approx(0.2)
    # Persisté en base, consultable via list_cross_dex_triangular_signals.
    stored = db.list_cross_dex_triangular_signals(limit=10)
    assert len(stored) == 1
    assert stored[0]["dex_ab"] == "uniswap"
    assert stored[0]["token_b_usd_price"] == pytest.approx(0.5)
    assert stored[0]["token_c_usd_price"] == pytest.approx(0.2)


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
        signals = run_cross_dex_triangular_scan(
            db, _config(cross_dex_min_spread_pct_to_log=0.1, cross_dex_max_plausible_spread_pct=100),
        )

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


def test_cross_dex_scan_discards_implausible_spreads_as_data_errors(db: Database, caplog):
    import logging

    # AERO->USDC coté 8x trop haut par une pool fantôme : cycle « à +740 % ».
    groups = {
        "USDC/WETH": [_pair("uniswap", _USDC, _WETH, 2.0)],
        "WETH/AERO": [_pair("aerodrome", _WETH, _AERO, 3.0)],
        "AERO/USDC": [_pair("sushiswap", _AERO, _USDC, 1.4)],
    }
    with (
        patch("app.trading.triangular._collect_pairs", return_value=groups),
        patch("app.trading.triangular._quote_token_usd_price", return_value=1.0),
        caplog.at_level(logging.INFO, logger="app.trading.triangular"),
    ):
        signals = run_cross_dex_triangular_scan(db, _config(cross_dex_min_spread_pct_to_log=0.1))

    assert signals == []
    assert db.list_cross_dex_triangular_signals(limit=10) == []          # rien de persisté non plus
    assert any("implausible" in record.getMessage() for record in caplog.records)

    # Plafond configurable : relevé à 1000 %, le cycle redevient visible.
    with (
        patch("app.trading.triangular._collect_pairs", return_value=groups),
        patch("app.trading.triangular._quote_token_usd_price", return_value=1.0),
    ):
        signals = run_cross_dex_triangular_scan(
            db, _config(cross_dex_min_spread_pct_to_log=0.1, cross_dex_max_plausible_spread_pct=1000),
        )
    assert len(signals) == 1 and signals[0]["spread_pct"] == pytest.approx(740.0)


def test_cross_dex_scan_ignores_pools_recently_judged_shallow(db: Database):
    from app.trading import pool_memory

    # Deux pools AERO/USDC : sushiswap cote mieux (0.25) mais vient d'être
    # jugée trop peu profonde on-chain -> la jambe doit retomber sur baseswap.
    groups = {
        "USDC/WETH": [_pair("uniswap", _USDC, _WETH, 2.0)],
        "WETH/AERO": [_pair("aerodrome", _WETH, _AERO, 3.0)],
        "AERO/USDC": [_pair("sushiswap", _AERO, _USDC, 0.25), _pair("baseswap", _AERO, _USDC, 0.17)],
    }
    pool_memory.remember_shallow_pool("sushiswap", _USDC, _AERO, 0.39)   # clé symétrique : ordre inversé volontaire
    with (
        patch("app.trading.triangular._collect_pairs", return_value=groups),
        patch("app.trading.triangular._quote_token_usd_price", return_value=1.0),
    ):
        signals = run_cross_dex_triangular_scan(
            db, _config(cross_dex_min_spread_pct_to_log=0.1, cross_dex_max_plausible_spread_pct=100),
        )

    assert len(signals) == 1
    assert signals[0]["dex_ca"] == "baseswap"
    assert signals[0]["cycle_multiplier"] == pytest.approx(2.0 * 3.0 * 0.17)
