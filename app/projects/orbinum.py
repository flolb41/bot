"""Orbinum — section 1.H du TODO."""
from __future__ import annotations

from app.projects.base import BaseProject


class OrbinumProject(BaseProject):
    id = "orbinum"
    name = "Orbinum"
    token = "ORB"
    network = "Orbinum Network"
    official_url = "https://app.orbinum.network/community"
    type = "campagne"
    status = "actif"
    requires_kyc = False
    requires_capital = False
    estimated_cost = 0.0
    notes = (
        "Genesis Community Season 1. Mainnet/TGE annoncé Q4 2026, snapshot ~14 jours avant "
        "le lancement mainnet. Site principal: https://orbinum.network"
    )

    def default_tasks(self) -> list[dict]:
        base = [
            ("Créer/ouvrir le compte officiel", "https://app.orbinum.network/community"),
            ("Effectuer les activités gratuites", ""),
            ("Suivre les ORB points/rewards", ""),
            ("Surveiller l'approche du mainnet", ""),
            ("Vérifier la date de fin de la saison", ""),
            ("Vérifier les conditions de distribution", ""),
        ]
        return [
            {
                "project_id": self.id,
                "title": title,
                "url": url,
                "difficulty": "facile",
                "reward_type": "ORB credits -> $ORB",
                "status": "pending",
            }
            for title, url in base
        ]
