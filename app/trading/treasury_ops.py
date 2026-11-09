"""Dépôt et retrait d'USDC sur Aave V3 (Base) : le plancher de rendement du
capital oisif, exécuté avec les garde-fous des swaps.

Pourquoi : au niveau de funding observé le 09/11/2026 (~8 %/an net réaliste),
un carry ETH à 1x rapporte ~3,8 %/an sur le capital engagé alors que le même
USDC sur Aave touche ~4,9 %/an sans liquidation, sans venue et sans frais de
rotation. Le carry ne redevient supérieur qu'au-dessus de ~15 %/an de
funding ; en attendant, l'USDC qui dort doit au moins toucher Aave.

Garde-fous (identiques à `recover_open_positions`/`sell_token_balance`) :
trading live requis (`is_trading_live`), killswitch, whitelist fixe (le Pool
Aave est dans `routers.YIELD_CONTRACT_ADDRESSES`), approbation ERC-20 au
montant exact, simulation obligatoire avant envoi. `onBehalfOf` et `to` sont
TOUJOURS le wallet de trading : les fonds ne peuvent aller nulle part
ailleurs. `dry_run=True` (défaut) calcule le plan sans rien envoyer.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from app.database import Database
from app.trading.executor import (
    USDC_ADDRESS,
    _balance_of_with_retry,
    _ensure_allowance,
    _is_connected_with_retry,
    _resolve_rpc_url,
    make_w3,
)
from app.trading.guardrails import TradingRefused, is_trading_live, send_trading_transaction
from app.trading.routers import AAVE_V3_POOL_ABI, AAVE_V3_POOL_ADDRESS, AAVE_V3_USDC_ATOKEN_ADDRESS, ERC20_ABI
from app.trading.rpc import call_with_rate_limit_retry

logger = logging.getLogger("app.trading.treasury_ops")

USDC_DECIMALS = 6
MAX_UINT256 = 2 ** 256 - 1
_RAY = 10 ** 27
MIN_OPERATION_USDC = 1.0   # sous 1 USDC, le gas (quelques millièmes de $) ne vaut plus la peine


def _wallet_address() -> str:
    from app.trading.guardrails import trading_keystore_path
    from app.wallet.signer import load_signer

    account = load_signer(keystore_path=trading_keystore_path(), passphrase=os.environ.get("WALLET_TRADING_PASSPHRASE"))
    return account.address


def treasury_snapshot(w3, wallet_address: str) -> dict[str, Any]:
    """Soldes USDC (liquide) et aUSDC (déposé, intérêts inclus) du wallet, et
    taux de dépôt courant d'Aave (lecture seule)."""
    from web3 import Web3

    usdc = w3.eth.contract(address=Web3.to_checksum_address(USDC_ADDRESS), abi=ERC20_ABI)
    ausdc = w3.eth.contract(address=Web3.to_checksum_address(AAVE_V3_USDC_ATOKEN_ADDRESS), abi=ERC20_ABI)
    pool = w3.eth.contract(address=Web3.to_checksum_address(AAVE_V3_POOL_ADDRESS), abi=AAVE_V3_POOL_ABI)
    liquid_units = _balance_of_with_retry(usdc, wallet_address)
    deposited_units = _balance_of_with_retry(ausdc, wallet_address)
    apr_pct = None
    try:
        reserve = call_with_rate_limit_retry(lambda: pool.functions.getReserveData(Web3.to_checksum_address(USDC_ADDRESS)).call())
        apr_pct = int(reserve[2]) / _RAY * 100
        if str(reserve[8]).lower() != AAVE_V3_USDC_ATOKEN_ADDRESS.lower():
            logger.warning("aToken USDC renvoyé par Aave (%s) différent de la constante %s.", reserve[8], AAVE_V3_USDC_ATOKEN_ADDRESS)
    except Exception as exc:  # noqa: BLE001 - l'APR est informatif, pas bloquant
        logger.info("Taux Aave indisponible : %s", exc)
    return {
        "wallet": wallet_address,
        "liquid_usdc": liquid_units / 10 ** USDC_DECIMALS, "liquid_units": liquid_units,
        "deposited_usdc": deposited_units / 10 ** USDC_DECIMALS, "deposited_units": deposited_units,
        "apr_pct": apr_pct,
    }


def _base_result(dry_run: bool, action: str) -> dict[str, Any]:
    return {"action": action, "dry_run": dry_run, "sent": False, "amount_usdc": 0.0, "before": None, "after": None,
            "tx_hash": None, "approve_tx_hash": None, "error": None}


