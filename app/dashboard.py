"""Dashboard : vue texte simple + page HTML + API JSON en lecture seule pour le
bot d'arbitrage inter-DEX."""
from __future__ import annotations

from html import escape

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from app.database import Database
from app.killswitch import is_stopped
from app.trading.executor import EXECUTABLE_QUOTE_TOKENS, _NO_SALE_CLOSED_SENTINEL


def build_dashboard_text(db: Database) -> str:
    stats = db.arbitrage_stats_summary()
    live_stats = db.live_trading_stats()
    cumulative = stats.get("cumulative_net_profit_usd") or 0.0

    lines = [
        "┌──────────────────────────────────────┐",
        "│ BOT D'ARBITRAGE INTER-DEX            │",
        "├──────────────────────────────────────┤",
        f"│ Signaux détectés :     {stats.get('total', 0):>8}      │",
        f"│ Dont rentables :       {stats.get('would_execute_count', 0) or 0:>8}      │",
        f"│ P&L cumulé (simu) :   {cumulative:>8.2f} $   │",
        f"│ Trades réels :         {live_stats.get('total_live', 0) or 0:>8}      │",
        f"│   dont terminés :      {live_stats.get('completed', 0) or 0:>8}      │",
        f"│   dont ouverts :       {live_stats.get('open_count', 0) or 0:>8}      │",
        "└──────────────────────────────────────┘",
    ]
    return "\n".join(lines)


