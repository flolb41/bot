"""Découverte automatique de nouveaux projets à surveiller (option 2, extension
du scanner de micro-récompenses).

IMPORTANT — garde-fou anti-scam : ce module ne fait QUE lister des pistes
provenant de sources publiques. Il n'ajoute JAMAIS automatiquement un projet à
`app.projects.PROJECT_REGISTRY`, ne le whiteliste jamais pour l'auto-signature,
et ne prend aucune décision de légitimité à la place de l'utilisateur — exactement
la même règle d'or que pour les claims automatiques (voir app/projects/base.py).
Chaque candidat reste "pending_review" tant qu'un humain ne l'a pas vérifié et
créé manuellement un fichier de projet dédié.
"""
from app.discovery.scanner import run_discovery

__all__ = ["run_discovery"]
