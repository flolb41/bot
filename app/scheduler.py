"""Scheduler (section 8 Phase 1 et section 13 du TODO).

Orchestre les scans périodiques, la détection des échéances et le digest
quotidien. Respecte le killswitch : si actif, aucun job ne s'exécute.
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.database import Database
from app.killswitch import is_stopped
from app.projects import all_projects
from app.scoring import score_from_project_row
from app.trackers.deadlines import upcoming_deadlines

logger = logging.getLogger("app.scheduler")


def seed_initial_data(db: Database) -> None:
    """Insère/MAJ les projets du registre et leurs tâches par défaut (idempotent)."""
    for project in all_projects():
        db.upsert_project(project.to_project_row())
        existing_tasks = db.list_tasks(project_id=project.id)
        if not existing_tasks:
            for task in project.default_tasks():
                db.add_task(task)
        logger.info("Projet initialisé: %s", project.id)


def run_scan_once(db: Database, telegram=None) -> list[dict]:
    """Exécute un cycle de scan complet : fetch + hash + score + notifications de changement."""
    if is_stopped(db):
        logger.info("Killswitch actif : scan ignoré.")
        return []

    changes: list[dict] = []
    for project in all_projects():
        result = project.scan()
        changed = db.update_project_scan_result(project.id, result.content_hash)
        row = db.get_project(project.id) or project.to_project_row()
        breakdown = score_from_project_row(row)
        db.update_project_score(project.id, breakdown.total)

        if changed:
            changes.append({"project_id": project.id, "name": project.name})
            if telegram:
                telegram.send(
                    f"🔔 Changement détecté sur <b>{project.name}</b>. "
                    f"Vérifie la source officielle : {project.official_url}"
                )
        if not result.reachable and result.error:
            logger.warning("Projet %s injoignable: %s", project.id, result.error)

    logger.info("Scan terminé : %d changement(s) détecté(s).", len(changes))
    return changes


def run_deadline_check(db: Database, telegram=None) -> None:
    if is_stopped(db):
        return
    items = upcoming_deadlines(db)
    if items and telegram:
        lines = [f"⏰ {i['name']} — {i['deadline']}" for i in items]
        telegram.send("📅 <b>Échéances à surveiller (48h)</b>\n" + "\n".join(lines))


def run_daily_digest(db: Database, telegram=None) -> None:
    if is_stopped(db) or not telegram:
        return
    telegram.cmd_today()


def build_scheduler(
    db: Database,
    telegram=None,
    scan_interval_minutes: int = 30,
    deadline_check_hour: int = 8,
    digest_hour: int = 9,
) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone="UTC")

    scheduler.add_job(
        run_scan_once, IntervalTrigger(minutes=scan_interval_minutes),
        args=[db, telegram], id="scan", replace_existing=True,
    )
    scheduler.add_job(
        run_deadline_check, CronTrigger(hour=deadline_check_hour, minute=0),
        args=[db, telegram], id="deadline_check", replace_existing=True,
    )
    scheduler.add_job(
        run_daily_digest, CronTrigger(hour=digest_hour, minute=0),
        args=[db, telegram], id="daily_digest", replace_existing=True,
    )
    return scheduler
