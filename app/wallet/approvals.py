"""Vérification des allowances ERC20 (lecture seule).

Section 4/12 du TODO : "Limiter les allowances", "Révoquer les approvals
inutiles". Ce module ne révoque rien automatiquement (cela nécessiterait de
signer une transaction) : il se contente de lister les approvals existants
pour que l'utilisateur décide manuellement.
"""
from __future__ import annotations

import logging

from app.wallet.balances import _resolve_rpc_url

logger = logging.getLogger("app.wallet.approvals")

ERC20_ALLOWANCE_ABI = [
    {
        "constant": True,
        "inputs": [
            {"name": "_owner", "type": "address"},
            {"name": "_spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    }
]


def check_allowance(token_address: str, owner: str, spender: str, network: str) -> dict:
    """Retourne l'allowance ERC20 accordée par `owner` à `spender` pour `token_address`."""
    rpc_url = _resolve_rpc_url(network)
    if not rpc_url:
        return {"allowance": None, "error": f"RPC non configuré pour {network}"}

    try:
        from web3 import Web3

        w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 10}))
        if not w3.is_connected():
            return {"allowance": None, "error": "RPC injoignable"}

        contract = w3.eth.contract(address=Web3.to_checksum_address(token_address), abi=ERC20_ALLOWANCE_ABI)
        raw_allowance = contract.functions.allowance(
            Web3.to_checksum_address(owner), Web3.to_checksum_address(spender)
        ).call()
        return {"allowance": raw_allowance, "error": None}
    except Exception as exc:  # noqa: BLE001
        logger.warning("Échec de lecture d'allowance (%s/%s/%s): %s", token_address, owner, spender, exc)
        return {"allowance": None, "error": str(exc)}


def is_unlimited_allowance(raw_allowance: int) -> bool:
    """Détecte une allowance proche de uint256 max (= approbation illimitée)."""
    uint256_max = 2**256 - 1
    return raw_allowance >= uint256_max // 2
