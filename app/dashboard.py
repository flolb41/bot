"""Dashboard de trading : vues HTML et texte, plus API JSON en lecture seule."""
from __future__ import annotations

import logging
import os
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from html import escape
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from app.monitoring.system_health import read_temperature_celsius
try:
    from pydantic import ConfigDict
except ImportError:
    ConfigDict = None

from app.database import Database
from app.killswitch import is_stopped, stop
from app.trading.executor import EXECUTABLE_QUOTE_TOKENS, _NO_SALE_CLOSED_SENTINEL
from app.trading.runtime_settings import (
    SETTINGS_LIMITS,
    get_runtime_settings,
    save_runtime_settings,
)

logger = logging.getLogger("app.dashboard")
_APP_STARTED_AT = time.time()


class RuntimeSettingsUpdate(BaseModel):
    min_net_profit_usd: float = Field(ge=0.01, le=5.0)
    min_net_profit_pct: float = Field(ge=0.5, le=1.0)
    flashloan_min_net_profit_usd: float = Field(ge=0.25, le=10.0)
    min_liquidity_usd: float = Field(ge=10_000.0, le=1_000_000.0)
    flashloan_enabled: bool

    if hasattr(BaseModel, "model_fields") and ConfigDict is not None:
        model_config = ConfigDict(extra="forbid")
    else:
        class Config:
            extra = "forbid"


def _authorize_dashboard_control(
    request: Request, provided_token: str | None,
) -> None:
    expected_token = os.environ.get("DASHBOARD_CONTROL_TOKEN", "")
    if not expected_token:
        raise HTTPException(
            status_code=503,
            detail="Contrôles désactivés : configurez DASHBOARD_CONTROL_TOKEN dans .env.",
        )
    origin = request.headers.get("origin")
    host = request.headers.get("host", "")
    parsed_origin = urlsplit(origin) if origin else None
    if (
        parsed_origin is None
        or parsed_origin.netloc.lower() != host.lower()
        or parsed_origin.scheme != request.url.scheme
    ):
        raise HTTPException(status_code=403, detail="Origine de requête invalide.")
    if not provided_token or not secrets.compare_digest(provided_token, expected_token):
        raise HTTPException(status_code=401, detail="Jeton de contrôle invalide.")


def _money(value: Any, digits: int = 3) -> str:
    if value is None:
        return "N/D"
    try:
        return f"{float(value):,.{digits}f} $"
    except (TypeError, ValueError):
        return "N/D"


def _percent(value: Any) -> str:
    try:
        return f"{float(value):.2f}%"
    except (TypeError, ValueError):
        return "N/D"


