"""Sources publiques, gratuites et sans clé API, utilisées pour la découverte
de nouveaux projets potentiels (option 2 du scanner de micro-récompenses).

Chaque fonction `fetch_*` retourne une liste de dicts :
    {"source": str, "title": str, "url": str, "summary": str, "published_at": str | None}

Conçu pour être tolérant aux pannes : toute erreur réseau/parsing d'UNE source
ne doit jamais empêcher les autres sources de fonctionner (voir scanner.py).
"""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = 15
_USER_AGENT = "Mozilla/5.0 (CryptoRewardHunter/1.0; +0-capital reward tracker bot)"

_ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}


def fetch_airdrops_io() -> list[dict]:
    """Flux RSS du blog airdrops.io — agrégateur qui annonce vérifier chaque
    projet manuellement avant publication (signal de légitimité plus fort
    qu'un flux non modéré, mais reste à revérifier soi-même : règle d'or)."""
    url = "https://airdrops.io/feed/"
    entries: list[dict] = []
    try:
        resp = httpx.get(url, timeout=_TIMEOUT, headers={"User-Agent": _USER_AGENT})
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
        channel = root.find("channel")
        if channel is None:
            return entries
        for item in channel.findall("item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            if not title or not link:
                continue
            description = (item.findtext("description") or "").strip()
            pub_date = (item.findtext("pubDate") or "").strip() or None
            entries.append({
                "source": "airdrops.io",
                "title": title,
                "url": link,
                "summary": description[:500],
                "published_at": pub_date,
            })
    except Exception as exc:  # defensif : une source HS ne doit jamais bloquer les autres
        logger.warning("Source airdrops.io indisponible : %s", exc)
    return entries


def fetch_reddit_airdrops(subreddit: str = "airdrops", limit: int = 15) -> list[dict]:
    """Flux Atom public de r/<subreddit> (communautaire, non modéré par une
    équipe — signal plus bruité qu'airdrops.io, à vérifier avec davantage de
    prudence). Reddit limite fortement le débit sans authentification : ne
    jamais appeler ceci plus d'une fois toutes les quelques minutes."""
    url = f"https://www.reddit.com/r/{subreddit}/new.rss?limit={limit}"
    entries: list[dict] = []
    try:
        resp = httpx.get(url, timeout=_TIMEOUT, headers={"User-Agent": _USER_AGENT})
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
        for entry in root.findall("a:entry", _ATOM_NS):
            title_el = entry.find("a:title", _ATOM_NS)
            link_el = entry.find("a:link", _ATOM_NS)
            if title_el is None or link_el is None:
                continue
            title = (title_el.text or "").strip()
            link = link_el.get("href", "")
            if not title or not link:
                continue
            summary_el = entry.find("a:summary", _ATOM_NS)
            published_el = entry.find("a:published", _ATOM_NS)
            entries.append({
                "source": f"reddit.com/r/{subreddit}",
                "title": title,
                "url": link,
                "summary": (summary_el.text or "")[:500] if summary_el is not None else "",
                "published_at": published_el.text if published_el is not None else None,
            })
    except Exception as exc:
        logger.warning("Source reddit.com/r/%s indisponible : %s", subreddit, exc)
    return entries


# Registre des sources activées. Ajouter une source = ajouter une entrée ici,
# en respectant la signature `() -> list[dict]`.
DISCOVERY_SOURCES = [
    fetch_airdrops_io,
    fetch_reddit_airdrops,
]
