#!/usr/bin/env python3
"""Persistance réelle des écarts inter-DEX détectés — verdict chiffré sur
l'arbitrage tel que ce bot le pratique (lecture seule de la base SQLite).

Usage (sur le Pi) :
    .venv/bin/python scripts/spread_persistence.py [--db data/reward_hunter.db]
        [--days 30] [--threshold 0.7] [--max-gap 180] [--json rapport.json]

Un « épisode » = suite de détections d'une même route (token/quote,
achat→vente) espacées de moins de `--max-gap` secondes et toutes au-dessus du
seuil. Un écart réel capté par un bot lent apparaît au plus dans UN scan
(épisode d'une détection) ; un écart présent pendant des heures, à valeur quasi
constante, est structurel : pool quasi vide, prix DexScreener figé, frais non
couverts — jamais exécutable. Le rapport croise aussi les tentatives live
(écart détecté vs écart revérifié on-chain au moment d'agir, P&L réalisé).
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import statistics
import sys
import time
from collections import defaultdict
from typing import Any

_FRESH_SPREAD_RE = re.compile(r"\((-?\d+(?:\.\d+)?)% < (\d+(?:\.\d+)?)%")


def _route_key(row: dict) -> tuple:
    return (row["token_symbol"], row["quote_symbol"], row["buy_dex"], row["sell_dex"])


def build_episodes(rows: list[dict], *, threshold_pct: float, max_gap_seconds: float) -> list[dict]:
    """Regroupe les détections ≥ seuil en épisodes par route (rows triés ou non).
    Un épisode s'interrompt dès qu'un scan de la même route repasse sous le
    seuil ou qu'il s'écoule plus de `max_gap_seconds` entre deux détections."""
    by_route: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        by_route[_route_key(row)].append(row)
    episodes: list[dict] = []
    for route, detections in by_route.items():
        detections.sort(key=lambda r: float(r["detected_at"]))
        current: list[dict] = []
        for row in detections:
            above = float(row["spread_pct"]) >= threshold_pct
            gap_exceeded = bool(current) and float(row["detected_at"]) - float(current[-1]["detected_at"]) > max_gap_seconds
            if current and (not above or gap_exceeded):
                episodes.append(_summarize_episode(route, current))
                current = []
            if above:
                current.append(row)
        if current:
            episodes.append(_summarize_episode(route, current))
    return episodes


def _summarize_episode(route: tuple, rows: list[dict]) -> dict:
    spreads = [float(r["spread_pct"]) for r in rows]
    nets = [float(r["net_profit_usd"]) for r in rows if r.get("net_profit_usd") is not None]
    first, last = float(rows[0]["detected_at"]), float(rows[-1]["detected_at"])
    return {
        "route": "/".join(route[:2]) + " " + route[2] + "→" + route[3],
        "scans": len(rows),
        "started_at": first,
        "duration_seconds": last - first,
        "spread_min": min(spreads),
        "spread_max": max(spreads),
        "spread_cv": (statistics.pstdev(spreads) / statistics.fmean(spreads)) if len(spreads) > 1 and statistics.fmean(spreads) else 0.0,
        "best_net_usd": max(nets) if nets else None,
        "would_execute": sum(1 for r in rows if r.get("would_execute")),
        "liquidity_min_usd": min(
            (min(float(r["buy_liquidity_usd"] or 0), float(r["sell_liquidity_usd"] or 0)) for r in rows), default=0.0,
        ),
    }


def bucket_episodes(episodes: list[dict]) -> dict[str, dict[str, Any]]:
    """Répartition des épisodes par longueur : 1 scan (fugace), 2–5, 6–20, > 20 (structurel)."""
    buckets = {"1 scan": (1, 1), "2-5 scans": (2, 5), "6-20 scans": (6, 20), ">20 scans": (21, 10**9)}
    out: dict[str, dict[str, Any]] = {}
    for label, (low, high) in buckets.items():
        subset = [e for e in episodes if low <= e["scans"] <= high]
        out[label] = {
            "episodes": len(subset),
            "share_pct": (len(subset) / len(episodes) * 100) if episodes else 0.0,
            "median_duration_min": (statistics.median(e["duration_seconds"] for e in subset) / 60) if subset else None,
            "with_positive_net": sum(1 for e in subset if (e["best_net_usd"] or 0) > 0),
            "median_spread_cv": statistics.median(e["spread_cv"] for e in subset) if subset else None,
        }
    return out


def route_table(episodes: list[dict], limit: int = 12) -> list[dict]:
    by_route: dict[str, list[dict]] = defaultdict(list)
    for e in episodes:
        by_route[e["route"]].append(e)
    table = []
    for route, eps in by_route.items():
        total_time = sum(e["duration_seconds"] for e in eps)
        table.append({
            "route": route,
            "episodes": len(eps),
            "scans": sum(e["scans"] for e in eps),
            "longest_min": max(e["duration_seconds"] for e in eps) / 60,
            "time_above_threshold_h": total_time / 3600,
            "median_cv": statistics.median(e["spread_cv"] for e in eps),
            "best_net_usd": max((e["best_net_usd"] or float("-inf")) for e in eps),
            "liquidity_min_usd": min(e["liquidity_min_usd"] for e in eps),
            "verdict": _route_verdict(eps),
        })
    table.sort(key=lambda r: r["scans"], reverse=True)
    return table[:limit]


def _route_verdict(episodes: list[dict]) -> str:
    longest = max(e["duration_seconds"] for e in episodes)
    median_cv = statistics.median(e["spread_cv"] for e in episodes)
    if longest >= 1800 and median_cv < 0.15:
        return "structurel (écart figé > 30 min : pool vide / prix périmé / frais)"
    if all(e["scans"] == 1 for e in episodes):
        return "fugace (jamais vu deux scans de suite)"
    return "mixte"


def next_scan_survival(rows: list[dict], *, threshold_pct: float, max_gap_seconds: float) -> dict[str, Any]:
    """Parmi les détections marquées rentables (`would_execute`), combien
    affichent encore ≥ seuil au scan suivant de la même route ?"""
    by_route: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        by_route[_route_key(row)].append(row)
    total = survived = no_follow_up = 0
    for detections in by_route.values():
        detections.sort(key=lambda r: float(r["detected_at"]))
        for current, following in zip(detections, detections[1:] + [None]):
            if not current.get("would_execute"):
                continue
            total += 1
            if following is None or float(following["detected_at"]) - float(current["detected_at"]) > max_gap_seconds:
                no_follow_up += 1
            elif float(following["spread_pct"]) >= threshold_pct:
                survived += 1
    return {
        "would_execute_signals": total,
        "still_above_threshold_next_scan": survived,
        "gone_next_scan": total - survived - no_follow_up,
        "no_follow_up_scan": no_follow_up,
        "survival_pct": (survived / (total - no_follow_up) * 100) if total - no_follow_up else None,
    }


def _classify_outcome(row: dict, error: str) -> str:
    """Issue d'une tentative live : succès, écart refermé après achat, revert
    on-chain (gas perdu), refus en simulation (rien envoyé) ou autre."""
    if (row.get("tx_hash_sell") or row.get("tx_hash")) and not error:
        return "succès complet"
    if "refermé" in error:
        return "écart refermé"
    if "status=0" in error:
        return "revert on-chain (gas perdu)"
    lowered = error.lower()
    if "simul" in lowered or "revert" in lowered:
        return "refus en simulation (aucune tx)"
    if not row.get("tx_hash_buy") and not row.get("tx_hash"):
        return "refus avant envoi"
    return "autre échec"


def live_reality(live_rows: list[dict]) -> dict[str, Any]:
    """Écart détecté vs écart revérifié on-chain au moment d'exécuter, P&L réalisé."""
    comparisons = []
    realized = []
    reasons: dict[str, int] = defaultdict(int)
    for row in live_rows:
        error = row.get("execution_error") or ""
        match = _FRESH_SPREAD_RE.search(error)
        if match:
            comparisons.append({
                "id": row["id"], "token": row["token_symbol"], "detected_spread_pct": float(row["spread_pct"]),
                "fresh_spread_pct": float(match.group(1)), "min_required_pct": float(match.group(2)),
            })
        if row.get("realized_pnl_usd") is not None:
            realized.append(float(row["realized_pnl_usd"]))
        reasons[_classify_outcome(row, error)] += 1
    return {
        "attempts": len(live_rows),
        "outcomes": dict(reasons),
        "detected_vs_fresh": comparisons,
        "avg_fresh_minus_detected_pct": (
            statistics.fmean(c["fresh_spread_pct"] - c["detected_spread_pct"] for c in comparisons) if comparisons else None
        ),
        "realized_pnl_total_usd": sum(realized) if realized else None,
        "realized_measured_trades": len(realized),
        "realized_winning_trades": sum(1 for v in realized if v > 0),
    }


