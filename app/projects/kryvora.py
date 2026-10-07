"""Kryvora Network — section 1.D du TODO (PRIORITÉ 2)."""
from __future__ import annotations

from app.projects.base import BaseProject


class KryvoraProject(BaseProject):
    id = "kryvora"
    name = "Kryvora Network"
    token = "KRV"
    network = "Kryvora (Ethereum Layer 2 testnet)"
    official_url = "https://kryvora.network"
    type = "testnet"
    status = "testnet"
    requires_kyc = False
    requires_capital = False
    estimated_cost = 0.0
    notes = "Portail tâches: https://tasks.kryvora.network/ . Docs: https://kryvora.network/docs"

    def default_tasks(self) -> list[dict]:
        base = [
            ("Créer le wallet testnet", ""),
            ("Obtenir les tokens de testnet", "https://kryvora.network"),
            ("Effectuer les interactions disponibles", "https://tasks.kryvora.network/"),
            ("Suivre les récompenses liées aux early testers", ""),
            ("Surveiller les annonces officielles", ""),
            ("Vérifier les conditions de distribution du $KRV", ""),
        ]
        return [
            {
                "project_id": self.id,
                "title": title,
                "url": url,
                "difficulty": "moyen",
                "reward_type": "$KRV potentiel",
                "status": "pending",
            }
            for title, url in base
        ]