def deposit_usdc(db: Database, config: dict, amount_usdc: float | None = None, *, keep_float_usdc: float = 2.0,
                 dry_run: bool = True) -> dict[str, Any]:
    """Dépose `amount_usdc` (ou tout le solde liquide moins `keep_float_usdc`)
    sur Aave V3. Le flottant conservé laisse de quoi payer un éventuel swap
    de clôture sans retrait préalable."""
    result = _base_result(dry_run, "deposit")
    if not dry_run and not is_trading_live(config):
        result["error"] = "Trading live désactivé (voir app.trading.guardrails.is_trading_live) — utiliser dry_run."
        return result
    try:
        w3 = make_w3(timeout=15)
        if not _is_connected_with_retry(w3):
            result["error"] = "RPC Base injoignable."
            return result
        wallet = _wallet_address()
        before = treasury_snapshot(w3, wallet)
        result["before"] = before
        available = before["liquid_usdc"] - max(0.0, keep_float_usdc)
        amount = available if amount_usdc is None else float(amount_usdc)
        if amount > before["liquid_usdc"] + 1e-9:
            result["error"] = f"Montant {amount:.2f} USDC > solde liquide {before['liquid_usdc']:.2f} USDC."
            return result
        if amount_usdc is not None and amount > available + 1e-9:
            result["error"] = (f"Montant {amount:.2f} USDC laisserait moins que le flottant de {keep_float_usdc:.2f} USDC "
                               f"(solde {before['liquid_usdc']:.2f}). Baisser --keep-float si c'est voulu.")
            return result
        if amount < MIN_OPERATION_USDC:
            result["error"] = f"Rien à déposer : {max(amount, 0):.2f} USDC disponibles au-dessus du flottant (minimum {MIN_OPERATION_USDC:.0f})."
            return result
        units = int(round(amount * 10 ** USDC_DECIMALS))
        result["amount_usdc"] = units / 10 ** USDC_DECIMALS
        if dry_run:
            return result

        approval = _ensure_allowance(
            db, w3, token_address=USDC_ADDRESS, owner=wallet, spender=AAVE_V3_POOL_ADDRESS, amount=units,
            count_against_daily_limit=False,
        )
        result["approve_tx_hash"] = approval.get("tx_hash")
        if approval.get("error"):
            result["error"] = f"Approbation USDC refusée : {approval['error']}"
            return result
        from web3 import Web3

        sent = send_trading_transaction(
            db, rpc_url=_resolve_rpc_url(), contract_address=AAVE_V3_POOL_ADDRESS, abi=AAVE_V3_POOL_ABI,
            function_name="supply",
            args=(Web3.to_checksum_address(USDC_ADDRESS), units, Web3.to_checksum_address(wallet), 0),
            whitelist_target=AAVE_V3_POOL_ADDRESS, action_label=f"Aave V3 supply {result['amount_usdc']:.2f} USDC",
            count_against_daily_limit=False,
        )
        result["tx_hash"] = sent.get("tx_hash")
        result["sent"] = bool(sent.get("sent")) and not sent.get("error")
        result["error"] = sent.get("error")
        result["after"] = treasury_snapshot(w3, wallet)
        if result["sent"]:
            logger.info("Aave V3 : %.2f USDC déposés (tx %s) — déposé total %.2f USDC, liquide %.2f USDC.",
                        result["amount_usdc"], result["tx_hash"], result["after"]["deposited_usdc"], result["after"]["liquid_usdc"])
    except TradingRefused as exc:
        result["error"] = str(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Dépôt Aave en échec.")
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def withdraw_usdc(db: Database, config: dict, amount_usdc: float | None = None, *, dry_run: bool = True) -> dict[str, Any]:
    """Retire `amount_usdc` (ou tout : Aave accepte `uint256.max` pour solder
    la position intérêts compris) vers le wallet de trading."""
    result = _base_result(dry_run, "withdraw")
    if not dry_run and not is_trading_live(config):
        result["error"] = "Trading live désactivé (voir app.trading.guardrails.is_trading_live) — utiliser dry_run."
        return result
    try:
        w3 = make_w3(timeout=15)
        if not _is_connected_with_retry(w3):
            result["error"] = "RPC Base injoignable."
            return result
        wallet = _wallet_address()
        before = treasury_snapshot(w3, wallet)
        result["before"] = before
        if before["deposited_usdc"] < 10 ** -USDC_DECIMALS:
            result["error"] = "Aucun dépôt Aave à retirer."
            return result
        withdraw_all = amount_usdc is None
        amount = before["deposited_usdc"] if withdraw_all else float(amount_usdc)
        if amount > before["deposited_usdc"] + 1e-9:
            result["error"] = f"Montant {amount:.2f} USDC > déposé {before['deposited_usdc']:.2f} USDC (omettre le montant pour tout retirer)."
            return result
        if amount < MIN_OPERATION_USDC and not withdraw_all:
            result["error"] = f"Minimum {MIN_OPERATION_USDC:.0f} USDC par retrait partiel."
            return result
        units = MAX_UINT256 if withdraw_all else int(round(amount * 10 ** USDC_DECIMALS))
        result["amount_usdc"] = before["deposited_usdc"] if withdraw_all else units / 10 ** USDC_DECIMALS
        if dry_run:
            return result
        from web3 import Web3

        label = "Aave V3 withdraw tout" if withdraw_all else f"Aave V3 withdraw {result['amount_usdc']:.2f} USDC"
        sent = send_trading_transaction(
            db, rpc_url=_resolve_rpc_url(), contract_address=AAVE_V3_POOL_ADDRESS, abi=AAVE_V3_POOL_ABI,
            function_name="withdraw",
            args=(Web3.to_checksum_address(USDC_ADDRESS), units, Web3.to_checksum_address(wallet)),
            whitelist_target=AAVE_V3_POOL_ADDRESS, action_label=label, count_against_daily_limit=False,
        )
        result["tx_hash"] = sent.get("tx_hash")
        result["sent"] = bool(sent.get("sent")) and not sent.get("error")
        result["error"] = sent.get("error")
        result["after"] = treasury_snapshot(w3, wallet)
        if result["sent"]:
            logger.info("Aave V3 : retrait de %s (tx %s) — déposé restant %.2f USDC, liquide %.2f USDC.",
                        "tout" if withdraw_all else f"{result['amount_usdc']:.2f} USDC", result["tx_hash"],
                        result["after"]["deposited_usdc"], result["after"]["liquid_usdc"])
    except TradingRefused as exc:
        result["error"] = str(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Retrait Aave en échec.")
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result
