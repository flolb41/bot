"""Dashboard web minimal du moteur perps papier (FastAPI, port 8001 par défaut)."""
from __future__ import annotations

import time
from html import escape
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from app.perps.engine import STRATEGY_CARRY, STRATEGY_TREND, PaperEngine
from app.perps.models import PairState, PerpsConfig
from app.perps.storage import PerpsStore


def _state_from_observation(row: dict[str, Any]) -> PairState:
    return PairState(
        symbol=row["symbol"], price=float(row["price"]), spread_pct=float(row["spread_pct"] or 0),
        open_fee_pct=float(row["open_fee_pct"] or 0), close_fee_pct=float(row["close_fee_pct"] or 0),
        funding_long_pct_h=float(row["funding_long_pct_h"] or 0), funding_short_pct_h=float(row["funding_short_pct_h"] or 0),
        rollover_pct_h=float(row["rollover_pct_h"] or 0), oi_long_usd=float(row["oi_long_usd"] or 0),
        oi_short_usd=float(row["oi_short_usd"] or 0), min_notional_usd=0.0, max_leverage=0.0,
        market_open=bool(row["market_open"]), observed_at=float(row["observed_at"]),
    )


def build_report(store: PerpsStore, engine: PaperEngine, config: PerpsConfig, now: float | None = None) -> dict[str, Any]:
    now = now if now is not None else time.time()
    latest = {row["symbol"]: _state_from_observation(row) for row in store.latest_observations()}
    markets = [
        {
            "symbol": state.symbol, "price": state.price, "spread_pct": state.spread_pct,
            "open_fee_pct": state.open_fee_pct, "close_fee_pct": state.close_fee_pct,
            "funding_long_pct_h": state.funding_long_pct_h, "funding_short_pct_h": state.funding_short_pct_h,
            "funding_income_apr_short_pct": state.funding_income_apr_pct("short"),
            "funding_income_apr_long_pct": state.funding_income_apr_pct("long"),
            "rollover_pct_h": state.rollover_pct_h, "oi_long_usd": state.oi_long_usd, "oi_short_usd": state.oi_short_usd,
            "market_open": state.market_open, "observed_at": state.observed_at,
        }
        for state in latest.values()
    ]
    open_positions = [
        engine.position_snapshot(position, latest.get(position["symbol"]), now)
        for position in store.open_positions()
    ]
    last_tick = store.get_state("last_tick_at")
    from app.perps.funding_scan import rank_funding

    funding_ranked = rank_funding(store.latest_funding_scan(), config.funding_scan)
    treasury = store.latest_treasury_observation()
    return {
        "mode": config.mode,
        "venue": config.venue,
        "interval_minutes": config.interval_minutes,
        "last_tick_at": float(last_tick) if last_tick else None,
        "markets": markets,
        "strategies": {name: store.strategy_stats(name) for name in (STRATEGY_CARRY, STRATEGY_TREND)},
        "open_positions": open_positions,
        "recent_closed": store.closed_positions(limit=30),
        "events": store.recent_events(limit=40),
        "funding_leaderboard": funding_ranked[:15],
        "funding_scan_pairs": len(funding_ranked),
        "treasury": treasury,
    }


