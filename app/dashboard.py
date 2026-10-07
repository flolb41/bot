"""Dashboard (section 10 du TODO) : vue texte simple + page HTML + API JSON en lecture seule."""
from __future__ import annotations

from html import escape

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from app.database import Database
from app.killswitch import is_stopped
from app.scoring import score_from_project_row
from app.trackers.deadlines import upcoming_deadlines
from app.trackers.rewards import rewards_summary
from app.wallet.autosign import autosign_enabled, contract_whitelist, max_tx_per_day, max_value_native
from app.wallet.balances import get_all_balances
from app.wallet.signer import get_autosigner_address

_EMOJI_BY_CLASS = {
    "PRIORITÉ CRITIQUE": "🔴",
    "PRIORITÉ HAUTE": "🟠",
    "À SURVEILLER": "🟡",
    "FAIBLE": "⚪",
    "IGNORER": "⚫",
}


def build_dashboard_text(db: Database) -> str:
    summary = rewards_summary(db)
    projects = sorted(db.list_projects(), key=lambda p: p["score"], reverse=True)[:5]

    total_received = sum(summary["by_token"].values())
    gas = summary["total_gas_spent"]

    lines = [
        "┌──────────────────────────────────────┐",
        "│ CRYPTO REWARD HUNTER                 │",
        "├──────────────────────────────────────┤",
        f"│ Capital investi :       0.00 €       │",
        f"│ Rewards reçus :    {total_received:>8.2f}           │",
        f"│ Rewards en attente :     {summary['nb_pending']:>3}           │",
        f"│ Gas dépensé :      {gas:>8.4f}           │",
        "├──────────────────────────────────────┤",
        "│ PRIORITÉS                             │",
        "│                                      │",
    ]
    for p in projects:
        breakdown = score_from_project_row(p)
        emoji = _EMOJI_BY_CLASS.get(breakdown.classify().value, "⚪")
        lines.append(f"│ {emoji} {p['name']:<17} {p['score']:>3}/100           │")
    lines.append("└──────────────────────────────────────┘")
    return "\n".join(lines)


_PASSIVE_TASK_KEYWORDS = (
    "surveiller",
    "suivre ",
    "suivre l'",
    "vérifier la date",
    "vérifier les conditions de distribution",
    "vérifier la future distribution",
    "vérifier les échéances",
)


def _is_passive_monitoring(title: str) -> bool:
    """Tâches de simple veille déjà couvertes par le scan auto + les notifications.

    Ces tâches ("surveiller X", "suivre Y") ne demandent aucune action humaine :
    le bot détecte les changements tout seul et notifie. On les masque par défaut
    du tableau "actions à faire" pour ne garder que ce qui nécessite réellement
    un clic/une inscription de ta part.
    """
    lowered = title.lower()
    return any(keyword in lowered for keyword in _PASSIVE_TASK_KEYWORDS)


def _status_badge(status: str) -> str:
    colors = {
        "actif": "#1f9d55", "testnet": "#1f9d55", "campagne_claim": "#1f9d55",
        "a_preparer": "#b7791f", "en_attente": "#b7791f",
        "ferme": "#a0a0a0", "suspect": "#c53030",
    }
    color = colors.get(status, "#718096")
    return f"<span class='badge' style='background:{color}'>{escape(status or '—')}</span>"


