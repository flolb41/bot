"""vibe/vibe — section 1.E du TODO (PRIORITÉ 2), réseau Robinhood Chain."""
from __future__ import annotations

from app.projects.base import BaseProject


class VibeProject(BaseProject):
    id = "vibe_vibe"
    name = "vibe/vibe"
    token = "VIBE"
    network = "Robinhood Chain (testnet)"
    official_url = "https://docs.robinhood.com/chain"
    type = "testnet"
    status = "testnet"
    requires_kyc = False
    requires_capital = False
    estimated_cost = 0.0
    notes = (
        "Pas de site officiel 'vibe/vibe' confirmé au moment de l'écriture : se fier uniquement "
        "aux docs Robinhood Chain (https://docs.robinhood.com/chain) et au faucet officiel "
        "(https://faucet.testnet.chain.robinhood.com). Vérifier toute mention tierce avant d'agir."
    )

    def default_tasks(self) -> list[dict]:
        base = [
            ("Accéder au testnet officiel Robinhood Chain", "https://docs.robinhood.com/chain"),
            ("Créer le wallet dédié si nécessaire", ""),
            ("Obtenir des tokens de testnet via le faucet officiel",
             "https://faucet.testnet.chain.robinhood.com"),
            ("Effectuer les actions de test vibe/vibe (vérifier la légitimité avant)", ""),
            ("Suivre les points/qualifications", ""),
            ("Surveiller les conditions liées aux testnet rewards", ""),
            ("Vérifier la future distribution du token", ""),
        ]
        return [
            {
                "project_id": self.id,
                "title": title,
                "url": url,
                "difficulty": "moyen",
                "reward_type": "$VIBE potentiel (non confirmé)",
                "status": "pending",
            }
            for title, url in base
        ]