def load_rows(db_path: str, since: float) -> tuple[list[dict], list[dict]]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, detected_at, token_symbol, quote_symbol, buy_dex, sell_dex, spread_pct, net_profit_usd, "
            "would_execute, buy_liquidity_usd, sell_liquidity_usd FROM arbitrage_signals WHERE detected_at >= ?",
            (since,),
        )]
        live = [dict(r) for r in conn.execute(
            "SELECT id, detected_at, token_symbol, spread_pct, tx_hash, tx_hash_buy, tx_hash_sell, execution_error, "
            "realized_pnl_usd FROM arbitrage_signals WHERE mode = 'live' AND executed = 1 AND detected_at >= ?",
            (since,),
        )]
    finally:
        conn.close()
    return rows, live


def build_report(rows: list[dict], live_rows: list[dict], *, threshold_pct: float, max_gap_seconds: float,
                 days: float) -> dict[str, Any]:
    episodes = build_episodes(rows, threshold_pct=threshold_pct, max_gap_seconds=max_gap_seconds)
    above = [r for r in rows if float(r["spread_pct"]) >= threshold_pct]
    return {
        "window_days": days,
        "threshold_pct": threshold_pct,
        "signals_total": len(rows),
        "signals_above_threshold": len(above),
        "signals_would_execute": sum(1 for r in rows if r.get("would_execute")),
        "signals_positive_net": sum(1 for r in rows if (r.get("net_profit_usd") or 0) > 0),
        "episodes_total": len(episodes),
        "episode_buckets": bucket_episodes(episodes),
        "routes": route_table(episodes),
        "next_scan_survival": next_scan_survival(rows, threshold_pct=threshold_pct, max_gap_seconds=max_gap_seconds),
        "live": live_reality(live_rows),
    }


