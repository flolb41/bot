"""Suivi des points/XP par projet (app/trackers/points.py)."""
from __future__ import annotations

from app.database import Database


def record_points(db: Database, project_id: str, points: float) -> float | None:
    """Enregistre un nouveau relevé de points et retourne le delta vs le relevé précédent."""
    previous = db.last_points(project_id)
    db.record_points(project_id, points)
    if previous is None:
        return None
    return points - previous["points"]


def points_history(db: Database, project_id: str, limit: int = 30) -> list[dict]:
    return db.fetch_all(
        "SELECT * FROM points_history WHERE project_id = ? ORDER BY recorded_at DESC LIMIT ?",
        (project_id, limit),
    )