def build_dashboard_html(db: Database) -> str:
    """Page HTML du dashboard : killswitch, panneau de notifications et section
    arbitrage (signaux détectés, rentabilité, trades réels).

    Auto-refresh toutes les 60s (meta refresh) + un panneau de notifications
    alimenté par un fetch JS léger toutes les 15s (alternative à Telegram, cf.
    /api/notifications).
    """
    stopped = is_stopped(db)
    unread_notifications = db.count_unread_notifications()

    killswitch_banner = (
        "<div class='banner stop'>🛑 Killswitch actif : scans et exécutions automatiques coupés.</div>"
        if stopped else ""
    )

    arb_stats = db.arbitrage_stats_summary()
    arb_signals = db.list_arbitrage_signals(limit=20)

    try:
        from app.config import load_config
        from app.trading.guardrails import is_trading_live
        trading_live = is_trading_live(load_config())
    except Exception:  # noqa: BLE001 - le dashboard ne doit jamais planter pour cette info
        trading_live = False

    if trading_live:
        live_stats = db.live_trading_stats()
        mode_title = "📈 Bot de trading — arbitrage inter-DEX (LIVE 🔴 argent réel)"
        mode_subtitle = (
            "Mode <b>LIVE</b> : les transactions rentables sont réellement envoyées avec le wallet de "
            f"trading. {arb_stats.get('total', 0)} signal(aux) détecté(s), "
            f"{live_stats.get('total_live', 0) or 0} round-trip(s) réel(s) tenté(s) "
            f"({live_stats.get('completed', 0) or 0} clôturé(s), "
            f"{live_stats.get('open_count', 0) or 0} position(s) en attente de clôture)."
        )
    else:
        mode_title = "📈 Bot de trading — arbitrage inter-DEX (SIMULATION)"
        mode_subtitle = None  # calculé plus bas selon la présence de signaux

    if arb_signals:
        arb_rows = []
        for s in arb_signals:
            net = f"{s['net_profit_usd']:.3f} $" if s["net_profit_usd"] is not None else "N/A"
            # `would_execute` (calculé dans app.trading.arbitrage) ne tient
            # compte QUE de la rentabilité après frais/slippage/gas — il
            # ignore si le quote token est réellement exécutable (seuls
            # USDC/WETH le sont, voir EXECUTABLE_QUOTE_TOKENS). Le scheduler
            # filtre ces signaux en silence (aucune transaction jamais
            # tentée) : sans distinction ici, le badge "rentable" était
            # trompeur pour des paires comme WETH/cbETH qui ne seront jamais
            # exécutées (bug de confusion observé en conditions réelles).
            quote_not_executable = (s.get("quote_address") or "").lower() not in EXECUTABLE_QUOTE_TOKENS
            if s["would_execute"] and quote_not_executable:
                tag = (
                    "<span class='badge' style='background:#a16207' "
                    "title=\"Rentable en théorie mais jamais exécuté : quote token non supporté "
                    "(seuls USDC/WETH le sont).\">rentable (quote non exécutable)</span>"
                )
            elif s["would_execute"]:
                tag = "<span class='badge' style='background:#15803d'>rentable</span>"
            else:
                tag = "<span class='badge' style='background:#475569'>non rentable</span>"
            exec_tag = ""
            if s.get("mode") == "live" and s.get("tx_hash_buy"):
                tx_sell = s.get("tx_hash_sell")
                if not tx_sell:
                    exec_tag = " <span class='badge' style='background:#b91c1c'>exécuté réel</span>"
                elif tx_sell == _NO_SALE_CLOSED_SENTINEL:
                    exec_tag = " <span class='badge' style='background:#92400e'>close (solde nul, pas de vente)</span>"
                else:
                    exec_tag = " <span class='badge' style='background:#166534'>round-trip réel complet</span>"
            flashloan_notional = s.get("flashloan_notional_usd")
            flashloan_net = s.get("flashloan_net_profit_usd")
            if flashloan_notional is not None and flashloan_net is not None:
                flashloan_cell = (
                    f"<span class='badge' style='background:#6d28d9'>⚡ {flashloan_notional:.0f} $</span> "
                    f"{flashloan_net:.3f} $"
                )
            else:
                flashloan_cell = "<span class='empty'>—</span>"
            arb_rows.append(
                f"<tr><td>{escape(s['token_symbol'])}/{escape(s['quote_symbol'])}</td>"
                f"<td>{escape(s['buy_dex'])} → {escape(s['sell_dex'])}</td>"
                f"<td>{s['spread_pct']:.2f}%</td><td>{s['trade_size_usd']:.2f} $</td>"
                f"<td>{net}</td><td>{flashloan_cell}</td><td>{tag}{exec_tag}</td></tr>"
            )
        cumulative = arb_stats.get("cumulative_net_profit_usd") or 0.0
        if mode_subtitle is None:
            mode_subtitle = (
                "Mode <b>SIMULATION</b> : aucune transaction réelle n'est envoyée. "
                f"{arb_stats.get('total', 0)} signal(aux) détecté(s), "
                f"{arb_stats.get('would_execute_count', 0) or 0} jugé(s) rentable(s) après frais+slippage+gas. "
                f"P&amp;L hypothétique cumulé : {cumulative:.2f} $ (si exécuté à chaque signal rentable)."
            )
        arbitrage_section = (
            "<section>"
            f"<h2>{mode_title}</h2>"
            f"<p class='subtitle'>{mode_subtitle}</p>"
            "<table><tr><th>Paire</th><th>Achat → Vente</th><th>Spread</th><th>Taille</th>"
            "<th>Profit net est.</th><th>⚡ Potentiel flashloan</th><th></th></tr>"
            + "".join(arb_rows) + "</table>"
            "</section>"
        )
    else:
        if mode_subtitle is None:
            mode_subtitle = (
                "Aucun signal détecté pour l'instant (scan en cours toutes les quelques "
                "minutes). Mode SIMULATION : aucune transaction réelle ne sera jamais envoyée sans validation "
                "manuelle explicite d'une phase live séparée."
            )
        arbitrage_section = (
            "<section>"
            f"<h2>{mode_title}</h2>"
            f"<p class='subtitle'>{mode_subtitle}</p>"
            "</section>"
        )

    cumulative_card = arb_stats.get("cumulative_net_profit_usd") or 0.0

    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="60">
