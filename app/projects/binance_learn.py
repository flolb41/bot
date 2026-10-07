"""Binance Learn & Earn — section 2 du TODO ("Binance Learn & Earn").

⚠️ Aucune clé API Binance n'est utilisée ici : ce module ne fait que surveiller
la page publique des campagnes Learn & Earn (changement de contenu) et
rappeler les règles du TODO. Les quiz/cours doivent être faits manuellement,
en se connectant toi-même sur binance.com — le bot ne se connecte jamais à
ton compte Binance et ne peut ni trader ni retirer quoi que ce soit.
"""
from __future__ import annotations

from app.projects.base import BaseProject


class BinanceLearnProject(BaseProject):
    id = "binance_learn_earn"
    name = "Binance Learn & Earn"
    token = "Divers (selon campagne)"
    network = "Binance (CEX, hors chaîne)"
    official_url = "https://www.binance.com/en/learn-and-earn"
    type = "campagne"
    status = "actif"
    requires_kyc = True  # un compte Binance vérifié est nécessaire pour la plupart des campagnes
    requires_capital = False
    estimated_cost = 0.0
    notes = (
        "Surveillance du site public uniquement, aucune clé API Binance n'est configurée. "
        "Ne jamais acheter un actif uniquement pour débloquer une récompense (règle du TODO). "
        "Faire les quiz/cours gratuits manuellement via l'app/site officiel Binance."
    )

    def default_tasks(self) -> list[dict]:
        base = [
            ("Vérifier les campagnes Learn & Earn disponibles", "https://www.binance.com/en/learn-and-earn"),
            ("Lire les conditions d'éligibilité de chaque campagne", ""),
            ("Faire les cours/quiz gratuits éligibles", ""),
            ("Ne jamais acheter un actif uniquement pour la récompense", ""),
            ("Surveiller l'arrivée de nouveaux programmes", ""),
        ]
        return [
            {
                "project_id": self.id,
                "title": title,
                "url": url,
                "difficulty": "facile",
                "reward_type": "Tokens (selon campagne, montant variable)",
                "status": "pending",
            }
            for title, url in base
        ]
