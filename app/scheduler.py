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
from app.discovery.auto_projects import auto_approved_projects
from app.discovery.scanner import run_discovery as _run_discovery_scan
from app.killswitch import is_stopped
from app.projects import all_projects
from app.scoring import score_from_project_row
from app.trackers.deadlines import upcoming_deadlines
from app.trackers.opportunities import scan_opportunities
from app.trading.arbitrage import run_arbitrage_scan

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


def seed_wallets(db: Database, config) -> None:
    """Enregistre en base les wallets déclarés dans la config (adresses publiques uniquement).

    Aucune seed phrase ni private key ne transite jamais ici : seules les
    entrées `address` (publiques) de la section `wallets` du config.yaml,
    elles-mêmes injectées via des variables d'environnement, sont utilisées.
    """
    for wallet in config.get("wallets", []) or []:
        address = (wallet.get("address") or "").strip()
        if not address:
            # Pas d'adresse configurée pour ce wallet : on ignore silencieusement
            # (évite de polluer la base avec des wallets vides tant que l'utilisateur
            # n'a pas créé et renseigné son wallet FARMING dédié).
            continue
        db.upsert_wallet(
            name=wallet.get("name", "wallet"),
            address=address,
            network=wallet.get("network", ""),
            purpose=wallet.get("purpose", ""),
        )
        logger.info("Wallet enregistré (adresse publique uniquement): %s", wallet.get("name"))


def run_scan_once(db: Database, telegram=None) -> list[dict]:
    """Exécute un cycle de scan complet : fetch + hash + score + notifications de changement."""
    if is_stopped(db):
        logger.info("Killswitch actif : scan ignoré.")
        return []

    changes: list[dict] = []
    # Les projets auto-approuvés par la découverte (surveillance passive
    # uniquement, voir app/discovery/auto_projects.py) sont scannés exactement
    # comme les 11 projets curés, via le même BaseProject.scan() générique.
    for project in all_projects() + auto_approved_projects(db):
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
                    f"Vérifie la source officielle : {project.official_url}",
                    level="alert", project_id=project.id,
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
        telegram.send("📅 <b>Échéances à surveiller (48h)</b>\n" + "\n".join(lines), level="warning")


def run_daily_digest(db: Database, telegram=None) -> None:
    if is_stopped(db) or not telegram:
        return
    telegram.cmd_today()


def run_opportunity_scan(db: Database, telegram=None) -> list[dict]:
    """Scanne les micro-récompenses auto-réclamables (section 16 étendue, voir
    app/trackers/opportunities.py) et notifie des claims effectués."""
    if is_stopped(db):
        return []
    results = scan_opportunities(db)
    claimed = [r for r in results if r["claimed"]]
    if claimed and telegram:
        lines = [f"✅ {r['project_name']} : ~{r['value_eur']:.4f} € réclamés" for r in claimed]
        telegram.send("💰 <b>Micro-récompenses réclamées automatiquement</b>\n" + "\n".join(lines), level="info")
    return results


def run_project_discovery(db: Database, telegram=None) -> list[dict]:
    """Détecte de nouveaux projets potentiels via des sources publiques (option 2
    du scanner de micro-récompenses, voir app/discovery/). N'ajoute jamais un
    projet au suivi actif automatiquement : validation humaine obligatoire."""
    if is_stopped(db):
        return []
    newly_found = _run_discovery_scan(db)
    if newly_found and telegram:
        lines = [f"• [{e['source']}] {e['title']}" for e in newly_found[:5]]
        telegram.send(
            f"🔭 <b>{len(newly_found)} nouveau(x) projet(s) potentiel(s) détecté(s)</b> "
            "(à valider manuellement sur le dashboard)\n" + "\n".join(lines),
            level="info",
        )
    return newly_found


# Nombre maximum de round-trips réellement exécutés par cycle de scan (cap
# conservateur volontairement bas, EN PLUS du cap journalier déjà imposé par
# `app.trading.guardrails.max_tx_per_day` — jamais tout exécuter d'un coup
# même si plusieurs signaux rentables apparaissent dans le même cycle).
_MAX_LIVE_EXECUTIONS_PER_CYCLE = 1


