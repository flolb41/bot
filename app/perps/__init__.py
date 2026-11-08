"""Trading de perpétuels sur Base — PHASE PAPIER UNIQUEMENT.

Ce paquet n'envoie aucun ordre, ne signe aucun message et n'a besoin
d'aucune clé : il lit le marché Veranta (ex-Avantis, perps sur Base) et
simule deux stratégies avec les frais, spreads, funding et rollover réels du
protocole, pour mesurer leur rentabilité avant toute décision live. Il tourne
dans un process et un venv séparés du bot d'arbitrage (voir perps_main.py,
scripts/perps-paper.service) afin de ne jamais le ralentir ni partager ses
dépendances.
"""