def build_dashboard_html(db: Database) -> str:
    """Page HTML du dashboard : tableau des projets, tâches, deadlines et rewards.

    Auto-refresh toutes les 60s (meta refresh, pas de JS requis pour les données
    principales) + un panneau de notifications alimenté par un fetch JS léger
    toutes les 15s (alternative à Telegram, cf. /api/notifications).
    """
    projects = sorted(db.list_projects(), key=lambda p: p["score"], reverse=True)
    summary = rewards_summary(db)
    deadlines = upcoming_deadlines(db)
    pending_tasks = db.list_tasks(status="pending")
    stopped = is_stopped(db)
    unread_notifications = db.count_unread_notifications()

    project_names = {p["id"]: p["name"] for p in projects}
    actionable_tasks = [t for t in pending_tasks if not _is_passive_monitoring(t["title"])]
    passive_tasks = [t for t in pending_tasks if _is_passive_monitoring(t["title"])]

    def _task_row(t: dict) -> str:
        url = t.get("url") or ""
        link = f"<a href='{escape(url)}' target='_blank' rel='noopener'>lien</a>" if url else "—"
        return (
            "<tr>"
            f"<td>{escape(project_names.get(t['project_id'], t['project_id']))}</td>"
            f"<td>{escape(t['title'])}</td>"
            f"<td>{escape(t.get('difficulty') or '—')}</td>"
            f"<td>{escape(t.get('reward_type') or '—')}</td>"
            f"<td>{link}</td>"
            "</tr>"
        )

    actionable_task_rows = "".join(_task_row(t) for t in actionable_tasks)
    passive_task_rows = "".join(_task_row(t) for t in passive_tasks)

    rows = []
    for p in projects:
        breakdown = score_from_project_row(p)
        emoji = _EMOJI_BY_CLASS.get(breakdown.classify().value, "⚪")
        official_url = escape(p.get("official_url") or "")
        link = f"<a href='{official_url}' target='_blank' rel='noopener'>lien</a>" if official_url else "—"
        rows.append(
            "<tr>"
            f"<td>{emoji} {p['score']}/100</td>"
            f"<td>{escape(p['name'])}</td>"
            f"<td>{escape(p.get('network') or '—')}</td>"
            f"<td>{_status_badge(p.get('status'))}</td>"
            f"<td>{'⚠️ oui' if p.get('requires_kyc') else 'non'}</td>"
            f"<td>{'⚠️ oui' if p.get('requires_capital') else 'non'}</td>"
            f"<td>{link}</td>"
            "</tr>"
        )

    deadline_rows = []
    for d in deadlines:
        flag = "🔴 dépassée" if d["expired"] else "🟠 proche"
        deadline_rows.append(
            f"<tr><td>{flag}</td><td>{escape(d['scope'])}</td>"
            f"<td>{escape(str(d['name']))}</td><td>{escape(str(d['deadline']))}</td></tr>"
        )

    wallet_rows = []
    for w in get_all_balances(db.list_wallets()):
        balance_display = f"{w['balance']:.6f}" if w.get("error") is None else f"⚠️ {escape(w['error'])}"
        wallet_rows.append(
            f"<tr><td>{escape(w['name'])}</td><td>{escape(w['network'])}</td>"
            f"<td><code>{escape(w['address'])}</code></td><td>{balance_display}</td></tr>"
        )

    autosigner_address = get_autosigner_address()
    autosign_status_badge = "🟢 activée" if autosign_enabled() else "⚪ désactivée (par défaut)"
    autosign_section = (
        "<section>"
        "<h2>Auto-signature (Phase 4 — lecture seule)</h2>"
        "<table>"
        "<tr><th>Statut</th><th>Wallet</th><th>Whitelist contrats</th>"
        "<th>Max/tx</th><th>Max tx/jour</th></tr>"
        "<tr>"
        f"<td>{autosign_status_badge}</td>"
        f"<td><code>{escape(autosigner_address or '— non créé —')}</code></td>"
        f"<td>{len(contract_whitelist())} contrat(s)</td>"
        f"<td>{max_value_native()}</td>"
        f"<td>{max_tx_per_day()}</td>"
        "</tr>"
        "</table>"
        "<p class='subtitle'>Activation/désactivation uniquement via le .env (AUTOSIGN_ENABLED) — "
        "aucun bouton web, par sécurité.</p>"
        "</section>"
    )

    killswitch_banner = (
        "<div class='banner stop'>🛑 Killswitch actif : scans et notifications automatiques coupés.</div>"
        if stopped else ""
    )

    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="60">
<title>Crypto Reward Hunter</title>
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
    <h1>🏹 Crypto Reward Hunter</h1>
    <div class="subtitle">Actualisation automatique toutes les 60s · 0€ de capital · lecture seule</div>
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
  <div class="card"><div class="label">Capital investi</div><div class="value">0.00 €</div></div>
  <div class="card"><div class="label">Rewards reçus</div><div class="value">{sum(summary['by_token'].values()):.2f}</div></div>
  <div class="card"><div class="label">Rewards en attente</div><div class="value">{summary['nb_pending']}</div></div>
  <div class="card"><div class="label">Gas dépensé</div><div class="value">{summary['total_gas_spent']:.4f}</div></div>
  <div class="card"><div class="label">Tâches en attente</div><div class="value">{len(pending_tasks)}</div></div>
