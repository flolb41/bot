"""Moteur d'auto-signature guardé (Phase 4 du TODO — "interactions").

Avant toute automatisation, le TODO impose explicitement :
    - whitelist des contrats
    - limite maximale de gas
    - limite maximale par transaction
    - simulation de transaction
    - arrêt automatique en cas d'anomalie
    - validation manuelle initiale

Ce module implémente ces garde-fous. Tant que `AUTOSIGN_ENABLED` n'est pas à
"true" dans le .env, `send_guarded_transaction` refuse systématiquement
d'envoyer quoi que ce soit (dry-run uniquement). Même activé, toute anomalie
déclenche le killswitch global (`app.killswitch.stop`) et notifie
immédiatement le dashboard web.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from app.database import Database
from app.killswitch import is_stopped, stop as killswitch_stop
from app.wallet.balances import _resolve_rpc_url
from app.wallet.signer import load_signer

logger = logging.getLogger("app.wallet.autosign")


class AutosignRefused(Exception):
    """Levée quand une transaction est refusée par un garde-fou (pas une erreur technique)."""


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    try:
        return float(raw) if raw not in (None, "") else default
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    try:
        return int(raw) if raw not in (None, "") else default
    except ValueError:
        return default


def autosign_enabled() -> bool:
    return _env_bool("AUTOSIGN_ENABLED", False)


def contract_whitelist() -> set[str]:
    raw = os.environ.get("AUTOSIGN_CONTRACT_WHITELIST", "")
    return {addr.strip().lower() for addr in raw.split(",") if addr.strip()}


def max_value_native() -> float:
    return _env_float("AUTOSIGN_MAX_VALUE_NATIVE", 0.0)


def max_gas_price_gwei() -> float:
    return _env_float("AUTOSIGN_MAX_GAS_PRICE_GWEI", 5.0)


def max_tx_per_day() -> int:
    return _env_int("AUTOSIGN_MAX_TX_PER_DAY", 3)


def _today_tx_count(db: Database, wallet_address: str) -> int:
    start_of_day = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    rows = db.fetch_all(
        "SELECT COUNT(*) AS n FROM transactions WHERE wallet = ? AND timestamp >= ?",
        (wallet_address.lower(), start_of_day),
    )
    return int(rows[0]["n"]) if rows else 0


def _notify(db: Database, title: str, message: str, level: str = "info") -> None:
    db.add_notification(title=title, message=message, level=level)


def _anomaly_stop(db: Database, reason: str) -> None:
    killswitch_stop(db)
    logger.error("Anomalie auto-signature -> killswitch activé : %s", reason)
    _notify(db, "🛑 Auto-signature : arrêt automatique", reason, level="alert")


def send_guarded_transaction(
    db: Database,
    *,
    network: str,
    contract_address: str,
    abi: list[dict],
    function_name: str,
    args: tuple = (),
    value_native: float = 0.0,
    action_label: str = "",
) -> dict:
    """Simule puis (si tout est vert) signe et diffuse un appel de contrat.

    Retourne toujours un dict `{"sent": bool, "tx_hash": str|None, "error": str|None}`.
    Ne lève `AutosignRefused` que pour les refus de garde-fou (comportement normal,
    pas une panne) ; toute exception technique est catchée et journalisée/notifiée.
    """
    contract_address_l = contract_address.lower()

    if is_stopped(db):
        raise AutosignRefused("Killswitch actif : aucune transaction automatique n'est envoyée.")
    if not autosign_enabled():
        raise AutosignRefused(
            "AUTOSIGN_ENABLED=false : l'auto-signature est désactivée par défaut. "
            "Active-la explicitement dans le .env après avoir validé le comportement en dry-run."
        )
    if contract_address_l not in contract_whitelist():
        raise AutosignRefused(
            f"Contrat {contract_address} absent de AUTOSIGN_CONTRACT_WHITELIST : refusé par garde-fou."
        )
    if value_native > max_value_native():
        raise AutosignRefused(
            f"Montant {value_native} > limite AUTOSIGN_MAX_VALUE_NATIVE ({max_value_native()})."
        )

    rpc_url = _resolve_rpc_url(network)
    if not rpc_url:
        return {"sent": False, "tx_hash": None, "error": f"RPC non configuré pour {network}"}

    from web3 import Web3

    w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 15}))
    if not w3.is_connected():
        return {"sent": False, "tx_hash": None, "error": "RPC injoignable"}

    signer_account = load_signer()  # clé déchiffrée en mémoire, uniquement ici
    wallet_address = signer_account.address

    if _today_tx_count(db, wallet_address) >= max_tx_per_day():
        raise AutosignRefused(
            f"Limite quotidienne atteinte (AUTOSIGN_MAX_TX_PER_DAY={max_tx_per_day()})."
        )

    gas_price_wei = w3.eth.gas_price
    gas_price_gwei = float(Web3.from_wei(gas_price_wei, "gwei"))
    if gas_price_gwei > max_gas_price_gwei():
        raise AutosignRefused(
            f"Gas price {gas_price_gwei:.2f} gwei > limite AUTOSIGN_MAX_GAS_PRICE_GWEI ({max_gas_price_gwei()})."
        )

    contract = w3.eth.contract(address=Web3.to_checksum_address(contract_address), abi=abi)
    fn = getattr(contract.functions, function_name)(*args)

    value_wei = Web3.to_wei(value_native, "ether")

    # --- Simulation obligatoire avant tout envoi (garde-fou Phase 4) -------
    try:
        fn.call({"from": wallet_address, "value": value_wei})
        estimated_gas = fn.estimate_gas({"from": wallet_address, "value": value_wei})
    except Exception as exc:  # noqa: BLE001
        _anomaly_stop(db, f"Simulation échouée pour {action_label or function_name} sur {contract_address}: {exc}")
        return {"sent": False, "tx_hash": None, "error": f"Simulation échouée : {exc}"}

    try:
        tx = fn.build_transaction({
            "from": wallet_address,
            "value": value_wei,
            "gas": int(estimated_gas * 1.2),
            "gasPrice": gas_price_wei,
            "nonce": w3.eth.get_transaction_count(wallet_address),
            "chainId": w3.eth.chain_id,
        })
        signed = signer_account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction).hex()
    except Exception as exc:  # noqa: BLE001
        _anomaly_stop(db, f"Échec d'envoi pour {action_label or function_name} sur {contract_address}: {exc}")
        return {"sent": False, "tx_hash": None, "error": str(exc)}
    finally:
        signer_account = None  # noqa: F841 - on abandonne la référence à la clé déchiffrée au plus vite

    db.add_transaction({
        "network": network,
        "tx_hash": tx_hash,
        "wallet": wallet_address.lower(),
        "action": action_label or function_name,
        "gas_cost": 0.0,
        "token": "",
        "amount": value_native,
    })
    _notify(
        db,
        "✅ Transaction auto-signée envoyée",
        f"{action_label or function_name} sur {contract_address} ({network}) — tx {tx_hash}",
        level="info",
    )
    return {"sent": True, "tx_hash": tx_hash, "error": None}