def run_arbitrage_scan_job(db: Database, telegram=None, config: dict | None = None) -> list[dict]:
    """Scanne les écarts de prix inter-DEX (bot de trading — voir
    app/trading/arbitrage.py). Les signaux rentables (would_execute=True) sont
    toujours notifiés. Si et SEULEMENT SI le trading live est activé (tous les
    garde-fous de `app.trading.guardrails.is_trading_live` réunis) :
    1. Tente d'abord de clôturer d'éventuelles positions restées ouvertes d'un
       cycle précédent (`app.trading.executor.recover_open_positions`) — priorité
       à la fermeture de l'exposition existante avant d'en ouvrir une nouvelle.
    2. Tente ensuite d'exécuter réellement au maximum `_MAX_LIVE_EXECUTIONS_PER_CYCLE`
       nouveau(x) round-trip(s) via `app.trading.executor.execute_signal`.
    Sinon reste purement informatif, comme avant (aucune transaction envoyée)."""
    if is_stopped(db):
        return []

    from app.trading.guardrails import is_trading_live
    live = is_trading_live(config or {})

    if live:
        from app.trading.executor import recover_open_positions
        recovery_results = recover_open_positions(db, config or {})
        if telegram:
            for r in recovery_results:
                if r["recovered"]:
                    telegram.send(
                        f"✅ <b>Position résiduelle clôturée</b> ({r['token_symbol']}) — tx {r['tx_hash_sell']}",
                        level="info",
                    )
                elif r["error"]:
                    telegram.send(
                        f"⚠️ <b>Récupération de position résiduelle en attente</b> "
                        f"({r['token_symbol']}) : {r['error']}",
                        level="warning",
                    )

    signals = run_arbitrage_scan(db, config)
    profitable = [s for s in signals if s["would_execute"]]

    executed_count = 0
    if live:
        from app.trading.executor import execute_signal
        for signal in profitable:
            if executed_count >= _MAX_LIVE_EXECUTIONS_PER_CYCLE:
                break
            exec_result = execute_signal(db, config, signal)
            if exec_result["executed"]:
                executed_count += 1
            elif telegram and exec_result["error"]:
                telegram.send(
                    f"⚠️ <b>Exécution d'arbitrage refusée/échouée</b> "
                    f"({signal['token_symbol']}/{signal['quote_symbol']}) : {exec_result['error']}",
                    level="warning",
                )

    if profitable and telegram:
        tag = "LIVE" if live else "SIMULATION"
        lines = [
            f"• {s['token_symbol']}/{s['quote_symbol']} : achat {s['buy_dex']} → vente {s['sell_dex']} "
            f"(spread {s['spread_pct']:.2f}%, net≈{s['net_profit_usd']:.2f}$)"
            for s in profitable[:5]
        ]
        telegram.send(
            f"📈 <b>{len(profitable)} opportunité(s) d'arbitrage rentable(s) détectée(s) [{tag}]</b>\n"
            + "\n".join(lines),
            level="info",
        )
    return signals


def build_scheduler(
    db: Database,
    telegram=None,
    scan_interval_minutes: int = 30,
    deadline_check_hour: int = 8,
    digest_hour: int = 9,
    opportunity_scan_interval_minutes: int = 120,
    discovery_scan_interval_minutes: int = 1440,
    arbitrage_scan_interval_minutes: int = 5,
    config: dict | None = None,
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
    scheduler.add_job(
        run_opportunity_scan, IntervalTrigger(minutes=opportunity_scan_interval_minutes),
        args=[db, telegram], id="opportunity_scan", replace_existing=True,
    )
    scheduler.add_job(
        run_project_discovery, IntervalTrigger(minutes=discovery_scan_interval_minutes),
        args=[db, telegram], id="project_discovery", replace_existing=True,
    )
    scheduler.add_job(
        run_arbitrage_scan_job, IntervalTrigger(minutes=arbitrage_scan_interval_minutes),
        args=[db, telegram, config], id="arbitrage_scan", replace_existing=True,
    )
    return scheduler
