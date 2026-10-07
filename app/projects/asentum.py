"""Asentum — section 1.I du TODO."""
from __future__ import annotations

from app.projects.base import BaseProject


class AsentumProject(BaseProject):
    id = "asentum"
    name = "Asentum"
    token = "ASE"
    network = "Asentum testnet incitatif"
    official_url = "https://airdrop.asentum.com"
    type = "testnet"
    status = "testnet"
    requires_kyc = False
    requires_capital = False
    estimated_cost = 0.0
    notes = "Site principal: https://www.asentum.com . XP accumulé = éligibilité airdrop."

    def default_tasks(self) -> list[dict]:
        base = [
            ("Vérifier la campagne officielle", "https://airdrop.asentum.com"),
            ("Effectuer les tâches gratuites disponibles", ""),
            ("Suivre l'éligibilité $ASE", ""),
            ("Vérifier les conditions KYC éventuelles", ""),
            ("Surveiller la date de snapshot/claim", ""),
        ]
        return [
            {
                "project_id": self.id,
                "title": title,
                "url": url,
                "difficulty": "facile",
                "reward_type": "XP -> $ASE potentiel",
                "status": "pending",
            }
            for title, url in base
        ]
