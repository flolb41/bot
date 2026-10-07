"""Polyester — section 1.F du TODO (PRIORITÉ 2)."""
from __future__ import annotations

from app.projects.base import BaseProject


class PolyesterProject(BaseProject):
    id = "polyester"
    name = "Polyester"
    token = "P"
    network = "Polyester testnet (DEX, paper trading)"
    official_url = "https://app.polyester.org"
    type = "testnet"
    status = "testnet"
    requires_kyc = False
    requires_capital = False
    estimated_cost = 0.0
    notes = "Paper assets quotidiens (pas de fonds réels). $P non annoncé officiellement."

    def default_tasks(self) -> list[dict]:
        base = [
            ("Rejoindre le testnet officiel", "https://app.polyester.org"),
            ("Créer le compte", ""),
            ("Obtenir les paper assets", ""),
            ("Effectuer les opérations de test", ""),
            ("Participer aux compétitions gratuites", ""),
            ("Suivre les rewards", ""),
            ("Surveiller un éventuel $P airdrop", ""),
        ]
        return [
            {
                "project_id": self.id,
                "title": title,
                "url": url,
                "difficulty": "facile",
                "reward_type": "$P potentiel (non confirmé)",
                "status": "pending",
            }
            for title, url in base
        ]
