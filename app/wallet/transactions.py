"""Suivi des transactions (app/wallet/transactions.py).

Phase 2 du TODO : "Suivre les transactions". Aucune transaction n'est signée
ici — ce module se contente de vérifier le statut d'une transaction déjà
diffusée (hash fourni manuellement) et de journaliser son coût en gas.
"""
from __future__ import annotations

import logging

from app.database import Database
from app.wallet.balances import _resolve_rpc_url

logger = logging.getLogger("app.wallet.transactions")


def fetch_receipt(tx_hash: str, network: str) -> dict:
    rpc_url = _resolve_rpc_url(network)
    if not rpc_url:
        return {"status": None, "gas_cost": None, "error": f"RPC non configuré pour {network}"}

    try:
        from web3 import Web3

        w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 10}))
        receipt = w3.eth.get_transaction_receipt(tx_hash)
        tx = w3.eth.get_transaction(tx_hash)
        gas_cost = float(Web3.from_wei(receipt["gasUsed"] * tx["gasPrice"], "ether"))
        return {"status": "success" if receipt["status"] == 1 else "failed", "gas_cost": gas_cost, "error": None}
    except Exception as exc:  # noqa: BLE001
        logger.warning("Échec de lecture de la transaction %s sur %s: %s", tx_hash, network, exc)
        return {"status": None, "gas_cost": None, "error": str(exc)}


def log_transaction(db: Database, *, network: str, tx_hash: str, wallet: str = "",
                     action: str = "", token: str = "", amount: float = 0.0) -> int:
    """Enregistre une transaction en base, en tentant de récupérer le coût en gas réel."""
    receipt = fetch_receipt(tx_hash, network)
    gas_cost = receipt.get("gas_cost") or 0.0
    return db.add_transaction({
        "network": network,
        "tx_hash": tx_hash,
        "wallet": wallet,
        "action": action,
        "gas_cost": gas_cost,
        "token": token,
        "amount": amount,
    })