def _time(value: Any) -> str:
    if value is None:
        return "—"
    try:
        return datetime.fromtimestamp(float(value), timezone.utc).astimezone().strftime("%d/%m %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return "—"


def _badge(label: str, kind: str = "muted", title: str = "") -> str:
    tooltip = f" title='{escape(title, quote=True)}'" if title else ""
    return f"<span class='badge {kind}'{tooltip}>{escape(label)}</span>"


def _profit_badge(signal: dict[str, Any], quote_executable: bool = True) -> str:
    if not signal.get("would_execute"):
        return _badge("Sous le seuil net", "muted")
    if not quote_executable:
        return _badge("Théorique · quote non exécutable", "warn",
                      "Le profit calculé ne garantit pas que le signal puisse être exécuté.")
    return _badge("Rentable estimé", "good",
                  "Estimation de simulation après frais, slippage et gas, pas un profit réalisé.")


def _execution_badge(signal: dict[str, Any], *, triangular: bool = False) -> str:
    if not signal.get("executed"):
        return _badge("Simulation", "muted")
    tx_hash = signal.get("tx_hash") if triangular else signal.get("tx_hash_sell")
    error = signal.get("execution_error")
    if error:
        return _badge("Échec / à vérifier", "bad", str(error))
    if triangular:
        return _badge("Transaction confirmée", "good", tx_hash or "")
    if signal.get("tx_hash_buy") and not tx_hash:
        return _badge("Position potentiellement ouverte", "bad",
                      "Achat diffusé sans vente confirmée. Vérifier le solde du wallet.")
    if tx_hash == _NO_SALE_CLOSED_SENTINEL:
        return _badge("Clôture sans vente", "warn")
    return _badge("Round-trip confirmé", "good", tx_hash or "")


def build_dashboard_text(db: Database) -> str:
    arbitrage = db.arbitrage_stats_summary()
    triangular = db.triangular_stats_summary()
    live = db.live_trading_stats()
    directional = db.directional_live_stats()
    paper = db.fetch_all(
        "SELECT strategy, COUNT(*) AS total, "
        "SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) AS open_trades, "
        "SUM(CASE WHEN status='closed' THEN net_pnl_usd ELSE 0 END) AS realized_pnl_usd "
        "FROM paper_strategy_trades GROUP BY strategy ORDER BY strategy"
    )
    return "\n".join((
        "BOT DE TRADING — BASE",
        f"Arbitrages inter-DEX : {arbitrage.get('total', 0)} signaux, "
        f"{arbitrage.get('would_execute_count', 0) or 0} rentables en simulation",
        f"Cycles triangulaires : {triangular.get('total', 0)} signaux, "
        f"{triangular.get('would_execute_count', 0) or 0} rentables en simulation",
        f"Trades réels : {live.get('total_live', 0) or 0}, "
        f"{live.get('completed', 0) or 0} terminés, {live.get('open_count', 0) or 0} ouverts",
        f"Momentum directionnel live : {directional.get('total', 0) or 0} tentatives, "
        f"{directional.get('open_count', 0) or 0} position(s) ouverte(s), "
        f"P&L mesuré "
        f"{'—' if directional.get('unmeasured_closed') else _money(directional.get('realized_pnl_usd'), 4)}",
        f"Stratégies paper : {len(paper)} expérimentations suivies, sans ordres live.",
        "P&L de simulation : estimation uniquement, pas un résultat réalisé.",
    ))


def build_dashboard_html(db: Database) -> str:
    """Construit une page de trading complète, sans action de trading."""
    stopped = is_stopped(db)
    unread_count = db.count_unread_notifications()
    arb_stats = db.arbitrage_stats_summary()
    tri_stats = db.triangular_stats_summary()
    live_stats = db.live_trading_stats()
    directional_stats = db.directional_live_stats()
    from app.trading.paper_strategies import list_paper_strategy_trades, paper_strategy_stats

    paper_stats = paper_strategy_stats(db)
    paper_trades = list_paper_strategy_trades(db, limit=20)
    arb_signals = db.list_arbitrage_signals(limit=30)
    tri_signals = db.list_triangular_signals(limit=30)
    cross_signals = db.list_cross_dex_triangular_signals(limit=20)
    missed = db.list_missed_opportunities(limit=20)
    open_positions = db.list_open_positions()
    directional_positions = db.fetch_all(
        "SELECT * FROM directional_live_trades "
        "WHERE status IN ('pending', 'open', 'unresolved') ORDER BY entry_at DESC"
    )
    controlled_position_count = len(open_positions) + len(directional_positions)
    unresolved_directional_count = db.directional_unresolved_positions_count()
    cross_stats = db.fetch_one(
        "SELECT COUNT(*) AS total FROM cross_dex_triangular_signals WHERE detected_at >= ?",
        (db.dashboard_stats_reset_at(),),
    ) or {"total": 0}

    blockers: list[str] = []
    config_error: str | None = None
    runtime_config: dict[str, Any] = {}
    try:
        from app.config import load_config
        from app.trading.guardrails import trading_live_blockers

        runtime_config = load_config().raw
        blockers = trading_live_blockers(runtime_config)
    except Exception as exc:
        logger.exception("Impossible de déterminer le mode live du dashboard.")
        config_error = type(exc).__name__
    live_authorized = not blockers and config_error is None and not stopped
    runtime_settings = get_runtime_settings(db, runtime_config)

    last_scan_state = db.get_state("last_scan_completed_at")
    try:
        last_scan = float(last_scan_state) if last_scan_state else None
    except ValueError:
        logger.exception("Horodatage du dernier scan invalide dans system_state.")
        last_scan = None
    if last_scan is None:
        last_scan_values = [
            value for value in (arb_stats.get("last_scan_at"), tri_stats.get("last_scan_at"))
            if value is not None
        ]
        last_scan = max(last_scan_values) if last_scan_values else None
    simulated_profit = (
        float(arb_stats.get("cumulative_net_profit_usd") or 0)
        + float(tri_stats.get("cumulative_net_profit_usd") or 0)
    )
    total_signals = int(arb_stats.get("total") or 0) + int(tri_stats.get("total") or 0)
    profitable_signals = (
        int(arb_stats.get("would_execute_count") or 0)
        + int(tri_stats.get("would_execute_count") or 0)
    )

    mode = (
        _badge("LIVE autorisé par la configuration", "bad")
        if live_authorized else _badge("SIMULATION · aucune transaction", "good")
    )
    if config_error:
        mode_note = f"Lecture de la configuration impossible ({escape(config_error)}). Le dashboard considère le live bloqué."
    elif stopped:
        mode_note = "Killswitch actif : les scans automatiques et toute exécution sont arrêtés."
    elif blockers:
        mode_note = "Exécution désarmée : " + "; ".join(escape(item) for item in blockers) + "."
    else:
        mode_note = "Les conditions de configuration autorisent le live ; les contrôles par transaction restent requis."

    stop_banner = (
        "<div class='banner danger'>⛔ Killswitch actif — trading automatique arrêté. "
        "Les métriques ci-dessous restent consultables.</div>"
        if stopped else ""
    )
    position_banner = (
        f"<div class='banner danger'>⚠️ {controlled_position_count} position(s) à contrôler"
        f"{f', dont {unresolved_directional_count} non résolue(s)' if unresolved_directional_count else ''}.</div>"
        if controlled_position_count else ""
    )

    arb_rows: list[str] = []
    for signal in arb_signals:
        quote_ok = (signal.get("quote_address") or "").lower() in EXECUTABLE_QUOTE_TOKENS
        flashloan = "—"
        if signal.get("flashloan_notional_usd") is not None:
            flashloan = (
                f"{_money(signal.get('flashloan_net_profit_usd'))} "
                f"<small>sur {_money(signal.get('flashloan_notional_usd'), 0)}</small>"
            )
        pair = f"{escape(str(signal.get('token_symbol') or '?'))}/{escape(str(signal.get('quote_symbol') or '?'))}"
        route = f"{escape(str(signal.get('buy_dex') or '?'))} → {escape(str(signal.get('sell_dex') or '?'))}"
        error = signal.get("execution_error")
        error_cell = f"<small class='error'>{escape(str(error))}</small>" if error else ""
        realized_cell = _money(signal.get("realized_pnl_usd"), 4) if signal.get("realized_pnl_usd") is not None else "—"
        arb_rows.append(
            "<tr>"
            f"<td>{_time(signal.get('detected_at'))}</td><td><strong>{pair}</strong></td><td>{route}</td>"
            f"<td>{_percent(signal.get('spread_pct'))}</td><td>{_money(signal.get('trade_size_usd'), 2)}</td>"
            f"<td>{_money(signal.get('net_profit_usd'))}</td><td>{flashloan}</td><td>{realized_cell}</td>"
            f"<td>{_profit_badge(signal, quote_ok)}<br>{_execution_badge(signal)}{error_cell}</td>"
            "</tr>"
        )
    arb_table = _table(
        ("Détecté", "Paire", "Achat → vente", "Spread", "Taille estimée", "Net estimé", "Flashloan estimé",
         "P&L réel", "Statut"),
        arb_rows, "Aucun signal d’arbitrage inter-DEX enregistré.",
    )

    tri_rows: list[str] = []
    for signal in tri_signals:
        cycle = " → ".join(escape(str(signal.get(f"token_{letter}_symbol") or "?")) for letter in "abc")
        cycle += f" → {escape(str(signal.get('token_a_symbol') or '?'))}"
        flashloan = "—"
        if signal.get("flashloan_notional_usd") is not None:
            flashloan = (
                f"{_money(signal.get('flashloan_net_profit_usd'))} "
                f"<small>sur {_money(signal.get('flashloan_notional_usd'), 0)}</small>"
            )
        realized_cell = _money(signal.get("realized_pnl_usd"), 4) if signal.get("realized_pnl_usd") is not None else "—"
        tri_rows.append(
            "<tr>"
            f"<td>{_time(signal.get('detected_at'))}</td><td><strong>{cycle}</strong></td>"
            f"<td>{escape(str(signal.get('dex') or '?'))}</td><td>{_percent(signal.get('spread_pct'))}</td>"
            f"<td>{_money(signal.get('trade_size_usd'), 2)}</td><td>{_money(signal.get('net_profit_usd'))}</td>"
            f"<td>{flashloan}</td><td>{realized_cell}</td>"
            f"<td>{_profit_badge(signal)}<br>{_execution_badge(signal, triangular=True)}</td>"
            "</tr>"
        )
    tri_table = _table(
        ("Détecté", "Cycle", "DEX", "Spread", "Taille estimée", "Net estimé", "Flashloan estimé",
         "P&L réel", "Statut"),
        tri_rows, "Aucun cycle triangulaire enregistré.",
    )

    paper_labels = {
        "liquid_momentum": "Momentum · actifs liquides",
        "microcap_momentum": "Momentum · microcaps observées",
    }
    paper_stat_rows = []
    for stat in paper_stats:
        closed = int(stat.get("closed_trades") or 0)
        winners = int(stat.get("winners") or 0)
        win_rate = f"{winners / closed * 100:.1f}%" if closed else "—"
        paper_stat_rows.append(
            "<tr>"
            f"<td><strong>{escape(paper_labels.get(stat['strategy'], stat['strategy']))}</strong></td>"
            f"<td>{closed}</td><td>{stat.get('open_trades', 0) or 0}</td><td>{win_rate}</td>"
            f"<td>{_money(stat.get('realized_pnl_usd'), 4) if closed else '—'}</td>"
            "</tr>"
        )
    paper_stats_table = _table(
        ("Expérience paper", "Clôturés", "Ouverts", "Taux gagnant", "P&amp;L net paper"),
        paper_stat_rows, "Aucun trade paper : constitution de l’historique de prix en cours.",
    )
    paper_trade_rows = []
    for trade in paper_trades:
        paper_trade_rows.append(
            "<tr>"
            f"<td>{escape(paper_labels.get(trade.get('strategy'), str(trade.get('strategy'))))}</td>"
            f"<td>{escape(str(trade.get('token_symbol') or '?'))}</td>"
            f"<td>{_time(trade.get('entry_at'))}</td>"
            f"<td>{_money(trade.get('entry_price_usd'), 6)}</td>"
            f"<td>{_money(trade.get('exit_price_usd'), 6) if trade.get('exit_price_usd') is not None else '—'}</td>"
            f"<td>{_money(trade.get('net_pnl_usd'), 4) if trade.get('net_pnl_usd') is not None else 'Ouvert'}</td>"
            f"<td>{escape(str(trade.get('exit_reason') or '—'))}</td></tr>"
        )
    paper_trades_table = _table(
        ("Stratégie", "Token", "Entrée", "Prix entrée", "Prix sortie", "P&amp;L net", "Sortie"),
        paper_trade_rows, "Aucun trade paper enregistré.",
    )

    cross_rows = []
    for signal in cross_signals:
        cycle = " → ".join(escape(str(signal.get(f"token_{letter}_symbol") or "?")) for letter in "abc")
        cycle += f" → {escape(str(signal.get('token_a_symbol') or '?'))}"
        route = " / ".join(escape(str(signal.get(key) or "?")) for key in ("dex_ab", "dex_bc", "dex_ca"))
        execution_status = signal.get("execution_status") or "detected"
        if execution_status == "executed":
            execution_badge = _badge("Exécuté", "good", str(signal.get("execution_tx_hash") or ""))
        elif execution_status == "failed":
            execution_badge = _badge(
                "Échec après envoi" if signal.get("execution_tx_hash") else "Refus pré-exécution",
                "bad",
                str(signal.get("execution_error") or ""),
            )
        elif signal.get("flashloan_net_profit_usd") is not None:
            execution_badge = _badge(
                "Préfiltre positif" if signal["flashloan_net_profit_usd"] > 0 else "Non rentable",
                "warn",
                "Estimation uniquement ; la profondeur et la simulation on-chain restent obligatoires.",
            )
        else:
            execution_badge = _badge("Informationnel", "muted")
        cross_rows.append(
            f"<tr><td>{_time(signal.get('detected_at'))}</td><td>{cycle}</td>"
            f"<td>{route}</td><td>{_percent(signal.get('spread_pct'))}</td>"
            f"<td>{_money(signal.get('min_liquidity_usd'), 0)}</td>"
            f"<td>{_money(signal.get('flashloan_net_profit_usd'), 4)}</td>"
            f"<td>{execution_badge}</td></tr>"
        )
    cross_table = _table(
        ("Détecté", "Cycle", "DEX par jambe", "Spread théorique", "Liquidité min.", "Net estimé", "État"),
        cross_rows, "Aucun cycle inter-DEX informationnel enregistré.",
    )

    position_rows = []
    for position in open_positions:
        position_rows.append(
            f"<tr><td>{_time(position.get('detected_at'))}</td>"
            f"<td>{escape(str(position.get('token_symbol') or '?'))}/{escape(str(position.get('quote_symbol') or '?'))}</td>"
            f"<td>{escape(str(position.get('buy_dex') or '?'))} → {escape(str(position.get('sell_dex') or '?'))}</td>"
            f"<td>{escape(str(position.get('tx_hash_buy') or '—'))}</td>"
            f"<td>{escape(str(position.get('execution_error') or 'Vérifier le solde du wallet'))}</td></tr>"
        )
    for position in directional_positions:
        quote_address = str(position.get("quote_address") or "")
        quote_label = "USDC" if quote_address.lower() == "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913" else quote_address[:10]
        position_rows.append(
            f"<tr><td>{_time(position.get('entry_at'))}</td>"
            f"<td>{escape(str(position.get('token_symbol') or '?'))}/{escape(quote_label or '?')}</td>"
            f"<td>{escape(str(position.get('dex') or '?'))} · momentum</td>"
            f"<td>{escape(str(position.get('entry_tx_hash') or '—'))}</td>"
            f"<td>{escape(str(position.get('execution_error') or position.get('status') or 'À contrôler'))}</td></tr>"
        )
    position_table = _table(
        ("Achat confirmé", "Paire", "Route", "Transaction achat", "État"),
        position_rows, "Aucune position résiduelle enregistrée.",
    )

    missed_rows = []
    for signal in missed:
        missed_rows.append(
            f"<tr><td>{_time(signal.get('detected_at'))}</td>"
            f"<td>{escape(str(signal.get('token_symbol') or '?'))}/{escape(str(signal.get('quote_symbol') or '?'))}</td>"
            f"<td>{_percent(signal.get('spread_pct'))}</td><td>{_money(signal.get('net_profit_usd'))}</td>"
            f"<td>{escape(str(signal.get('reason') or '—'))}</td></tr>"
        )
    missed_table = _table(
        ("Détecté", "Paire", "Spread", "Net estimé", "Pourquoi filtré"),
        missed_rows, "Aucune occasion filtrée enregistrée.",
    )

    def slider(name: str, label: str, step: float, unit: str) -> str:
        minimum, maximum = SETTINGS_LIMITS[name]
        value = runtime_settings[name]
        precision = 0 if name == "min_liquidity_usd" else 3
        return (
            f"<label class='setting'><span>{label} "
            f"<strong id='{name}-value'>{value:.{precision}f}{unit}</strong></span>"
            f"<input type='range' id='{name}' min='{minimum}' max='{maximum}' "
            f"step='{step}' value='{value}'></label>"
        )

    unread_class = " unread" if unread_count else ""
    database_size = 0
    try:
        database_size = db.db_path.stat().st_size
    except OSError:
        logger.exception("Impossible de lire la taille de la base depuis le dashboard.")
    last_scan_age = max(0, int(time.time() - last_scan)) if last_scan else None
    cpu_temperature = read_temperature_celsius()
    scan_health = (
        "degraded" if unresolved_directional_count
        else "healthy" if last_scan_age is not None and last_scan_age <= 180
        else "stale"
    )
    controls_status = (
        "Contrôles authentifiés disponibles ; changements actifs au prochain scan."
        if os.environ.get("DASHBOARD_CONTROL_TOKEN") else
        "Contrôles verrouillés : configurez DASHBOARD_CONTROL_TOKEN dans .env."
    )
    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Dashboard trading Base</title>
<style>
  :root {{ color-scheme:dark; --bg:#0b1020; --panel:#121a2b; --line:#26334a; --muted:#91a0b8; --text:#e6edf7; --blue:#60a5fa; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; padding:24px; background:var(--bg); color:var(--text); font:14px/1.5 system-ui,sans-serif; }}
  main {{ max-width:1500px; margin:auto; }}
  h1 {{ margin:0; font-size:26px; }} h2 {{ margin:0 0 12px; font-size:18px; }}
  h3 {{ margin:0 0 8px; font-size:14px; }}
  .topbar {{ display:flex; justify-content:space-between; align-items:flex-start; gap:18px; margin-bottom:22px; }}
  .subtle, small {{ color:var(--muted); }} .subtle {{ margin:5px 0; }}
  .mode {{ margin-top:10px; display:flex; align-items:center; gap:10px; flex-wrap:wrap; }}
  .cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:12px; margin:18px 0 24px; }}
  .card, section {{ background:var(--panel); border:1px solid var(--line); border-radius:12px; }}
  .card {{ padding:15px; }} .card .label {{ color:var(--muted); text-transform:uppercase; font-size:11px; letter-spacing:.06em; }}
  .card .value {{ font-size:23px; font-weight:700; margin-top:3px; }}
  section {{ padding:17px; margin:16px 0; }} .section-head {{ display:flex; justify-content:space-between; gap:10px; align-items:baseline; }}
  .table-wrap {{ overflow-x:auto; }} table {{ width:100%; border-collapse:collapse; min-width:800px; }}
  th,td {{ text-align:left; padding:9px 10px; border-bottom:1px solid var(--line); vertical-align:top; font-size:12px; }}
  th {{ color:var(--muted); text-transform:uppercase; font-size:10px; white-space:nowrap; }}
  tr:last-child td {{ border-bottom:0; }} .badge {{ display:inline-block; padding:2px 8px; border-radius:99px; font-size:10px; white-space:nowrap; }}
  .good {{ background:#14532d; color:#bbf7d0; }} .warn {{ background:#78350f; color:#fde68a; }}
  .bad {{ background:#7f1d1d; color:#fecaca; }} .muted {{ background:#334155; color:#cbd5e1; }}
  .banner {{ border-radius:9px; padding:12px 15px; margin:12px 0; }} .danger {{ background:#491b25; border:1px solid #7f1d1d; color:#fecaca; }}
  .empty {{ color:var(--muted); padding:16px; text-align:center; }}
  .bell {{ border:1px solid var(--line); color:var(--text); background:var(--panel); border-radius:8px; padding:10px 14px; cursor:pointer; }}
  .dot {{ display:none; background:#b91c1c; border-radius:20px; padding:1px 6px; margin-left:5px; }} .bell.unread .dot {{ display:inline; }}
  .notif-panel {{ display:none; position:absolute; right:24px; top:75px; width:min(380px,90vw); max-height:420px; overflow:auto; background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:10px; z-index:3; }}
  .notif-panel.open {{ display:block; }} .notif-item {{ padding:9px; border-bottom:1px solid var(--line); }}
  .error {{ color:#fca5a5; display:block; margin-top:4px; }} a {{ color:var(--blue); }}
  .tabs {{ display:flex; gap:8px; overflow:auto; margin:20px 0 14px; border-bottom:1px solid var(--line); }}
  .tab {{ background:transparent; border:0; border-bottom:2px solid transparent; color:var(--muted); padding:10px 14px; cursor:pointer; white-space:nowrap; }}
  .tab.active {{ color:var(--text); border-color:var(--blue); }} .tab-panel {{ display:none; }} .tab-panel.active {{ display:block; }}
  .health-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:10px; }}
  .health-item {{ padding:12px; border-radius:8px; background:#0c1728; }}
  .setting {{ display:block; margin:18px 0; }} .setting span {{ display:flex; justify-content:space-between; }}
  input[type=range] {{ width:100%; margin-top:10px; accent-color:var(--blue); }}
  input[type=password] {{ width:min(420px,100%); padding:10px; border:1px solid var(--line); border-radius:7px; background:#0b1423; color:var(--text); }}
  .checkbox,.actions {{ display:flex; align-items:center; gap:10px; margin:16px 0; flex-wrap:wrap; }}
  .action-button {{ border:1px solid var(--line); border-radius:8px; padding:10px 14px; background:#17253a; color:var(--text); cursor:pointer; }}
  .action-button.primary {{ background:#1d4ed8; }} .action-button.danger {{ background:#7f1d1d; }}
  footer {{ color:var(--muted); padding:8px 0 24px; font-size:12px; }}
  @media(max-width:600px) {{ body {{ padding:13px; }} h1 {{ font-size:21px; }} section {{ padding:12px; }} }}
</style>
</head>
<body><main>
<div class="topbar"><div>
  <h1>📊 Dashboard du bot de trading · Base</h1>
  <p class="subtle">Arbitrage inter-DEX · triangulaire · suivi des exécutions</p>
  <div class="mode">{mode}<span class="subtle">{mode_note}</span></div>
  <p class="subtle">Dernier scan : {_time(last_scan)} · Actualisation santé toutes les 15 s</p>
</div><div style="position:relative">
  <button class="bell{unread_class}" id="notif-bell" onclick="toggleNotifications()">🔔<span class="dot" id="notif-count">{unread_count}</span></button>
  <div class="notif-panel" id="notif-panel"><div id="notif-list" class="subtle">Ouvrir pour charger les notifications.</div></div>
</div></div>
{stop_banner}
{position_banner}
<nav class="tabs" role="tablist" aria-label="Sections du dashboard">
  <button type="button" class="tab active" id="tab-button-overview" role="tab" aria-controls="tab-overview" aria-selected="true" tabindex="0" data-tab="overview">Vue d’ensemble</button>
  <button type="button" class="tab" id="tab-button-trading" role="tab" aria-controls="tab-trading" aria-selected="false" tabindex="-1" data-tab="trading">Signaux &amp; exécutions</button>
  <button type="button" class="tab" id="tab-button-research" role="tab" aria-controls="tab-research" aria-selected="false" tabindex="-1" data-tab="research">Recherche paper</button>
  <button type="button" class="tab" id="tab-button-settings" role="tab" aria-controls="tab-settings" aria-selected="false" tabindex="-1" data-tab="settings">Réglages &amp; système</button>
</nav>
<div class="tab-panel active" id="tab-overview" role="tabpanel" aria-labelledby="tab-button-overview" tabindex="0">
<div class="cards">
  <div class="card"><div class="label">Signaux analysés</div><div class="value">{total_signals}</div></div>
  <div class="card"><div class="label">Rentables estimés</div><div class="value">{profitable_signals}</div></div>
  <div class="card"><div class="label">P&amp;L hypothétique</div><div class="value">{_money(simulated_profit, 2)}</div><small>Simulation cumulée, non réalisée</small></div>
  <div class="card"><div class="label">P&amp;L réalisé mesuré</div><div class="value">{_money(live_stats.get('realized_pnl_usd'), 4) if live_stats.get('measured_trades') else "—"}</div><small>{live_stats.get('measured_trades', 0)} trade(s) mesuré(s)</small></div>
  <div class="card"><div class="label">Gas on-chain mesuré</div><div class="value">{_money(live_stats.get('actual_gas_cost_usd'), 4) if live_stats.get('measured_trades') else "—"}</div><small>Reçus de transactions live</small></div>
  <div class="card"><div class="label">Transactions tentées</div><div class="value">{live_stats.get('total_live', 0) or 0}</div></div>
  <div class="card"><div class="label">Round-trips terminés</div><div class="value">{live_stats.get('completed', 0) or 0}</div></div>
  <div class="card"><div class="label">Positions momentum live</div><div class="value">{directional_stats.get('open_count', 0) or 0}</div><small>{directional_stats.get('completed', 0) or 0} clôturée(s) · P&amp;L mesuré {'—' if directional_stats.get('unmeasured_closed') else _money(directional_stats.get('realized_pnl_usd'), 4)}</small></div>
  <div class="card"><div class="label">Positions à contrôler</div><div class="value">{controlled_position_count}</div></div>
  <div class="card"><div class="label">Cycles inter-DEX info</div><div class="value">{cross_stats.get('total', 0) or 0}</div></div>
</div>
<section><div class="section-head"><h2>Santé du bot</h2><span id="health-status" class="badge {'good' if scan_health == 'healthy' else 'warn' if scan_health == 'stale' else 'bad'}">{'Sain' if scan_health == 'healthy' else 'Scan en retard' if scan_health == 'stale' else 'Position à contrôler'}</span></div>
<div class="health-grid">
  <div class="health-item"><small>Âge du dernier scan</small><div id="health-scan">{'Aucun' if last_scan_age is None else f'{last_scan_age} s'}</div></div>
  <div class="health-item"><small>Température CPU</small><div id="health-cpu-temp">{'—' if cpu_temperature is None else f'{cpu_temperature:.1f} °C'}</div></div>
  <div class="health-item"><small>Uptime du processus</small><div id="health-uptime">{int(time.time() - _APP_STARTED_AT)} s</div></div>
  <div class="health-item"><small>Base SQLite</small><div id="health-db">{database_size / (1024 * 1024):.1f} Mio</div></div>
  <div class="health-item"><small>Positions à contrôler</small><div id="health-positions">{controlled_position_count}</div></div>
</div><p class="subtle">Un scan de plus de 3 minutes est signalé. Cela ne certifie ni la fraîcheur des prix ni la rentabilité.</p></section>
</div>

<div class="tab-panel" id="tab-trading" role="tabpanel" aria-labelledby="tab-button-trading" tabindex="0">

<section><div class="section-head"><h2>Arbitrage inter-DEX</h2><span class="subtle">{arb_stats.get('total', 0) or 0} signaux au total</span></div>
<p class="subtle">Le net et le flashloan sont des estimations de scan. Le P&amp;L réel est mesuré après exécution à partir du delta on-chain et des frais de gas du reçu ; « — » signifie qu’aucune mesure n’est disponible pour ce trade.</p>
{_table(('Détecté','Paire','Achat → vente','Spread','Taille estimée','Net estimé','Flashloan estimé','Statut'), arb_rows, 'Aucun signal inter-DEX.')}</section>

<section><div class="section-head"><h2>Arbitrage triangulaire sur un DEX</h2><span class="subtle">{tri_stats.get('total', 0) or 0} cycles au total</span></div>
<p class="subtle">Les cycles triangulaires sont exécutés atomiquement lorsqu’ils sont autorisés. Le P&amp;L réel inclut le delta du token de départ et les frais de gas du reçu.</p>
{tri_table}</section>

<section><h2>Cycles entre plusieurs DEX</h2>
<p class="subtle">Le flashloan cross-DEX est soumis au rendement minimum, à la profondeur on-chain et à une simulation atomique avant diffusion.</p>{cross_table}</section>

<section><h2>Positions résiduelles</h2>
<p class="subtle">Un achat confirmé sans vente confirmée peut laisser une exposition au token. Contrôler le wallet et la transaction avant toute nouvelle action.</p>
{position_table}</section>

<section><h2>Signaux filtrés avant journalisation</h2>{missed_table}</section>
</div>
<div class="tab-panel" id="tab-research" role="tabpanel" aria-labelledby="tab-button-research" tabindex="0">
<section><h2>Expériences paper — aucune transaction envoyée</h2>
<p class="subtle">Momentum 15 min sur pools observées : actifs liquides et microcaps (FDV ≤ 50 M$, liquidité ≥ 25 k$, volume 24 h ≥ 100 k$). Taille simulée 5 $, frais DEX, slippage et gas estimés inclus. Les résultats ne sont pas des performances live et aucun résultat ne sera promu automatiquement.</p>
{paper_stats_table}{paper_trades_table}</section>
</div>

<div class="tab-panel" id="tab-settings" role="tabpanel" aria-labelledby="tab-button-settings" tabindex="0">
<section><h2>Réglages de stratégie live</h2>
<div class="banner danger"><strong>Attention :</strong> ces réglages modifient directement les critères du trading live au prochain scan, sans phase paper. La prévalidation on-chain de chaque transaction reste active. Les tailles maximales, plafonds de risque et garde-fous du contrat ne sont pas modifiables ici.</div>
<p class="subtle">{controls_status} Dashboard en HTTP sur le LAN : ne redirigez pas le port 8000 vers Internet et n’utilisez les commandes que sur un réseau de confiance.</p>
<label class="setting"><span>Jeton administrateur (DASHBOARD_CONTROL_TOKEN)</span><input type="password" id="control-token" autocomplete="current-password" placeholder="Jeton de contrôle"></label>
{slider('min_net_profit_usd', 'Profit net minimum classique', 0.01, ' $')}
{slider('min_net_profit_pct', 'Profit net minimum relatif', 0.05, ' %')}
{slider('flashloan_min_net_profit_usd', 'Profit net minimum flashloan', 0.01, ' $')}
{slider('min_liquidity_usd', 'Liquidité minimale des pools', 1000, ' $')}
<label class="checkbox"><input type="checkbox" id="flashloan_enabled" {'checked' if runtime_settings['flashloan_enabled'] else ''}> Autoriser les exécutions flashloan (plafonds de risque conservés)</label>
<div class="actions"><button class="action-button primary" id="save-settings">Enregistrer les réglages live</button><button class="action-button danger" id="stop-trading">Arrêt d’urgence (killswitch)</button><span id="settings-result" role="status" class="subtle"></span></div>
<p class="subtle">Les commandes sont authentifiées et journalisées. Le jeton n’est gardé que dans cet onglet navigateur.</p>
</section>
<section><h2>Accès aux données</h2>
<p class="subtle"><a href="/api/status">État</a> · <a href="/api/arbitrage">Arbitrage</a> ·
<a href="/api/triangular">Triangulaire</a> · <a href="/api/health">Santé</a> ·
<a href="/api/runtime-settings">Réglages effectifs</a> · <a href="/api/paper-strategies">Stratégies paper</a> ·
<a href="/api/notifications">Notifications</a></p>
</section>
</div>
<footer>Le bot peut trader en réel si ses garde-fous l’autorisent. Les estimations ne garantissent pas de profit ; le P&amp;L mesuré reste la référence.</footer>
</main>
<script>
var tabButtons=Array.from(document.querySelectorAll('[role="tab"]'));
function activateTab(name,updateUrl) {{
  var selected=tabButtons.find(function(button){{return button.dataset.tab===name;}});
  if(!selected)return false;
  tabButtons.forEach(function(button) {{
    var active=button===selected;
    button.classList.toggle('active',active);
    button.setAttribute('aria-selected',String(active));
    button.tabIndex=active?0:-1;
    document.getElementById(button.getAttribute('aria-controls')).classList.toggle('active',active);
  }});
  if(updateUrl)history.replaceState(null,'','#'+name);
  return true;
}}
tabButtons.forEach(function(button,index) {{
  button.addEventListener('click',function(){{activateTab(button.dataset.tab,true);}});
  button.addEventListener('keydown',function(event) {{
    var nextIndex=index;
    if(event.key==='ArrowRight')nextIndex=(index+1)%tabButtons.length;
    else if(event.key==='ArrowLeft')nextIndex=(index+tabButtons.length-1)%tabButtons.length;
    else if(event.key==='Home')nextIndex=0;
    else if(event.key==='End')nextIndex=tabButtons.length-1;
    else return;
    event.preventDefault();
    var next=tabButtons[nextIndex];
    activateTab(next.dataset.tab,true);
    next.focus();
  }});
}});
window.addEventListener('hashchange',function(){{activateTab(location.hash.slice(1),false);}});
activateTab(location.hash.slice(1),false);
var tokenInput=document.getElementById('control-token');
tokenInput.value=sessionStorage.getItem('dashboardControlToken')||'';
['min_net_profit_usd','min_net_profit_pct','flashloan_min_net_profit_usd','min_liquidity_usd'].forEach(function(name) {{
  var input=document.getElementById(name), output=document.getElementById(name+'-value');
  input.addEventListener('input',function() {{
    output.textContent=Number(input.value).toFixed(name==='min_liquidity_usd'?0:2)+(name==='min_net_profit_pct'?' %':' $');
  }});
}});
function controlHeaders(){{return {{'Content-Type':'application/json','X-Dashboard-Control-Token':tokenInput.value}};}}
document.getElementById('save-settings').addEventListener('click',async function() {{
  var result=document.getElementById('settings-result');
  if(!confirm('Appliquer ces paramètres au trading réel dès le prochain scan ?'))return;
  try {{
    var response=await fetch('/api/runtime-settings',{{method:'PUT',headers:controlHeaders(),body:JSON.stringify({{
      min_net_profit_usd:Number(document.getElementById('min_net_profit_usd').value),
      min_net_profit_pct:Number(document.getElementById('min_net_profit_pct').value),
      flashloan_min_net_profit_usd:Number(document.getElementById('flashloan_min_net_profit_usd').value),
      min_liquidity_usd:Number(document.getElementById('min_liquidity_usd').value),
      flashloan_enabled:document.getElementById('flashloan_enabled').checked
    }})}});
    var data=await response.json(); if(!response.ok)throw new Error(data.detail||'Enregistrement refusé');
    sessionStorage.setItem('dashboardControlToken',tokenInput.value);
    result.textContent='Réglages enregistrés et actifs au prochain scan.';
  }} catch(error) {{result.textContent='Erreur : '+error.message;}}
}});
document.getElementById('stop-trading').addEventListener('click',async function() {{
  if(!confirm('Activer le killswitch ? Les nouveaux scans seront arrêtés ; une transaction déjà envoyée ne peut pas être annulée.'))return;
  var result=document.getElementById('settings-result');
  try {{
    var response=await fetch('/api/control/stop',{{method:'POST',headers:controlHeaders()}});
    var data=await response.json(); if(!response.ok)throw new Error(data.detail||'Commande refusée');
    sessionStorage.setItem('dashboardControlToken',tokenInput.value);
    result.textContent='Killswitch activé. Reprise via Telegram /start.';
    setTimeout(function(){{window.location.reload();}},1000);
  }} catch(error) {{result.textContent='Erreur : '+error.message;}}
}});
function toggleNotifications() {{
  var panel=document.getElementById('notif-panel');
  panel.classList.toggle('open');
  if(panel.classList.contains('open')) loadNotifications();
}}
function esc(value) {{
  var node=document.createElement('span'); node.textContent=value || ''; return node.innerHTML;
}}
function updateBell(count) {{
  document.getElementById('notif-count').textContent=count;
  document.getElementById('notif-bell').classList.toggle('unread',count>0);
}}
function loadNotifications() {{
  fetch('/api/notifications?limit=30').then(function(r){{return r.json();}}).then(function(data) {{
    var list=document.getElementById('notif-list');
    updateBell(data.unread_count || 0);
    if(!data.items || !data.items.length) {{ list.textContent='Aucune notification.'; return; }}
    list.innerHTML=data.items.map(function(n) {{
      return '<div class="notif-item"><strong>'+esc(n.title)+'</strong><div class="subtle">'
        +new Date(n.created_at*1000).toLocaleString('fr-FR')+'</div><div>'+esc(n.message)+'</div></div>';
    }}).join('');
  }}).catch(function(){{document.getElementById('notif-list').textContent='Notifications indisponibles.';}});
}}
setInterval(function() {{
  fetch('/api/notifications?limit=1').then(function(r){{return r.json();}})
    .then(function(data){{updateBell(data.unread_count || 0);}}).catch(function(){{}});
}},15000);
async function refreshHealth() {{
  try {{
    var d=await fetch('/api/health').then(function(r){{return r.json();}});
    var status=document.getElementById('health-status');
    status.textContent=d.status==='healthy'?'Sain':d.status==='stale'?'Scan en retard':d.status==='degraded'?'Position à contrôler':'État inconnu';
    status.className='badge '+(d.status==='healthy'?'good':d.status==='stale'?'warn':'bad');
    document.getElementById('health-scan').textContent=d.last_scan_age_seconds===null?'Aucun scan':d.last_scan_age_seconds+' s';
    document.getElementById('health-cpu-temp').textContent=d.cpu_temperature_celsius===null?'Indisponible':Number(d.cpu_temperature_celsius).toFixed(1)+' °C';
    document.getElementById('health-uptime').textContent=d.uptime_seconds+' s';
    document.getElementById('health-db').textContent=(d.database_size_bytes/1048576).toFixed(1)+' Mio';
    document.getElementById('health-positions').textContent=d.open_positions;
  }} catch(error) {{document.getElementById('health-status').textContent='API indisponible';}}
}}
setInterval(refreshHealth,15000);
</script>
</body></html>"""


def _table(headers: tuple[str, ...], rows: list[str], empty_message: str) -> str:
    if not rows:
        return f"<div class='empty'>{escape(empty_message)}</div>"
    head = "".join(f"<th>{escape(header)}</th>" for header in headers)
    return f"<div class='table-wrap'><table><thead><tr>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"


def create_app(db: Database) -> FastAPI:
    app = FastAPI(title="Dashboard trading Base")

    @app.get("/", response_class=HTMLResponse)
    def dashboard() -> str:
        return build_dashboard_html(db)

    @app.get("/text", response_class=HTMLResponse)
    def dashboard_text() -> str:
        return f"<html><body><pre style='font-size:16px'>{escape(build_dashboard_text(db))}</pre></body></html>"

    @app.get("/api/arbitrage")
    def api_arbitrage() -> JSONResponse:
        return JSONResponse({
            "stats": db.arbitrage_stats_summary(),
            "live_trading": db.live_trading_stats(),
            "signals": db.list_arbitrage_signals(limit=100),
        })

    @app.get("/api/triangular")
    def api_triangular() -> JSONResponse:
        return JSONResponse({
            "stats": db.triangular_stats_summary(),
            "signals": db.list_triangular_signals(limit=100),
        })

    @app.get("/api/cross-dex-triangular")
    def api_cross_dex_triangular() -> JSONResponse:
        return JSONResponse({
            "signals": db.list_cross_dex_triangular_signals(limit=100),
        })

    @app.get("/api/missed-opportunities")
    def api_missed_opportunities() -> JSONResponse:
        return JSONResponse({"signals": db.list_missed_opportunities(limit=100)})

    @app.get("/api/paper-strategies")
    def api_paper_strategies() -> JSONResponse:
        from app.trading.paper_strategies import list_paper_strategy_trades, paper_strategy_stats

        return JSONResponse({
            "strategies": paper_strategy_stats(db),
            "trades": list_paper_strategy_trades(db, limit=100),
        })

    @app.get("/api/runtime-settings")
    def api_runtime_settings() -> JSONResponse:
        from app.config import load_config
        from app.trading.guardrails import trading_live_blockers

        config = load_config().raw
        return JSONResponse({
            "settings": get_runtime_settings(db, config),
            "limits": SETTINGS_LIMITS,
            "live_authorized": not is_stopped(db) and not trading_live_blockers(config),
        })

    @app.put("/api/runtime-settings")
    def api_update_runtime_settings(
        settings: RuntimeSettingsUpdate,
        request: Request,
        x_dashboard_control_token: str | None = Header(default=None),
    ) -> JSONResponse:
        _authorize_dashboard_control(request, x_dashboard_control_token)
        payload = settings.model_dump() if hasattr(settings, "model_dump") else settings.dict()
        try:
            saved = save_runtime_settings(db, payload)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        db.add_notification(
            "Réglages live modifiés",
            "Réglages effectifs : " + ", ".join(
                f"{key}={value}" for key, value in sorted(saved.items())
            ),
            level="warning",
        )
        logger.warning("Réglages stratégiques live modifiés via le dashboard : %s", saved)
        return JSONResponse({"ok": True, "settings": saved})

    @app.post("/api/control/stop")
    def api_stop_trading(
        request: Request,
        x_dashboard_control_token: str | None = Header(default=None),
    ) -> JSONResponse:
        _authorize_dashboard_control(request, x_dashboard_control_token)
        stop(db)
        db.add_notification(
            "Killswitch activé depuis le dashboard",
            "Les scans et nouvelles tentatives de trading ont été arrêtés.",
            level="warning",
        )
        logger.warning("Killswitch activé via le dashboard.")
        return JSONResponse({"ok": True, "stopped": True})

    @app.get("/api/health")
    def api_health() -> JSONResponse:
        now = time.time()
        arbitrage = db.arbitrage_stats_summary()
        triangular = db.triangular_stats_summary()
        last_scan_state = db.get_state("last_scan_completed_at")
        try:
            last_scan = float(last_scan_state) if last_scan_state else None
        except ValueError:
            logger.exception("Horodatage du dernier scan invalide pour l’API santé.")
            last_scan = None
        if last_scan is None:
            scan_times = [
                value for value in (arbitrage.get("last_scan_at"), triangular.get("last_scan_at"))
                if value is not None
            ]
            last_scan = max(scan_times) if scan_times else None
        age = max(0, int(now - last_scan)) if last_scan else None
        try:
            database_size = db.db_path.stat().st_size
            db.fetch_one("SELECT 1 AS healthy")
            database_ok = True
        except (OSError, sqlite3.Error):
            logger.exception("Vérification santé SQLite échouée.")
            database_size = 0
            database_ok = False
        directional_stats = db.directional_live_stats()
        directional_unresolved = db.directional_unresolved_positions_count()
        directional_open = int(directional_stats.get("open_count") or 0)
        classic_open = len(db.list_open_positions())
        status = "healthy" if database_ok and age is not None and age <= 180 else "stale"
        if not database_ok:
            status = "unhealthy"
        elif directional_unresolved:
            status = "degraded"
        return JSONResponse({
            "status": status,
            "checked_at": now,
            "uptime_seconds": max(0, int(now - _APP_STARTED_AT)),
            "last_scan_at": last_scan,
            "last_scan_age_seconds": age,
            "last_scan_duration_seconds": db.get_state("last_scan_duration_seconds"),
            "database_ok": database_ok,
            "database_size_bytes": database_size,
            "open_positions": classic_open + directional_open,
            "directional_open_positions": directional_open,
            "unresolved_positions": directional_unresolved,
            "killswitch": is_stopped(db),
            "cpu_temperature_celsius": read_temperature_celsius(),
        })

    @app.get("/api/status")
    def api_status() -> JSONResponse:
        stopped = is_stopped(db)
        blockers: list[str] = []
        config_error: str | None = None
        directional_stats = db.directional_live_stats()
        try:
            from app.config import load_config
            from app.trading.guardrails import trading_live_blockers

            blockers = trading_live_blockers(load_config().raw)
        except Exception as exc:
            logger.exception("Impossible de déterminer le mode live de l’API dashboard.")
            config_error = type(exc).__name__
        return JSONResponse({
            "stopped": stopped,
            "mode": "simulation" if stopped or blockers or config_error else "live_authorized",
            "live_blockers": blockers,
            "config_error": config_error,
            "arbitrage": db.arbitrage_stats_summary(),
            "triangular": db.triangular_stats_summary(),
            "live_trading": db.live_trading_stats(),
            "directional_live": directional_stats,
            "open_positions": len(db.list_open_positions()) + int(
                directional_stats.get("open_count") or 0
            ),
            "unresolved_positions": db.directional_unresolved_positions_count(),
        })

    @app.get("/api/notifications")
    def api_notifications(limit: int = 50, unread_only: bool = False) -> JSONResponse:
        return JSONResponse({
            "unread_count": db.count_unread_notifications(),
            "items": db.list_notifications(limit=limit, unread_only=unread_only),
        })

    @app.post("/api/notifications/mark_read")
    def api_notifications_mark_read() -> JSONResponse:
        db.mark_notifications_read()
        return JSONResponse({"ok": True})

    return app
