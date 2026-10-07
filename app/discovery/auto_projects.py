"""Projets auto-approuvés : candidats de découverte ayant passé les garde-fous
objectifs (voir app/discovery/scanner.py::_passes_promotion_guardrails).

Exposés comme BaseProject génériques pour bénéficier du MÊME scan passif en
lecture seule (hash de contenu, voir app/projects/base.py::scan) que les 11
projets suivis manuellement — et rien de plus :
- Pas de default_tasks() personnalisées (aucune n'est connue avec certitude).
- claim_candidates() reste [] (hérité de BaseProject) : AUCUNE automatisation
  financière n'est jamais débloquée par ce canal, quels que soient les
  garde-fous passés. Seule la revue manuelle d'un contrat peut whitelister un
  claim.
- "Passer les garde-fous" = critères strictement vérifiables (source
  pré-filtrée par une équipe tierce + absence de mot-clé à risque + lien
  actif), PAS une preuve de légitimité totale : reste affiché comme
  "non vérifié manuellement" sur le dashboard.
"""
from __future__ import annotations

import re

from app.database import Database
from app.projects.base import BaseProject


def _slug(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")
    return f"disc_{s[:40]}" or "disc_projet"


class DiscoveredGenericProject(BaseProject):
    """Instance générique pour un candidat de découverte auto-approuvé."""

    type = "decouverte_auto"
    status = "surveillance_auto (non vérifié manuellement)"

    def __init__(self, title: str, url: str, source: str):
        self.id = _slug(title)
        self.name = title
        self.official_url = url
        self.notes = (
            f"Ajouté automatiquement par le module de découverte (source : {source}). "
            "A passé les garde-fous objectifs (source pré-filtrée + aucun mot-clé à "
            "risque détecté + lien actif) mais N'A PAS été vérifié manuellement en "
            "détail (KYC réel, dépôt requis, légitimité du token...). Aucune "
            "automatisation financière (claim/auto-signature) n'est jamais activée "
            "via ce canal : seule la surveillance passive de la page est automatique."
        )


def auto_approved_projects(db: Database) -> list[BaseProject]:
    """Construit les instances génériques correspondant aux candidats déjà
    auto-approuvés en base (status='auto_approved' dans discovered_projects)."""
    rows = db.list_discovered_projects(status="auto_approved", limit=200)
    return [DiscoveredGenericProject(r["title"], r["url"], r["source"]) for r in rows]
