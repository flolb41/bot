"""Détection + réclamation guardée de micro-récompenses (extension section 16 du TODO).

Règle d'or appliquée ici strictement : AUCUNE décision n'est prise sur la
seule foi d'un agrégateur ou d'un prix supposé. Pour chaque candidat déclaré
par un projet (`BaseProject.claim_candidates()`), on effectue dans l'ordre :

    1. Lecture on-chain (lecture seule, gratuite) du montant réellement
       réclamable pour le wallet auto-signature.
    2. Vérification de légitimité minimale : par défaut (AUTOSIGN_CONTRACT_WHITELIST
       vide), tout candidat est considéré pré-vérifié — car `claim_candidates()`
       ne peut provenir QUE du code source d'un projet, écrit et revu par un
       humain au moment du développement (jamais d'un projet découvert
       automatiquement via RSS : voir app/discovery/auto_projects.py, dont
       claim_candidates() reste [] en dur, en permanence). Si l'utilisateur
       renseigne explicitement AUTOSIGN_CONTRACT_WHITELIST, elle redevient une
       restriction stricte (allow-list) en plus des autres garde-fous.
    3. Estimation de la valeur en EUR de la récompense (CoinGecko). Si le
       token n'est pas coté, on NE RÉCLAME PAS (prudence : mieux vaut rater
       une récompense que de signer à l'aveugle).
    4. Estimation des frais de gas en EUR.
    5. Claim automatique UNIQUEMENT si : whitelist (si définie) respectée +
       valeur > frais (+ marge) + AUTOSIGN_ENABLED=true — via
       `send_guarded_transaction`, qui revérifie indépendamment tous les
       garde-fous (y compris la rentabilité, au cas où les prix auraient bougé
       entre le pré-filtre ici et l'envoi réel), plus la limite de montant, de
       gas, de transactions/jour et le killswitch global.

Dans tous les autres cas (non rentable, autosign désactivé, whitelist
restrictive non respectée, erreur quelconque), une notification informative
est envoyée — jamais de transaction "tentée au hasard".
"""
from __future__ import annotations

import json
import logging

from app.database import Database
from app.killswitch import is_stopped
from app.projects import all_projects
from app.wallet.autosign import (
    AutosignRefused,
    autosign_enabled,
    contract_whitelist,
    min_profit_margin_eur,
    send_guarded_transaction,
)
from app.wallet.balances import _resolve_rpc_url
from app.wallet.pricing import get_price_eur, native_coingecko_id
from app.wallet.signer import get_autosigner_address

logger = logging.getLogger("app.trackers.opportunities")

_STATE_KEY = "last_opportunity_scan"


def _resolve_args(args: tuple, wallet_address: str) -> tuple:
    """Remplace le jeton "{wallet}" par l'adresse réelle dans les args d'appel."""
    return tuple(wallet_address if a == "{wallet}" else a for a in (args or ()))


def _new_entry(project, candidate: dict) -> dict:
    whitelist = contract_whitelist()
    is_whitelisted = (not whitelist) or candidate["contract_address"].lower() in whitelist
    return {
        "project_id": project.id,
        "project_name": project.name,
        "contract_address": candidate["contract_address"],
        "network": candidate["network"],
        "action_label": candidate.get("action_label") or candidate.get("claim_function", "claim"),
        "claimable_amount": None,
        "value_eur": None,
        "fee_eur": None,
        "whitelisted": is_whitelisted,
        "profitable": None,
        "claimed": False,
        "error": None,
    }


