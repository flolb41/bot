"""Canopy — section 1.B du TODO (PRIORITÉ 1)."""
from __future__ import annotations

from app.projects.base import BaseProject


class CanopyProject(BaseProject):
    id = "canopy"
    name = "Canopy"
    token = "CNPY"
    network = "Canopy Network"
    official_url = "https://app.canopynetwork.org/claim"
    type = "campagne"
    status = "campagne_claim"
    requires_kyc = False
    requires_capital = False
    estimated_cost = 0.0
    notes = (
        "Portail de claim officiel: https://app.canopynetwork.org/claim . "
        "Ne jamais fournir seed/private key, ne jamais envoyer de fonds pour 'activer' l'airdrop."
    )

    def default_tasks(self) -> list[dict]:
        base = [
            ("Vérifier si le wallet possède déjà des points Canopy", "https://app.canopynetwork.org/claim"),
            ("Utiliser uniquement le portail officiel de claim", "https://app.canopynetwork.org/claim"),
            ("Soumettre les points si éligible", ""),
            ("Continuer les activités encore disponibles", ""),
            ("Surveiller le snapshot", ""),
        ]
        return [
            {
                "project_id": self.id,
                "title": title,
                "url": url,
                "difficulty": "facile",
                "reward_type": "$CNPY (claim)",
                "status": "pending",
            }
            for title, url in base
        ]
