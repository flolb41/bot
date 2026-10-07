"""Classe de base pour les modules de suivi de projet (app/projects/*.py).

Chaque projet est une source d'information en lecture seule : on récupère la
page officielle, on calcule un hash de son contenu textuel, et on compare au
hash précédemment enregistré en base pour détecter un changement (nouvelle
campagne, deadline modifiée, etc.). Cette approche générique évite d'écrire
un scraper fragile et spécifique à chaque site (souvent des SPA React) ; elle
sert uniquement à ALERTER qu'une vérification manuelle est nécessaire,
conformément à la règle d'or du TODO (section 16) : jamais de décision
automatique sur la seule foi d'un agrégateur.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass

import httpx

logger = logging.getLogger("app.projects")

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

USER_AGENT = "CryptoRewardHunter/2.0 (+monitoring personnel, lecture seule)"


@dataclass
class ScanResult:
    project_id: str
    reachable: bool
    status_code: int | None
    content_hash: str | None
    changed: bool
    error: str | None = None


def _normalize_html(html: str) -> str:
    """Retire les balises pour limiter le bruit (scripts/CSS avec hash qui change à chaque build)."""
    text = _TAG_RE.sub(" ", html)
    return _WS_RE.sub(" ", text).strip()


class BaseProject:
    """Métadonnées statiques + vérification de changement pour un projet suivi."""

    id: str = ""
    name: str = ""
    token: str = ""
    network: str = ""
    official_url: str = ""
    type: str = "testnet"
    status: str = "actif"
    requires_kyc: bool = False
    requires_capital: bool = False
    estimated_cost: float = 0.0
    notes: str = ""
    start_date: str | None = None
    deadline: str | None = None

    def to_project_row(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "token": self.token,
            "network": self.network,
            "url": self.official_url,
            "official_url": self.official_url,
            "type": self.type,
            "status": self.status,
            "requires_kyc": int(self.requires_kyc),
            "requires_capital": int(self.requires_capital),
            "estimated_cost": self.estimated_cost,
            "notes": self.notes,
            "start_date": self.start_date,
            "deadline": self.deadline,
        }

    def default_tasks(self) -> list[dict]:
        """Liste de tâches de référence (section 1 du TODO). À ajuster manuellement."""
        return []

    def scan(self, timeout: float = 15.0) -> ScanResult:
        """Récupère la page officielle et calcule un hash de contenu normalisé."""
        if not self.official_url:
            return ScanResult(self.id, False, None, None, False, "official_url non configurée")
        try:
            resp = httpx.get(
                self.official_url,
                timeout=timeout,
                headers={"User-Agent": USER_AGENT},
                follow_redirects=True,
            )
            content_hash = hashlib.sha256(_normalize_html(resp.text).encode("utf-8")).hexdigest()
            return ScanResult(self.id, resp.is_success, resp.status_code, content_hash, False)
        except httpx.HTTPError as exc:
            logger.warning("Échec du scan de %s (%s): %s", self.id, self.official_url, exc)
            return ScanResult(self.id, False, None, None, False, str(exc))