def scan_opportunities(db: Database) -> list[dict]:
    """Scanne tous les candidats déclarés, estime leur rentabilité, et ne
    réclame automatiquement que ce qui est whitelisté + rentable + activé.

    Retourne la liste des résultats (aussi persistée en base pour le dashboard,
    clé `last_opportunity_scan` via `Database.get_state`).
    """
    results: list[dict] = []

    if is_stopped(db):
        logger.info("Killswitch actif : scan d'opportunités ignoré.")
        return results

    wallet_address = get_autosigner_address()
    margin = min_profit_margin_eur()

    for project in all_projects():
        candidates = project.claim_candidates()
        for candidate in candidates:
            entry = _new_entry(project, candidate)
            results.append(entry)

            if not wallet_address:
                entry["error"] = "Wallet auto-signature non créé (python main.py create-autosigner-wallet)."
                continue

            rpc_url = _resolve_rpc_url(candidate["network"])
            if not rpc_url:
                entry["error"] = f"RPC non configuré pour {candidate['network']} (RPC_<NETWORK> dans le .env)."
                continue

            try:
                from web3 import Web3

                w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 15}))
                if not w3.is_connected():
                    entry["error"] = "RPC injoignable."
                    continue

                contract = w3.eth.contract(
                    address=Web3.to_checksum_address(candidate["contract_address"]),
                    abi=candidate["abi"],
                )

                amount: float | None = None
                view_fn_name = candidate.get("view_function")
                if view_fn_name:
                    view_args = _resolve_args(candidate.get("view_args"), wallet_address)
                    raw_amount = getattr(contract.functions, view_fn_name)(*view_args).call()
                    decimals = candidate.get("token_decimals", 18)
                    amount = raw_amount / (10 ** decimals)
                entry["claimable_amount"] = amount
            except Exception as exc:  # noqa: BLE001 - toute erreur réseau/ABI est non bloquante pour le scan
                entry["error"] = f"Lecture on-chain échouée : {exc}"
                logger.warning(
                    "Opportunité %s/%s illisible: %s", project.id, candidate["contract_address"], exc
                )
                continue

            if not amount:
                entry["profitable"] = False
                continue  # rien à réclamer actuellement

            token_price = get_price_eur(candidate.get("token_coingecko_id"))
            if token_price is None:
                entry["error"] = "Valeur du token inconnue (non coté) : rentabilité non vérifiable, pas de claim auto."
                entry["profitable"] = False
                continue
            value_eur = amount * token_price
            entry["value_eur"] = value_eur

            native_price = get_price_eur(native_coingecko_id(candidate["network"]))
            try:
                claim_args = _resolve_args(candidate.get("claim_args"), wallet_address)
                fn = getattr(contract.functions, candidate["claim_function"])(*claim_args)
                estimated_gas = fn.estimate_gas({"from": wallet_address})
                gas_price_wei = w3.eth.gas_price
                fee_native = float(Web3.from_wei(estimated_gas * gas_price_wei, "ether"))
            except Exception as exc:  # noqa: BLE001
                entry["error"] = f"Simulation de claim impossible : {exc}"
                entry["profitable"] = False
                continue

            if native_price is None:
                entry["error"] = (
                    f"Prix du gas token de {candidate['network']} inconnu (configure PRICE_NATIVE_"
                    f"{candidate['network'].upper()}) : rentabilité non vérifiable."
                )
                entry["profitable"] = False
                continue
            fee_eur = fee_native * native_price
            entry["fee_eur"] = fee_eur
            entry["profitable"] = value_eur > (fee_eur + margin)

            if not entry["whitelisted"]:
                db.add_notification(
                    title=f"💰 Micro-récompense détectée mais exclue par ta whitelist : {project.name}",
                    message=(
                        f"{amount:.6f} token(s) réclamable(s) (~{value_eur:.4f} € estimés, "
                        f"~{fee_eur:.4f} € de frais). AUTOSIGN_CONTRACT_WHITELIST est définie "
                        f"et n'inclut pas {candidate['contract_address']} — ajoute-le à la liste "
                        "si tu veux autoriser ce claim automatique."
                    ),
                    level="info", project_id=project.id,
                )
                continue

            if not entry["profitable"]:
                db.add_notification(
                    title=f"⚪ Micro-récompense non rentable : {project.name}",
                    message=f"Valeur estimée ~{value_eur:.4f} € <= frais estimés ~{fee_eur:.4f} € : pas de claim.",
                    level="info", project_id=project.id,
                )
                continue

            if not autosign_enabled():
                db.add_notification(
                    title=f"💰 Micro-récompense rentable, claim manuel requis : {project.name}",
                    message=(
                        f"~{value_eur:.4f} € pour ~{fee_eur:.4f} € de frais (rentable). "
                        "AUTOSIGN_ENABLED=false : active-le dans le .env pour automatiser ce claim."
                    ),
                    level="info", project_id=project.id,
                )
                continue

            try:
                claim_args = _resolve_args(candidate.get("claim_args"), wallet_address)
                result = send_guarded_transaction(
                    db,
                    network=candidate["network"],
                    contract_address=candidate["contract_address"],
                    abi=candidate["abi"],
                    function_name=candidate["claim_function"],
                    args=claim_args,
                    action_label=f"Claim {project.name} ({entry['action_label']})",
                    value_eur_estimate=value_eur,
                )
                entry["claimed"] = bool(result.get("sent"))
                if not result.get("sent"):
                    entry["error"] = result.get("error")
            except AutosignRefused as exc:
                entry["error"] = str(exc)

    db.set_state(_STATE_KEY, json.dumps(results, default=str))
    logger.info("Scan d'opportunités terminé : %d candidat(s), %d réclamé(s).",
                len(results), sum(1 for r in results if r["claimed"]))
    return results


def last_scan_results(db: Database) -> list[dict]:
    """Relit le dernier scan persisté (pour le dashboard), sans en relancer un nouveau."""
    raw = db.get_state(_STATE_KEY)
    if not raw:
        return []
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return []
