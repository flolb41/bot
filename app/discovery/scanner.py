"""Moteur de découverte de nouveaux projets (option 2 du scanner de
micro-récompenses).

Deux niveaux de garde-fous, bien distincts :
1. Promotion vers la SURVEILLANCE PASSIVE automatique (ce module) : critères
   strictement objectifs et vérifiables (source pré-filtrée par une équipe
   tierce + absence de mot-clé à risque + lien réellement accessible). Si un
   candidat les passe, il est ajouté comme projet générique en lecture seule
   (même mécanisme que les 11 projets suivis : hash de contenu, aucune action).
2. Automatisation FINANCIÈRE (claim/auto-signature) : reste séparée, jamais
   débloquée par ce module — nécessite toujours une revue manuelle de contrat
   et un whitelisting explicite (voir app/wallet/autosign.py).
Passer les garde-fous du niveau 1 n'est PAS une preuve de légitimité totale :
impossible à garantir à 100% depuis un simple titre de flux RSS. C'est un
filtre défensif minimal, pas un jugement de confiance.
"""
from __future__ import annotations

import logging

import httpx

from app.database import Database
from app.discovery.sources import DISCOVERY_SOURCES, _USER_AGENT
from app.killswitch import is_stopped

logger = logging.getLogger(__name__)

# Mots-clés purement indicatifs (jamais une preuve de légitimité ni de scam) pour
# aider au triage manuel rapide sur le dashboard. Ne sert à aucune décision automatique.
_RISK_HINT_KEYWORDS = (
    "guaranteed", "garanti", "free money", "argent facile", "100% sûr",
    "send your seed", "seed phrase", "connect wallet to claim instantly",
)

# Sources considérées comme suffisamment pré-filtrées pour une auto-approbation
# de la SURVEILLANCE PASSIVE uniquement (ex: airdrops.io annonce vérifier chaque
# projet avant publication). Les sources communautaires non modérées (Reddit)
# restent toujours en 'pending_review', quel que soit leur contenu.
_TRUSTED_SOURCES_FOR_AUTO_APPROVAL = {"airdrops.io"}


def risk_hint(title: str, summary: str) -> str | None:
    """Repère quelques mots-clés indicatifs (jamais une preuve de légitimité ni de
    scam) pour aider au triage manuel rapide — exposé publiquement pour réutilisation
    côté dashboard (app/dashboard.py)."""
    text = f"{title} {summary}".lower()
    for kw in _RISK_HINT_KEYWORDS:
        if kw in text:
            return f"⚠️ mot-clé suspect détecté : « {kw} » — vérifier manuellement avant toute action."
    return None


def _passes_promotion_guardrails(source: str, title: str, summary: str, url: str) -> tuple[bool, str]:
    """Critères strictement objectifs pour l'auto-approbation en surveillance
    passive. Ne débloque JAMAIS claim_candidates()/whitelist autosign."""
    if source not in _TRUSTED_SOURCES_FOR_AUTO_APPROVAL:
        return False, f"source « {source} » non pré-filtrée par une équipe (seul {sorted(_TRUSTED_SOURCES_FOR_AUTO_APPROVAL)} auto-approuvé)"
    if risk_hint(title, summary):
        return False, "mot-clé à risque détecté dans le titre/résumé"
    try:
        resp = httpx.get(url, timeout=10, follow_redirects=True, headers={"User-Agent": _USER_AGENT})
        if resp.status_code >= 400:
            return False, f"lien renvoie une erreur HTTP {resp.status_code}"
    except Exception as exc:
        return False, f"lien injoignable ({exc})"
    return True, "garde-fous passés : source fiable + aucun mot-clé à risque + lien actif"


