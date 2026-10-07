"""IRIS Credit — section 1.J du TODO."""
from __future__ import annotations

from app.projects.base import BaseProject


class IrisCreditProject(BaseProject):
    id = "iris_credit"
    name = "IRIS Credit"
    token = "IRIS"
    network = "IRIS Credit testnet (lending/borrowing)"
    official_url = "https://app.iris.credit/borrow"
    type = "testnet"
    status = "testnet"
    requires_kyc = False
    requires_capital = False
    estimated_cost = 0.0
    notes = "Aucun airdrop officiellement confirmé. Participation testnet = éligibilité potentielle future."

    def default_tasks(self) -> list[dict]:
        base = [
            ("Accéder au testnet officiel", "https://app.iris.credit/borrow"),
            ("Obtenir les actifs de test", ""),
            ("Effectuer les interactions gratuites", ""),
            ("Suivre les points/rewards", ""),
            ("Surveiller les conditions du futur airdrop", ""),
        ]
        return [
            {
                "project_id": self.id,
                "title": title,
                "url": url,
                "difficulty": "moyen",
                "reward_type": "$IRIS potentiel (non confirmé)",
                "status": "pending",
            }
            for title, url in base
        ]
