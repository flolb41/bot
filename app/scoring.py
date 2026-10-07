"""Système de scoring des campagnes (section 7 du TODO).

Score sur 100, avec bonus pour les critères favorables et malus pour les
signaux de risque. Le score sert uniquement à prioriser l'attention humaine :
il ne remplace jamais une vérification manuelle de la source officielle.
"""
from __future__ import annotations

from app.models import ScoreBreakdown

# Bonus (section 7)
BONUS_REWARD_ELEVEE = 25          # récompense potentielle élevée
BONUS_GRATUIT = 20                # participation réellement gratuite
BONUS_OFFICIEL_ACTIF = 15         # projet officiel actif
BONUS_FENETRE_COURTE = 15         # fenêtre de participation courte
BONUS_EARLY = 10                  # early user / testnet
BONUS_FAIBLE_CONCURRENCE = 10     # faible concurrence estimée
BONUS_AUTOMATISABLE = 5           # automatisable

# Malus (section 7)
MALUS_DEPOT_OBLIGATOIRE = -30
MALUS_KYC_OBLIGATOIRE = -20
MALUS_FRAIS_IMPORTANTS = -20
MALUS_CONTRAT_NON_VERIFIE = -30
MALUS_DEMANDE_SEED = -50
MALUS_PROJET_DOUTEUX = -20


def compute_score(
    *,
    reward_potential_high: bool = False,
    truly_free: bool = True,
    official_active: bool = True,
    short_window: bool = False,
    early_testnet: bool = False,
    low_competition: bool = False,
    automatable: bool = False,
    requires_deposit: bool = False,
    requires_kyc: bool = False,
    high_fees: bool = False,
    unverified_contract: bool = False,
    asks_for_seed: bool = False,
    is_suspicious: bool = False,
) -> ScoreBreakdown:
    """Calcule le score (0-100, peut être négatif avant clamp) d'un projet/campagne.

    Les paramètres sont des signaux booléens évalués manuellement ou déduits
    des métadonnées du projet (requires_kyc, requires_capital, etc.).
    """
    breakdown = ScoreBreakdown()

    if reward_potential_high:
        breakdown.bonuses["recompense_elevee"] = BONUS_REWARD_ELEVEE
    if truly_free:
        breakdown.bonuses["gratuit"] = BONUS_GRATUIT
    if official_active:
        breakdown.bonuses["officiel_actif"] = BONUS_OFFICIEL_ACTIF
    if short_window:
        breakdown.bonuses["fenetre_courte"] = BONUS_FENETRE_COURTE
    if early_testnet:
        breakdown.bonuses["early_testnet"] = BONUS_EARLY
    if low_competition:
        breakdown.bonuses["faible_concurrence"] = BONUS_FAIBLE_CONCURRENCE
    if automatable:
        breakdown.bonuses["automatisable"] = BONUS_AUTOMATISABLE

    if requires_deposit:
        breakdown.maluses["depot_obligatoire"] = MALUS_DEPOT_OBLIGATOIRE
    if requires_kyc:
        breakdown.maluses["kyc_obligatoire"] = MALUS_KYC_OBLIGATOIRE
    if high_fees:
        breakdown.maluses["frais_importants"] = MALUS_FRAIS_IMPORTANTS
    if unverified_contract:
        breakdown.maluses["contrat_non_verifie"] = MALUS_CONTRAT_NON_VERIFIE
    if asks_for_seed:
        breakdown.maluses["demande_seed"] = MALUS_DEMANDE_SEED
    if is_suspicious:
        breakdown.maluses["projet_douteux"] = MALUS_PROJET_DOUTEUX

    raw_total = sum(breakdown.bonuses.values()) + sum(breakdown.maluses.values())
    breakdown.total = max(0, min(100, raw_total))
    return breakdown


def score_from_project_row(project: dict, **overrides: bool) -> ScoreBreakdown:
    """Dérive les signaux de scoring à partir d'une ligne `projects` en base.

    `overrides` permet de forcer certains signaux (ex: reward_potential_high=True
    renseigné manuellement) sans dupliquer de colonnes supplémentaires en base.
    """
    signals = {
        "requires_deposit": bool(project.get("requires_capital")),
        "requires_kyc": bool(project.get("requires_kyc")),
        "high_fees": (project.get("estimated_cost") or 0) > 20,
        "official_active": project.get("status") in ("actif", "testnet", "campagne_claim"),
        "early_testnet": project.get("status") == "testnet",
        "truly_free": not bool(project.get("requires_capital")),
    }
    signals.update(overrides)
    return compute_score(**signals)