<title>Bot d'arbitrage</title>
<style>
  body {{ font-family: system-ui, sans-serif; background:#0f1115; color:#e2e8f0; margin:0; padding:24px; }}
  h1 {{ font-size:22px; margin-bottom:4px; }}
  .subtitle {{ color:#94a3b8; margin-bottom:20px; font-size:13px; }}
  .cards {{ display:flex; gap:16px; flex-wrap:wrap; margin-bottom:24px; }}
  .card {{ background:#1a1d24; border-radius:10px; padding:14px 18px; min-width:160px; }}
  .card .label {{ color:#94a3b8; font-size:12px; text-transform:uppercase; }}
  .card .value {{ font-size:22px; font-weight:600; margin-top:4px; }}
  table {{ width:100%; border-collapse:collapse; background:#1a1d24; border-radius:10px; overflow:hidden; }}
  th, td {{ padding:8px 12px; text-align:left; font-size:13px; border-bottom:1px solid #2d323d; }}
  th {{ color:#94a3b8; font-weight:600; text-transform:uppercase; font-size:11px; }}
  tr:last-child td {{ border-bottom:none; }}
  .badge {{ padding:2px 8px; border-radius:999px; font-size:11px; color:#fff; }}
  a {{ color:#60a5fa; }}
  section {{ margin-bottom:28px; }}
  .banner {{ padding:10px 16px; border-radius:8px; margin-bottom:16px; font-weight:600; }}
  .banner.stop {{ background:#7f1d1d; color:#fecaca; }}
  .empty {{ color:#64748b; font-size:13px; padding:12px; }}
  .topbar {{ display:flex; justify-content:space-between; align-items:flex-start; }}
  .bell {{ position:relative; background:#1a1d24; border:none; color:#e2e8f0; border-radius:8px;
           padding:10px 14px; font-size:18px; cursor:pointer; }}
  .bell .dot {{ position:absolute; top:4px; right:4px; background:#c53030; color:#fff; border-radius:999px;
                font-size:10px; font-weight:700; padding:1px 5px; display:none; }}
  .bell.unread .dot {{ display:inline-block; }}
  .notif-panel {{ display:none; position:absolute; right:24px; top:70px; width:360px; max-height:420px;
                  overflow-y:auto; background:#1a1d24; border:1px solid #2d323d; border-radius:10px;
                  padding:8px; z-index:10; box-shadow:0 8px 24px rgba(0,0,0,.4); }}
  .notif-panel.open {{ display:block; }}
  .notif-item {{ padding:8px 10px; border-bottom:1px solid #2d323d; font-size:13px; }}
  .notif-item:last-child {{ border-bottom:none; }}
  .notif-item .meta {{ color:#64748b; font-size:11px; margin-top:2px; }}
  .notif-item.level-alert {{ border-left:3px solid #c53030; }}
  .notif-item.level-warning {{ border-left:3px solid #b7791f; }}
  .notif-item.level-info {{ border-left:3px solid #2563eb; }}
  code {{ font-size:12px; background:#0f1115; padding:2px 6px; border-radius:4px; }}
</style>
</head>
<body>
<div class="topbar">
  <div>
    <h1>🤖 Bot d'arbitrage inter-DEX</h1>
    <div class="subtitle">Actualisation automatique toutes les 60s · {
        '🔴 trading réel actif (wallet dédié)' if trading_live else 'simulation · lecture seule'
    }</div>
  </div>
  <div style="position:relative">
    <button class="bell{' unread' if unread_notifications else ''}" id="notif-bell" onclick="toggleNotifPanel()">
      🔔<span class="dot" id="notif-count">{unread_notifications}</span>
    </button>
    <div class="notif-panel" id="notif-panel">
      <div id="notif-list" class="empty">Chargement…</div>
    </div>
  </div>
</div>
{killswitch_banner}
<div class="cards">
  <div class="card"><div class="label">Signaux détectés</div><div class="value">{arb_stats.get('total', 0)}</div></div>
  <div class="card"><div class="label">Dont rentables</div><div class="value">{arb_stats.get('would_execute_count', 0) or 0}</div></div>
  <div class="card"><div class="label">P&amp;L cumulé (simu)</div><div class="value">{cumulative_card:.2f} $</div></div>
</div>

{arbitrage_section}

<section>
  <p class="subtitle">API JSON disponible : <a href="/api/status">/api/status</a> ·
  <a href="/api/notifications">/api/notifications</a> ·
  <a href="/api/arbitrage">/api/arbitrage</a></p>
</section>
<script>
  // Panneau de notifications web (alternative à Telegram) : poll léger toutes les 15s,
  // sans dépendance externe, compatible avec un accès LAN simple (http://, pas besoin de HTTPS).
  function toggleNotifPanel() {{
    var panel = document.getElementById('notif-panel');
    var opening = !panel.classList.contains('open');
    panel.classList.toggle('open');
    if (opening) {{
      loadNotifications();
      fetch('/api/notifications/mark_read', {{ method: 'POST' }})
        .then(function () {{ updateBell(0); }})
        .catch(function () {{}});
    }}
  }}

  function updateBell(unreadCount) {{
    var bell = document.getElementById('notif-bell');
    var count = document.getElementById('notif-count');
    count.textContent = unreadCount;
    bell.classList.toggle('unread', unreadCount > 0);
  }}

  function escapeHtml(str) {{
    var div = document.createElement('div');
    div.textContent = str || '';
    return div.innerHTML;
  }}

  function loadNotifications() {{
    fetch('/api/notifications?limit=30')
      .then(function (r) {{ return r.json(); }})
      .then(function (data) {{
        var list = document.getElementById('notif-list');
        if (!data.items || data.items.length === 0) {{
          list.className = 'empty';
          list.textContent = 'Aucune notification pour le moment.';
          return;
        }}
        list.className = '';
        list.innerHTML = data.items.map(function (n) {{
          var date = new Date(n.created_at * 1000).toLocaleString('fr-FR');
          return '<div class="notif-item level-' + escapeHtml(n.level) + '">'
            + '<div>' + escapeHtml(n.title) + '</div>'
            + '<div class="meta">' + date + '</div>'
            + '</div>';
        }}).join('');
      }})
      .catch(function () {{}});
  }}

  function pollUnreadCount() {{
    fetch('/api/notifications?limit=1')
      .then(function (r) {{ return r.json(); }})
      .then(function (data) {{ updateBell(data.unread_count || 0); }})
      .catch(function () {{}});
  }}

  setInterval(pollUnreadCount, 15000);
</script>
</body>
</html>"""


def create_app(db: Database) -> FastAPI:
    app = FastAPI(title="Bot d'arbitrage — dashboard")

    @app.get("/", response_class=HTMLResponse)
    def dashboard() -> str:
        return build_dashboard_html(db)

    @app.get("/text", response_class=HTMLResponse)
    def dashboard_text() -> str:
        text = build_dashboard_text(db)
        return f"<html><body><pre style='font-size:16px'>{text}</pre></body></html>"

    @app.get("/api/arbitrage")
    def api_arbitrage() -> JSONResponse:
        return JSONResponse({
            "stats": db.arbitrage_stats_summary(),
            "signals": db.list_arbitrage_signals(limit=100),
        })

    @app.get("/api/triangular")
    def api_triangular() -> JSONResponse:
        return JSONResponse({
            "signals": db.list_triangular_signals(limit=100),
        })

    @app.get("/api/cross-dex-triangular")
    def api_cross_dex_triangular() -> JSONResponse:
        """Cycles triangulaires inter-DEX détectés — voir
        `app.trading.triangular.run_cross_dex_triangular_scan`. Purement
        informationnel : ces cycles ne sont jamais exécutables automatiquement
        (chaque jambe peut être sur un DEX différent, aucune transaction
        atomique ne peut les router toutes en une fois sans redéployer
        `FlashArbitrage.sol`). Exposé ici pour inspection manuelle uniquement."""
        return JSONResponse({
            "signals": db.list_cross_dex_triangular_signals(limit=100),
        })

    @app.get("/api/status")
    def api_status() -> JSONResponse:
        return JSONResponse({
            "stopped": is_stopped(db),
            "arbitrage": db.arbitrage_stats_summary(),
            "live_trading": db.live_trading_stats(),
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
