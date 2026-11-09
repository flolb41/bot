"""Balayage du funding de toutes les paires Veranta : classement et alertes.

Un seul appel `markets.snapshot()` donne funding (par côté, par heure),
rollover et intérêt ouvert des ~200 paires. On en déduit, pour chaque paire et
chaque côté, le rendement NET annualisé d'un portage (funding encaissé −
rollover) ; les paires dont une jambe spot existe sur Base sont marquées
« couvrable » (portage réel possible), les autres ne sont qu'un biais
directionnel payé (XMR short +110 %/an le 09/11/2026 : pas de spot sur Base).
"""
from __future__ import annotations

import logging
from typing import Any

from app.perps.models import HOURS_PER_YEAR, FundingScanConfig
from app.perps.storage import PerpsStore

logger = logging.getLogger("app.perps.funding_scan")


def rank_funding(rows: list[dict[str, Any]], config: FundingScanConfig,
                 averages: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Classement par meilleur rendement net (tous côtés), avec le côté gagnant.
    `averages` (sortie de `PerpsStore.funding_scan_averages`) ajoute le rendement
    net MOYEN du côté gagnant sur la fenêtre : c'est lui qui approche ce qu'un
    portage encaisserait vraiment, le taux instantané étant volatil."""
    ranked: list[dict[str, Any]] = []
    for row in rows:
        rollover_apr = float(row.get("rollover_pct_h") or 0) * HOURS_PER_YEAR
        short_apr = -float(row.get("funding_short_pct_h") or 0) * HOURS_PER_YEAR - rollover_apr
        long_apr = -float(row.get("funding_long_pct_h") or 0) * HOURS_PER_YEAR - rollover_apr
        best_side, best_apr = ("short", short_apr) if short_apr >= long_apr else ("long", long_apr)
        avg = (averages or {}).get(row["symbol"])
        avg_apr = None
        if avg:
            avg_funding = float(avg.get(f"funding_{best_side}_pct_h") or 0)
            avg_apr = -avg_funding * HOURS_PER_YEAR - float(avg.get("rollover_pct_h") or 0) * HOURS_PER_YEAR
        ranked.append({
            **row,
            "rollover_apr_pct": rollover_apr,
            "net_apr_short_pct": short_apr,
            "net_apr_long_pct": long_apr,
            "best_side": best_side,
            "best_net_apr_pct": best_apr,
            "avg_net_apr_pct": avg_apr,
            "avg_samples": int(avg["samples"]) if avg else 0,
            "oi_total_usd": float(row.get("oi_long_usd") or 0) + float(row.get("oi_short_usd") or 0),
            "spot_hedgeable": row["symbol"] in config.spot_hedgeable,
        })
    ranked.sort(key=lambda r: r["best_net_apr_pct"], reverse=True)
    return ranked


def run_funding_scan(store: PerpsStore, config: FundingScanConfig, rows: list[dict[str, Any]], now: float) -> list[str]:
    """Persiste le balayage, émet au plus une alerte par (paire, côté) et par
    `alert_cooldown_hours` quand le rendement net dépasse le seuil sur un
    marché vivant (OI total ≥ `alert_min_oi_usd`)."""
    if not rows:
        return []
    store.record_funding_scan(rows)
    store.prune_funding_scan(now - config.retention_days * 86400)
    store.set_state("last_funding_scan_at", str(now))
    averages = store.funding_scan_averages(now - config.average_window_days * 86400)
    alerts: list[str] = []
    for entry in rank_funding(rows, config, averages):
        if entry["best_net_apr_pct"] < config.alert_min_net_apr_pct or not entry.get("market_open", True):
            continue
        if entry["oi_total_usd"] < config.alert_min_oi_usd:
            continue
        key = f"funding_alert:{entry['symbol']}:{entry['best_side']}"
        last = store.get_state(key)
        if last and now - float(last) < config.alert_cooldown_hours * 3600:
            continue
        store.set_state(key, str(now))
        kind = "portage couvrable (spot Base)" if entry["spot_hedgeable"] else "biais directionnel seulement (pas de spot Base)"
        average = (f", moyenne {config.average_window_days:g} j {entry['avg_net_apr_pct']:+.1f} %/an sur {entry['avg_samples']} balayages"
                   if entry["avg_net_apr_pct"] is not None else "")
        message = (
            f"Funding extrême {entry['symbol']} : {entry['best_side']} encaisse {entry['best_net_apr_pct']:+.1f} %/an net "
            f"(rollover {entry['rollover_apr_pct']:.1f} %/an{average}, OI L/S {entry['oi_long_usd']:,.0f}/{entry['oi_short_usd']:,.0f} $) — {kind}."
        )
        store.add_event(message, level="info", strategy="funding_scan", symbol=entry["symbol"], created_at=now)
        logger.info(message)
        alerts.append(message)
    return alerts
