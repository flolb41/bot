"""Dashboard (section 10 du TODO) : vue texte simple + API JSON en lecture seule."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from app.database import Database
from app.scoring import score_from_project_row
from app.trackers.rewards import rewards_summary


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
    emojis = {"PRIORITÉ CRITIQUE": "🔴", "PRIORITÉ HAUTE": "🟠", "À SURVEILLER": "🟡"}
    for p in projects:
        breakdown = score_from_project_row(p)
        emoji = emojis.get(breakdown.classify().value, "⚪")
        lines.append(f"│ {emoji} {p['name']:<17} {p['score']:>3}/100           │")
    lines.append("└──────────────────────────────────────┘")
    return "\n".join(lines)


def create_app(db: Database) -> FastAPI:
    app = FastAPI(title="Crypto Reward Hunter Dashboard")

    @app.get("/", response_class=HTMLResponse)
    def dashboard() -> str:
        text = build_dashboard_text(db)
        return f"<html><body><pre style='font-size:16px'>{text}</pre></body></html>"

    @app.get("/api/projects")
    def api_projects() -> JSONResponse:
        return JSONResponse(db.list_projects())

    @app.get("/api/rewards")
    def api_rewards() -> JSONResponse:
        return JSONResponse(rewards_summary(db))

    @app.get("/api/status")
    def api_status() -> JSONResponse:
        from app.killswitch import is_stopped
        return JSONResponse({
            "stopped": is_stopped(db),
            "nb_projects": len(db.list_projects()),
            "nb_tasks_pending": len(db.list_tasks(status="pending")),
        })

    return app