def reclassify_pending(db: Database) -> list[dict]:
    """Ré-évalue les candidats déjà en base avec le statut 'pending_review'
    (par exemple détectés avant l'introduction des garde-fous d'auto-approbation,
    ou dont le lien était temporairement injoignable) et les promeut en
    'auto_approved' si — et seulement si — ils passent désormais les garde-fous
    objectifs. Ne rétrograde jamais un candidat déjà 'auto_approved'. Retourne
    la liste des candidats nouvellement promus lors de cet appel."""
    promoted: list[dict] = []
    for entry in db.list_discovered_projects(status="pending_review", limit=1000):
        passed, reason = _passes_promotion_guardrails(
            entry["source"], entry["title"], entry.get("summary", ""), entry["url"],
        )
        if not passed:
            continue
        db.set_discovered_project_status(entry["id"], "auto_approved")
        from app.discovery.auto_projects import DiscoveredGenericProject
        project = DiscoveredGenericProject(entry["title"], entry["url"], entry["source"])
        db.upsert_project(project.to_project_row())
        entry["status"] = "auto_approved"
        entry["promotion_reason"] = reason
        promoted.append(entry)

    if promoted:
        lines = [f"• [{e['source']}] {e['title']}" for e in promoted[:8]]
        more = f"\n… et {len(promoted) - 8} autre(s)." if len(promoted) > 8 else ""
        db.add_notification(
            title=f"✅ {len(promoted)} candidat(s) existant(s) promu(s) en surveillance passive auto",
            message=(
                "Re-vérifiés et désormais conformes aux garde-fous objectifs "
                "(source fiable + aucun mot-clé à risque + lien actif) :\n"
                + "\n".join(lines) + more
            ),
            level="info",
        )
    logger.info("Reclassification : %d candidat(s) en attente promu(s) en auto-approuvé.", len(promoted))
    return promoted


def run_discovery(db: Database) -> list[dict]:
    """Interroge toutes les sources publiques enregistrées, insère les nouveaux
    candidats en base et retourne la liste des candidats NOUVELLEMENT détectés
    lors de cet appel (déjà-vus = ignorés). Ceux qui passent les garde-fous
    objectifs sont auto-approuvés pour la surveillance passive uniquement
    (voir app/discovery/auto_projects.py) ; les autres restent 'pending_review'
    pour validation humaine. Ne modifie jamais `app.projects.PROJECT_REGISTRY`
    ni la whitelist d'auto-signature.
    """
    if is_stopped(db):
        logger.info("Killswitch actif : découverte de projets suspendue.")
        return []

    reclassify_pending(db)

    newly_found: list[dict] = []
    for fetch_source in DISCOVERY_SOURCES:
        try:
            entries = fetch_source()
        except Exception as exc:  # défensif : une source cassée ne doit jamais stopper les autres
            logger.warning("Erreur inattendue dans la source %s : %s", fetch_source.__name__, exc)
            continue
        for entry in entries:
            passed, reason = _passes_promotion_guardrails(
                entry["source"], entry["title"], entry.get("summary", ""), entry["url"],
            )
            status = "auto_approved" if passed else "pending_review"
            inserted_id = db.add_discovered_project(
                source=entry["source"],
                title=entry["title"],
                url=entry["url"],
                summary=entry.get("summary", ""),
                published_at=entry.get("published_at"),
                status=status,
            )
            if inserted_id is not None:
                entry["id"] = inserted_id
                entry["status"] = status
                entry["promotion_reason"] = reason
                entry["risk_hint"] = risk_hint(entry["title"], entry.get("summary", ""))
                newly_found.append(entry)
                if status == "auto_approved":
                    # Import local pour éviter un cycle (auto_projects importe aussi ce module indirectement via le registre)
                    from app.discovery.auto_projects import DiscoveredGenericProject
                    project = DiscoveredGenericProject(entry["title"], entry["url"], entry["source"])
                    db.upsert_project(project.to_project_row())

    approved = [e for e in newly_found if e["status"] == "auto_approved"]
    pending = [e for e in newly_found if e["status"] == "pending_review"]

    if approved:
        lines = [f"• [{e['source']}] {e['title']}" for e in approved[:8]]
        db.add_notification(
            title=f"✅ {len(approved)} nouveau(x) projet(s) ajouté(s) en surveillance passive auto",
            message=(
                "Garde-fous objectifs passés (source fiable + aucun mot-clé à risque + lien actif). "
                "Surveillance passive uniquement — aucune automatisation financière :\n"
                + "\n".join(lines)
            ),
            level="info",
        )
    if pending:
        lines = [f"• [{e['source']}] {e['title']}" for e in pending[:8]]
        more = f"\n… et {len(pending) - 8} autre(s)." if len(pending) > 8 else ""
        db.add_notification(
            title=f"🔭 {len(pending)} nouveau(x) projet(s) en attente de validation manuelle",
            message="N'a pas passé les garde-fous objectifs (source non pré-filtrée ou autre) :\n"
                    + "\n".join(lines) + more,
            level="info",
        )
    logger.info("Découverte : %d nouveau(x) candidat(s) (%d auto-approuvé(s), %d en attente).",
                len(newly_found), len(approved), len(pending))

    return newly_found
