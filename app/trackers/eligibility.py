"""Vérification d'éligibilité "0 coût / 0 risque" (app/trackers/eligibility.py).

Traduit la règle du TODO : écarter les campagnes nécessitant un dépôt, un
achat ou des frais importants, ou demandant KYC/seed/private key.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class EligibilityCheck:
    eligible_zero_cost: bool
    reasons: list[str]


def check_zero_cost_eligibility(project: dict, max_acceptable_cost: float = 0.0) -> EligibilityCheck:
    reasons: list[str] = []

    if project.get("requires_capital"):
        reasons.append("Nécessite un dépôt/capital (exclu par défaut).")
    if (project.get("estimated_cost") or 0) > max_acceptable_cost:
        reasons.append(
            f"Coût estimé ({project.get('estimated_cost')}) supérieur au plafond accepté "
            f"({max_acceptable_cost})."
        )
    if project.get("requires_kyc"):
        reasons.append("KYC obligatoire : à valider manuellement avant de continuer.")

    return EligibilityCheck(eligible_zero_cost=not reasons, reasons=reasons)


def is_red_flag(text: str) -> bool:
    """Détection naïve de formulations dangereuses dans une description/annonce.

    Ne remplace jamais une vérification humaine ; sert uniquement d'alerte.
    """
    lowered = text.lower()
    red_flags = [
        "seed phrase",
        "private key",
        "clé privée",
        "phrase de récupération",
        "envoyer des fonds pour activer",
        "send funds to activate",
        "deposit required",
        "dépôt obligatoire",
    ]
    return any(flag in lowered for flag in red_flags)