def _fmt(value: Any, digits: int = 4, suffix: str = "") -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):+.{digits}f}{suffix}" if suffix == " $" else f"{float(value):.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return escape(str(value))


def _table(headers: list[str], rows: list[list[str]]) -> str:
    head = "".join(f"<th>{escape(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows) or \
        f"<tr><td colspan='{len(headers)}' class='muted'>aucune ligne</td></tr>"
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _ts(value: Any) -> str:
    if not value:
        return "—"
    return time.strftime("%d/%m %H:%M", time.gmtime(float(value))) + " UTC"


def build_html(report: dict[str, Any]) -> str:
    markets_rows = [[
        escape(m["symbol"]), _fmt(m["price"], 4), _fmt(m["spread_pct"], 3, " %"),
        _fmt(m["funding_income_apr_short_pct"], 2, " %/an"), _fmt(m["funding_income_apr_long_pct"], 2, " %/an"),
        _fmt(m["rollover_pct_h"], 5, " %/h"), f"{m['oi_long_usd']:,.0f} / {m['oi_short_usd']:,.0f}",
        "ouvert" if m["market_open"] else "fermé", _ts(m["observed_at"]),
    ] for m in report["markets"]]
    strategy_rows = []
    for name, stats in report["strategies"].items():
        strategy_rows.append([
            escape(name), str(stats.get("open_positions", 0)), str(stats.get("closed_trades", 0)),
            _fmt(stats.get("win_rate_pct"), 0, " %"), _fmt(stats.get("realized_pnl_usd"), 4, " $"),
            _fmt(-(stats.get("funding_paid_usd") or 0), 4, " $"), _fmt(stats.get("fees_usd"), 4),
            str(stats.get("liquidations", 0)),
        ])
    open_rows = [[
        escape(p["strategy"]), escape(p["symbol"]), escape(p["side"]), _fmt(p["notional_usd"], 0),
        _fmt(p["leverage"], 1, "x"), _fmt(p["entry_price"], 4), _fmt(p.get("mark_price"), 4),
        _fmt(p.get("unrealized_net_usd"), 4, " $"), _fmt(-(p.get("funding_usd") or 0), 4, " $"),
        _fmt(p.get("liquidation_price"), 4), f"{p['hours_open']:.1f} h", escape(p.get("entry_reason") or ""),
    ] for p in report["open_positions"]]
    closed_rows = [[
        _ts(p["closed_at"]), escape(p["strategy"]), escape(p["symbol"]), escape(p["side"]),
        _fmt(p["entry_price"], 4), _fmt(p["exit_price"], 4), _fmt(p["net_pnl_usd"], 4, " $"),
        escape(p.get("exit_reason") or ""),
    ] for p in report["recent_closed"]]
    event_rows = [[_ts(e["created_at"]), escape(e["level"]), escape(e.get("strategy") or ""), escape(e["message"])]
                  for e in report["events"]]
    funding_rows = [[
        escape(f["symbol"]), escape(f["best_side"]), _fmt(f["best_net_apr_pct"], 1, " %/an"),
        _fmt(f["net_apr_short_pct"], 1, " %"), _fmt(f["net_apr_long_pct"], 1, " %"), _fmt(f["rollover_apr_pct"], 1, " %"),
        f"{f['oi_long_usd']:,.0f} / {f['oi_short_usd']:,.0f}", "oui" if f["spot_hedgeable"] else "non",
        "ouvert" if f.get("market_open", True) else "fermé",
    ] for f in report["funding_leaderboard"]]
    treasury = report.get("treasury")
    if treasury:
        treasury_rows = [[
            _ts(treasury["observed_at"]), _fmt(treasury.get("idle_usdc"), 2, " USDC"), _fmt(treasury.get("aave_usdc_apr_pct"), 2, " %"),
            _fmt(treasury.get("vault_7d_apr_pct"), 1, " %"), _fmt(treasury.get("vault_30d_apr_pct"), 1, " %"),
            _fmt(treasury.get("vault_12m_apr_pct"), 1, " %"), escape(str(treasury.get("best_venue") or "—")),
            _fmt(treasury.get("missed_yield_usd"), 4, " $"),
        ]]
    else:
        treasury_rows = []
    return f"""<!doctype html><html lang='fr'><head><meta charset='utf-8'>
<meta http-equiv='refresh' content='60'><title>Perps papier — Base</title>
<style>body{{font-family:system-ui,sans-serif;margin:1.5rem;background:#0f1419;color:#e6e6e6}}
h1,h2{{font-weight:600}} table{{border-collapse:collapse;width:100%;margin-bottom:1.5rem;font-size:.9rem}}
th,td{{border-bottom:1px solid #2a3139;padding:.35rem .5rem;text-align:left;white-space:nowrap}}
th{{color:#9fb3c8}} .muted{{color:#7f8c99}} .banner{{background:#3b2f00;border:1px solid #c89b00;padding:.6rem 1rem;border-radius:6px}}
</style></head><body>
<h1>Perps papier sur Base — {escape(report['venue'])}</h1>
<p class='banner'>Mode <b>{escape(report['mode'])}</b> : aucune transaction réelle, aucune clé. Dernier tick : {_ts(report['last_tick_at'])} (toutes les {report['interval_minutes']:g} min).</p>
<h2>Marchés</h2>{_table(['Paire', 'Prix', 'Spread', 'Funding short (revenu)', 'Funding long (revenu)', 'Rollover', 'OI long / short ($)', 'Marché', 'Observé'], markets_rows)}
<h2>Stratégies</h2>{_table(['Stratégie', 'Ouvertes', 'Clôturées', 'Taux de réussite', 'P&L net réalisé', 'Funding net encaissé', 'Frais ($)', 'Liquidations'], strategy_rows)}
<h2>Positions ouvertes</h2>{_table(['Stratégie', 'Paire', 'Sens', 'Notionnel $', 'Levier', 'Entrée', 'Marque', 'Non réalisé net', 'Funding encaissé', 'Liquidation', 'Durée', 'Motif'], open_rows)}
<h2>Dernières clôtures</h2>{_table(['Date', 'Stratégie', 'Paire', 'Sens', 'Entrée', 'Sortie', 'Net', 'Motif'], closed_rows)}
<h2>Funding — toutes paires ({report.get('funding_scan_pairs', 0)} balayées, top 15 par rendement net)</h2>{_table(['Paire', 'Côté payé', 'Net', 'Net short', 'Net long', 'Rollover', 'OI long / short ($)', 'Spot Base', 'Marché'], funding_rows)}
<h2>Trésorerie (papier) — rendement du capital oisif</h2>{_table(['Observé', 'USDC oisif', 'Aave USDC', 'Vault Veranta 7 j', '30 j', '12 m', 'Meilleur', 'Manque à gagner cumulé'], treasury_rows)}
<h2>Événements</h2>{_table(['Date', 'Niveau', 'Stratégie', 'Message'], event_rows)}
</body></html>"""


def create_perps_app(store: PerpsStore, engine: PaperEngine, config: PerpsConfig) -> FastAPI:
    app = FastAPI(title="Perps papier — Base")

    @app.get("/", response_class=HTMLResponse)
    def dashboard() -> str:
        return build_html(build_report(store, engine, config))

    @app.get("/api/perps")
    def api_perps() -> JSONResponse:
        return JSONResponse(build_report(store, engine, config))

    @app.get("/api/health")
    def api_health() -> JSONResponse:
        now = time.time()
        last_tick = store.get_state("last_tick_at")
        age = max(0.0, now - float(last_tick)) if last_tick else None
        stale_after = config.interval_minutes * 60 * 3
        return JSONResponse({
            "status": "healthy" if age is not None and age <= stale_after else "stale",
            "mode": config.mode,
            "checked_at": now,
            "last_tick_at": float(last_tick) if last_tick else None,
            "last_tick_age_seconds": age,
            "open_positions": len(store.open_positions()),
        })

    return app
