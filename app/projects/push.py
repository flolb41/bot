"""Push Chain — section 1.A du TODO (PRIORITÉ 1)."""
from __future__ import annotations

from app.projects.base import BaseProject


class PushProject(BaseProject):
    id = "push_chain"
    name = "Push Chain"
    token = "PUSH"
    network = "Push Chain (Donut Testnet)"
    official_url = "https://www.pushchain.org/"
    type = "testnet"
    status = "testnet"
    requires_kyc = False
    requires_capital = False
    estimated_cost = 0.0
    notes = (
        "Faucet officiel: https://faucet.push.org/ (1 PC / 6h). "
        "Documentation: https://docs.push.org/setup/faucet . "
        "URLs à reconfirmer via les canaux officiels avant toute interaction."
    )

    def default_tasks(self) -> list[dict]:
        base = [
            ("Créer/ouvrir le compte officiel Push Chain", "https://www.pushchain.org/"),
            ("Connecter le wallet FARMING uniquement", ""),
            ("Obtenir les tokens de testnet via le faucet officiel", "https://faucet.push.org/"),
            ("Effectuer les quêtes actuellement disponibles", ""),
            ("Suivre les XP / passes / points / rewards", ""),
            ("Vérifier les échéances des saisons", ""),
            ("Surveiller les changements de règles", ""),
        ]
        return [
            {
                "project_id": self.id,
                "title": title,
                "url": url,
                "difficulty": "facile",
                "reward_type": "points/XP -> $PUSH potentiel",
                "status": "pending",
            }
            for title, url in base
        ]
