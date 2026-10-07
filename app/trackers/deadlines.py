"""Suivi des échéances (app/trackers/deadlines.py)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.database import Database

DEFAULT_WARNING_WINDOW_HOURS = 48


def _parse_deadline(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def upcoming_deadlines(db: Database, warning_hours: int = DEFAULT_WARNING_WINDOW_HOURS) -> list[dict]:
    """Retourne les projets/tâches dont la deadline approche (ou est dépassée)."""
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(hours=warning_hours)

    results: list[dict] = []
    for project in db.list_projects():
        deadline = _parse_deadline(project.get("deadline"))
        if deadline and deadline <= horizon:
            results.append({
                "scope": "project",
                "id": project["id"],
                "name": project["name"],
                "deadline": project["deadline"],
                "expired": deadline < now,
            })

    for task in db.list_tasks(status="pending"):
        deadline = _parse_deadline(task.get("deadline"))
        if deadline and deadline <= horizon:
            results.append({
                "scope": "task",
                "id": task["id"],
                "name": task["title"],
                "deadline": task["deadline"],
                "expired": deadline < now,
            })

    results.sort(key=lambda r: r["deadline"] or "")
    return results
