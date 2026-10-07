"""Moteur de découverte de nouveaux projets (option 2 du scanner de
micro-récompenses). Voir app/discovery/__init__.py pour la règle d'or
anti-scam : jamais de promotion automatique, validation humaine obligatoire.
"""
from __future__ import annotations

import logging

from app.database import Database
from app.discovery.sources import DISCOVERY_SOURCES
from app.killswitch import is_stopped

logger = logging.getLogger(__name__)

# Mots-clés purement indicatifs (jamais une preuve de légitimité ni de scam) pour
# aider au triage manuel rapide sur le dashboard. Ne sert à aucune décision automatique.
_RISK_HINT_KEYWORDS = (
    "guaranteed", "garanti", "free money", "argent facile", "100% sûr",
    "send your seed", "seed phrase", "connect wallet to claim instantly",
)


def risk_hint(title: str, summary: str) -> str | None:
    """Repère quelques mots-clés indicatifs (jamais une preuve de légitimité ni de
    scam) pour aider au triage manuel rapide — exposé publiquement pour réutilisation
    côté dashboard (app/dashboard.py)."""
    text = f"{title} {summary}".lower()
    for kw in _RISK_HINT_KEYWORDS:
        if kw in text:
            return f"⚠️ mot-clé suspect détecté : « {kw} » — vérifier manuellement avant toute action."
    return None


def run_discovery(db: Database) -> list[dict]:
    """Interroge toutes les sources publiques enregistrées, insère les nouveaux
    candidats en base (status='pending_review') et retourne la liste des
    candidats NOUVELLEMENT détectés lors de cet appel (déjà-vus = ignorés).

    Ne modifie jamais `app.projects.PROJECT_REGISTRY` : purement informatif.
    """
    if is_stopped(db):
        logger.info("Killswitch actif : découverte de projets suspendue.")
        return []

    newly_found: list[dict] = []
    for fetch_source in DISCOVERY_SOURCES:
        try:
            entries = fetch_source()
        except Exception as exc:  # défensif : une source cassée ne doit jamais stopper les autres
            logger.warning("Erreur inattendue dans la source %s : %s", fetch_source.__name__, exc)
            continue
        for entry in entries:
            inserted_id = db.add_discovered_project(
                source=entry["source"],
                title=entry["title"],
                url=entry["url"],
                summary=entry.get("summary", ""),
                published_at=entry.get("published_at"),
            )
            if inserted_id is not None:
                entry["id"] = inserted_id
                entry["risk_hint"] = risk_hint(entry["title"], entry.get("summary", ""))
                newly_found.append(entry)

    if newly_found:
        lines = [f"• [{e['source']}] {e['title']}" for e in newly_found[:8]]
        more = f"\n… et {len(newly_found) - 8} autre(s)." if len(newly_found) > 8 else ""
        db.add_notification(
            title=f"🔭 {len(newly_found)} nouveau(x) projet(s) potentiel(s) détecté(s)",
            message=(
                "À valider manuellement avant tout suivi actif (aucune promotion "
                "automatique) :\n" + "\n".join(lines) + more
            ),
            level="info",
        )
        logger.info("Découverte : %d nouveau(x) candidat(s) détecté(s).", len(newly_found))
    else:
        logger.info("Découverte : aucun nouveau candidat (sources interrogées : %d).",
                     len(DISCOVERY_SOURCES))

    return newly_found
