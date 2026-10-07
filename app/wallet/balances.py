"""Lecture des balances de wallets (lecture seule, aucune clé privée).

Les RPC endpoints sont résolus depuis la variable d'environnement
`RPC_<NETWORK>` (network normalisé en MAJUSCULES_SANS_ESPACES). Si aucun RPC
n'est configuré pour un réseau, la balance est simplement signalée comme
indisponible plutôt que de faire échouer tout le scan.
"""
from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger("app.wallet.balances")


def _rpc_env_key(network: str) -> str:
    sanitized = re.sub(r"[^A-Z0-9]+", "_", network.upper()).strip("_")
    return f"RPC_{sanitized}"


def _resolve_rpc_url(network: str) -> str | None:
    return os.environ.get(_rpc_env_key(network))


def get_native_balance(address: str, network: str) -> dict:
    """Retourne la balance native (ETH, PC, etc.) d'une adresse EVM, en lecture seule."""
    rpc_url = _resolve_rpc_url(network)
    if not rpc_url:
        return {"balance": None, "error": f"RPC non configuré ({_rpc_env_key(network)})"}

    try:
        from web3 import Web3  # import local : web3 est une dépendance assez lourde

        w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 10}))
        if not w3.is_connected():
            return {"balance": None, "error": "RPC injoignable"}
        checksum = Web3.to_checksum_address(address)
        wei_balance = w3.eth.get_balance(checksum)
        return {"balance": float(Web3.from_wei(wei_balance, "ether")), "error": None}
    except Exception as exc:  # noqa: BLE001 - on isole toute erreur réseau/parsing
        logger.warning("Échec de lecture de balance pour %s sur %s: %s", address, network, exc)
        return {"balance": None, "error": str(exc)}


def get_all_balances(wallets: list[dict]) -> list[dict]:
    results = []
    for w in wallets:
        info = get_native_balance(w["address"], w["network"])
        results.append({
            "name": w["name"],
            "network": w["network"],
            "address": w["address"],
            **info,
        })
    return results
