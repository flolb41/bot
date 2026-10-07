"""Estimation des coûts réels d'un aller-retour d'arbitrage (frais DEX + gas).

Tout est volontairement conservateur (on surestime les coûts plutôt que de
les sous-estimer) : un faux négatif ("occasion ratée") ne coûte rien, un faux
positif ("ça a l'air rentable" alors que ça ne l'est pas) pourrait faire
perdre du capital réel une fois la phase live activée.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# Frais "swap" typiques par DEX (en % du montant tradé, par swap — un aller-
# retour d'arbitrage = 2 swaps, un sur chaque DEX). Valeurs publiques connues
# des pools les plus courantes ; volontairement arrondies au-dessus (prudence).
DEX_FEE_PCT = {
    "uniswap": 0.30,      # pools V3 génériques 0.3% (certaines pools sont à 0.05%/1%, on prend le cas médian)
    "aerodrome": 0.30,
    "sushiswap": 0.30,
    "pancakeswap": 0.25,
    "baseswap": 0.30,     # fork UniswapV2 classique, fee standard 0.3%
    "alien-base": 0.30,   # fork UniswapV2 classique, fee standard 0.3%
}
_DEFAULT_DEX_FEE_PCT = 0.30

# Gas estimé pour un aller-retour (2 swaps) sur Base, en unités de gas EVM.
# Base a des blocks gas très peu chers mais le calcul reste fait explicitement
# plutôt que de supposer "c'est négligeable".
ESTIMATED_GAS_UNITS_ROUNDTRIP = 300_000

# Tampon de slippage : sécurité supplémentaire au-delà du spread affiché,
# pour couvrir le mouvement de prix entre détection et exécution ainsi que
# l'impact de prix de notre propre trade sur des pools à faible liquidité.
DEFAULT_SLIPPAGE_BUFFER_PCT = 0.15


def dex_fee_pct(dex_id: str) -> float:
    return DEX_FEE_PCT.get(dex_id.lower(), _DEFAULT_DEX_FEE_PCT)


def estimate_base_gas_price_gwei() -> float | None:
    """Interroge le RPC Base configuré (RPC_BASE, sinon l'endpoint public
    gratuit mainnet.base.org) pour le gas price courant. Lecture seule,
    aucune clé privée utilisée. Retourne None si indisponible."""
    rpc_url = os.environ.get("RPC_BASE") or "https://mainnet.base.org"
    try:
        from web3 import Web3  # import local : dépendance lourde, chargée à la demande

        w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 10}))
        gas_price_wei = w3.eth.gas_price
        return gas_price_wei / 1e9
    except Exception as exc:  # defensif : RPC public parfois instable
        logger.warning("Impossible de récupérer le gas price Base (%s) : %s", rpc_url, exc)
        return None


def estimate_gas_cost_usd(eth_usd_price: float | None, gas_price_gwei: float | None = None,
                           gas_units: int = ESTIMATED_GAS_UNITS_ROUNDTRIP) -> float | None:
    """Coût estimé en USD du gas pour un aller-retour d'arbitrage (2 swaps).
    Retourne None si le prix ETH ou le gas price sont indisponibles (on ne
    devine jamais une valeur par défaut pour un coût qui doit rester fiable)."""
    if eth_usd_price is None:
        return None
    if gas_price_gwei is None:
        gas_price_gwei = estimate_base_gas_price_gwei()
    if gas_price_gwei is None:
        return None
    gas_cost_eth = (gas_price_gwei * 1e9 * gas_units) / 1e18
    return gas_cost_eth * eth_usd_price