</div>

<section>
  <h2>Projets suivis ({len(projects)})</h2>
  <table>
    <tr><th>Score</th><th>Projet</th><th>Réseau</th><th>Statut</th><th>KYC</th><th>Dépôt requis</th><th>Site</th></tr>
    {''.join(rows) if rows else "<tr><td class='empty' colspan='7'>Aucun projet (lance `python main.py seed`).</td></tr>"}
  </table>
</section>

<section>
  <h2>Actions concrètes à faire ({len(actionable_tasks)})</h2>
  <div class="subtitle">Tâches nécessitant une action humaine réelle (inscription, quête, claim…).
  Les tâches de simple veille sont masquées (voir ci-dessous) : le bot les surveille tout seul.</div>
  <table>
    <tr><th>Projet</th><th>Tâche</th><th>Difficulté</th><th>Récompense</th><th>Lien</th></tr>
    {actionable_task_rows if actionable_task_rows else "<tr><td class='empty' colspan='5'>Aucune action en attente 🎉</td></tr>"}
  </table>
  <details style="margin-top:12px">
    <summary style="cursor:pointer;color:#94a3b8;font-size:13px">
      Voir aussi les {len(passive_tasks)} tâches de surveillance passive (gérées automatiquement par le bot)
    </summary>
    <table style="margin-top:8px">
      <tr><th>Projet</th><th>Tâche</th><th>Difficulté</th><th>Récompense</th><th>Lien</th></tr>
      {passive_task_rows if passive_task_rows else "<tr><td class='empty' colspan='5'>Aucune.</td></tr>"}
    </table>
  </details>
</section>

<section>
  <h2>Échéances à venir (48h)</h2>
  <table>
    <tr><th>Urgence</th><th>Type</th><th>Nom</th><th>Deadline</th></tr>
    {''.join(deadline_rows) if deadline_rows else "<tr><td class='empty' colspan='4'>Aucune échéance proche.</td></tr>"}
  </table>
</section>

<section>
  <h2>Wallets (lecture seule)</h2>
  <table>
    <tr><th>Nom</th><th>Réseau</th><th>Adresse</th><th>Balance native</th></tr>
    {''.join(wallet_rows) if wallet_rows else "<tr><td class='empty' colspan='4'>Aucun wallet configuré (renseigne WALLET_FARM_EVM_ADDRESS dans le .env).</td></tr>"}
  </table>
</section>

{autosign_section}

<section>
  <p class="subtitle">API JSON disponible : <a href="/api/projects">/api/projects</a> ·
  <a href="/api/rewards">/api/rewards</a> · <a href="/api/wallets">/api/wallets</a> ·
  <a href="/api/status">/api/status</a> · <a href="/api/notifications">/api/notifications</a></p>
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
    app = FastAPI(title="Crypto Reward Hunter Dashboard")

    @app.get("/", response_class=HTMLResponse)
    def dashboard() -> str:
        return build_dashboard_html(db)

    @app.get("/text", response_class=HTMLResponse)
    def dashboard_text() -> str:
        text = build_dashboard_text(db)
        return f"<html><body><pre style='font-size:16px'>{text}</pre></body></html>"

    @app.get("/api/projects")
    def api_projects() -> JSONResponse:
        return JSONResponse(db.list_projects())

    @app.get("/api/rewards")
    def api_rewards() -> JSONResponse:
        return JSONResponse(rewards_summary(db))

    @app.get("/api/wallets")
    def api_wallets() -> JSONResponse:
        return JSONResponse(get_all_balances(db.list_wallets()))

    @app.get("/api/autosign/status")
    def api_autosign_status() -> JSONResponse:
        return JSONResponse({
            "enabled": autosign_enabled(),
            "wallet_address": get_autosigner_address(),
            "contract_whitelist_count": len(contract_whitelist()),
            "max_value_native": max_value_native(),
            "max_tx_per_day": max_tx_per_day(),
        })

    @app.get("/api/status")
    def api_status() -> JSONResponse:
        return JSONResponse({
            "stopped": is_stopped(db),
            "nb_projects": len(db.list_projects()),
            "nb_tasks_pending": len(db.list_tasks(status="pending")),
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
