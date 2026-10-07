"""Flop / FLOPAI — section 1.G du TODO (à préparer, testnet annoncé Q4 2026)."""
from __future__ import annotations

from app.projects.base import BaseProject


class FlopProject(BaseProject):
    id = "flop_flopai"
    name = "Flop / FLOPAI"
    token = "FLOP"
    network = "inconnu (testnet annoncé)"
    official_url = ""
    type = "a_preparer"
    status = "a_preparer"
    requires_kyc = False
    requires_capital = False
    estimated_cost = 0.0
    notes = (
        "Aucune URL officielle confirmée au moment de l'écriture. À compléter manuellement "
        "dès l'ouverture du testnet (Q4 2026 annoncé). Ne jamais installer de binaire non "
        "vérifié en root, même pour du compute CPU/GPU."
    )

    def default_tasks(self) -> list[dict]:
        base = [
            ("Surveiller l'ouverture du testnet", ""),
            ("Lire les règles de participation", ""),
            ("Identifier les possibilités de contribution CPU/GPU/compute", ""),
            ("Vérifier si le Raspberry Pi 3 peut participer utilement", ""),
            ("Vérifier les éventuels rewards Genesis", ""),
            ("Préparer un conteneur isolé", ""),
            ("Ne jamais installer un binaire non vérifié en root", ""),
        ]
        return [
            {
                "project_id": self.id,
                "title": title,
                "url": url,
                "difficulty": "moyen",
                "reward_type": "$FLOP potentiel (non confirmé)",
                "status": "pending",
            }
            for title, url in base
        ]