def _fmt(value: Any, digits: int = 2, suffix: str = "") -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}f}{suffix}"


def render_text(report: dict[str, Any]) -> str:
    lines = [
        f"=== Persistance des écarts ≥ {report['threshold_pct']:.2f} % — {report['window_days']:g} derniers jours ===",
        f"Signaux : {report['signals_total']} au total, {report['signals_above_threshold']} ≥ seuil, "
        f"{report['signals_would_execute']} marqués rentables, {report['signals_positive_net']} à net > 0.",
        f"Épisodes (≥ seuil, même route, gaps ≤ max-gap) : {report['episodes_total']}",
        "",
        "--- Répartition des épisodes par durée ---",
    ]
    for label, stats in report["episode_buckets"].items():
        lines.append(
            f"  {label:<11} {stats['episodes']:>5} épisodes ({stats['share_pct']:5.1f} %)  durée médiane "
            f"{_fmt(stats['median_duration_min'], 1, ' min'):>10}  net>0 : {stats['with_positive_net']:>3}  "
            f"CV médian de l'écart : {_fmt(stats['median_spread_cv'], 2)}"
        )
    lines += ["", "--- Routes les plus fréquentes au-dessus du seuil ---"]
    for route in report["routes"]:
        lines.append(
            f"  {route['route']:<38} {route['episodes']:>4} ép. / {route['scans']:>5} scans | plus long "
            f"{route['longest_min']:7.1f} min | {route['time_above_threshold_h']:6.1f} h ≥ seuil | CV {route['median_cv']:.2f} | "
            f"meilleur net {_fmt(route['best_net_usd'], 4, ' $')} | liq. min {route['liquidity_min_usd']:,.0f} $ → {route['verdict']}"
        )
    survival = report["next_scan_survival"]
    lines += [
        "",
        "--- Signaux « rentables » : encore là au scan suivant ? ---",
        f"  {survival['would_execute_signals']} signaux rentables ; {survival['still_above_threshold_next_scan']} encore ≥ seuil au scan suivant, "
        f"{survival['gone_next_scan']} disparus, {survival['no_follow_up_scan']} sans scan suivant → survie {_fmt(survival['survival_pct'], 0, ' %')}",
    ]
    live = report["live"]
    lines += ["", f"--- Réalité des tentatives live ({live['attempts']}) ---", f"  Issues : {live['outcomes']}"]
    for c in live["detected_vs_fresh"]:
        lines.append(
            f"  #{c['id']} {c['token']}: détecté {c['detected_spread_pct']:+.2f} % → on-chain au moment d'agir "
            f"{c['fresh_spread_pct']:+.3f} % (frais minimum {c['min_required_pct']:.2f} %)"
        )
    lines.append(
        f"  Écart revérifié − écart détecté : {_fmt(live['avg_fresh_minus_detected_pct'], 2, ' pt')} en moyenne | "
        f"P&L réalisé mesuré : {_fmt(live['realized_pnl_total_usd'], 4, ' $')} sur {live['realized_measured_trades']} trades "
        f"({live['realized_winning_trades']} gagnants)"
    )
    lines += ["", "--- Lecture ---"]
    one_scan = report["episode_buckets"]["1 scan"]["share_pct"]
    structural = sum(1 for r in report["routes"] if r["verdict"].startswith("structurel"))
    lines.append(
        f"  {one_scan:.0f} % des épisodes ne durent qu'un scan (fenêtre < cadence du bot) ; {structural} des "
        f"{len(report['routes'])} routes principales sont structurelles (écart figé, jamais exécutable)."
    )
    if live["detected_vs_fresh"]:
        lines.append("  À l'exécution, l'écart réel on-chain était systématiquement sous les frais : les écarts affichés étaient périmés ou fictifs.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="data/reward_hunter.db")
    parser.add_argument("--days", type=float, default=30.0)
    parser.add_argument("--threshold", type=float, default=0.7, help="écart minimal (%%)")
    parser.add_argument("--max-gap", type=float, default=180.0, help="secondes max entre deux scans d'un même épisode")
    parser.add_argument("--json", default=None, help="écrit aussi le rapport complet en JSON")
    args = parser.parse_args(argv)
    rows, live = load_rows(args.db, time.time() - args.days * 86400)
    report = build_report(rows, live, threshold_pct=args.threshold, max_gap_seconds=args.max_gap, days=args.days)
    print(render_text(report))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=1, default=str)
        print(f"\nRapport JSON : {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
