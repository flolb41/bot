"""Analog — section 1.C du TODO (PRIORITÉ 1)."""
from __future__ import annotations

from app.projects.base import BaseProject


class AnalogProject(BaseProject):
    id = "analog"
    name = "Analog"
    token = "ANLOG"
    network = "Analog Testnet (Substrate + EVM via GMP)"
    official_url = "https://www.analog.one/testnet"
    type = "testnet"
    status = "testnet"
    requires_kyc = False
    requires_capital = False
    estimated_cost = 0.0
    notes = (
        "Watch Quests: https://watch.testnet.analog.one/ . Galxe: https://galxe.com/Analog . "
        "Docs: https://docs.analog.one/ . Wallet Substrate pour Watch Quests, EVM pour GMP Quests."
    )

    def default_tasks(self) -> list[dict]:
        base = [
            ("Créer le compte testnet", "https://www.analog.one/testnet"),
            ("Connecter le wallet approprié (Substrate / EVM)", ""),
            ("Effectuer les Watch Quests", "https://watch.testnet.analog.one/"),
            ("Effectuer les GMP Quests si disponibles", ""),
            ("Effectuer les campagnes Galxe associées", "https://galxe.com/Analog"),
            ("Suivre les Analog Testnet Points (ATP)", ""),
            ("Surveiller les critères d'éligibilité du futur $ANLOG", ""),
        ]
        return [
            {
                "project_id": self.id,
                "title": title,
                "url": url,
                "difficulty": "moyen",
                "reward_type": "ATP -> $ANLOG potentiel",
                "status": "pending",
            }
            for title, url in base
        ]
