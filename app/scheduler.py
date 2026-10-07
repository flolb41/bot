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


# Nombre maximum de tentatives d'exécution (round-trip complet uniquement — voir
# `_PAIR_COOLDOWN_SECONDS`) par appel de job — pas une cible, un garde-fou pour
# borner le temps total passé dans un seul appel si le marché enchaîne des
# signaux rentables et gagnants (voir la boucle de re-scan immédiat ci-dessous).
# Le débit réel vers DEX Screener reste de toute façon plafonné par le throttle
# 1 appel/1.1s (voir app/trading/dex_sources.py), qu'on boucle ou non.
_MAX_ATTEMPTS_PER_CYCLE = 10

# Durée (secondes) pendant laquelle une paire (token/quote) dont la dernière
# tentative s'est soldée par un abandon/échec est exclue des candidats
# exécutables — évite de retenter en boucle un écart structurellement
# illusoire (ex: spread qui se referme systématiquement avant la 2e jambe,
# observé en conditions réelles) à chaque scan ou à chaque itération du re-scan
# immédiat. Persisté en base (`system_state`) pour survivre à un redémarrage.
#
# Deux durées distinctes selon que du capital a RÉELLEMENT été engagé ou non :
# - un échec de SIMULATION (web3 `.call()` qui revert, ex: "Too little
#   received") se produit AVANT tout envoi de transaction — aucun gas, aucun
#   capital déplacé. Un cooldown de 10 min y est disproportionné : observé en
#   conditions réelles, ça bloquait de vraies opportunités WETH/USDC
#   rentables pendant 3+ cycles de scan (3 min chacun) pour une "erreur" qui
#   n'a rien coûté. Cooldown court pour laisser le scan suivant retenter vite.
# - un échec APRÈS un achat réellement envoyé on-chain (ex: écart refermé
#   avant la 2e jambe, position laissée ouverte) a un vrai coût (gas +
#   éventuel slippage de récupération) : on garde le cooldown long pour éviter
#   de marteler un écart structurellement illusoire.
_PAIR_COOLDOWN_SECONDS = 600
_PAIR_COOLDOWN_SECONDS_NO_CAPITAL_RISK = 90


def _pair_cooldown_key(signal: dict) -> str:
    return f"arbitrage_cooldown:{signal['chain']}:{signal['token_address']}:{signal['quote_address']}"


def _is_pair_in_cooldown(db: Database, signal: dict) -> bool:
    import time
    until = db.get_state(_pair_cooldown_key(signal))
    return bool(until) and time.time() < float(until)


def _set_pair_cooldown(db: Database, signal: dict, seconds: int = _PAIR_COOLDOWN_SECONDS) -> None:
    import time
    db.set_state(_pair_cooldown_key(signal), str(time.time() + seconds))


def run_arbitrage_scan_job(db: Database, telegram=None, config: dict | None = None) -> list[dict]:
    """Scanne les écarts de prix inter-DEX (bot de trading — voir
    app/trading/arbitrage.py). Les signaux rentables (would_execute=True) sont
    toujours notifiés. Si et SEULEMENT SI le trading live est activé (tous les
    garde-fous de `app.trading.guardrails.is_trading_live` réunis) :
    1. Tente d'abord de clôturer d'éventuelles positions restées ouvertes d'un
       cycle précédent (`app.trading.executor.recover_open_positions`) — priorité
       à la fermeture de l'exposition existante avant d'en ouvrir une nouvelle.
    2. Exécute la signal rentable la PLUS rentable (net_profit_usd le plus élevé,
       pas simplement la première détectée) parmi celles dont la paire n'est pas
       en cooldown, via `app.trading.executor.execute_signal`.
    3. Si ce round-trip aboutit PLEINEMENT (achat + vente réels), relance
       immédiatement un nouveau cycle complet (recovery + scan + exécution) SANS
       attendre le prochain tick du scheduler — jusqu'à ce qu'il n'y ait plus
       d'opportunité rentable ou que `_MAX_ATTEMPTS_PER_CYCLE` soit atteint.
       Si le round-trip est abandonné ou échoue, la paire concernée est mise en
       cooldown (`_PAIR_COOLDOWN_SECONDS`) et la boucle s'arrête pour laisser le
       prochain tick normal du scheduler reprendre la main (pas de ré-essai
       immédiat sur un échec, pour éviter de marteler un écart illusoire).
    Sinon (trading non-live) reste purement informatif, un seul passage, comme avant
    (aucune transaction envoyée)."""
    if is_stopped(db):
        return []

    from app.trading.guardrails import is_trading_live
    live = is_trading_live(config or {})

    all_signals: list[dict] = []
    attempts = 0

    while True:
        if is_stopped(db):
            break

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
        all_signals.extend(signals)
        # Toujours la plus rentable d'abord (net_profit_usd décroissant) — pas
        # l'ordre de détection, qui dépend juste de l'ordre des seed tokens.
        profitable = sorted(
            (s for s in signals if s["would_execute"]),
            key=lambda s: s["net_profit_usd"] or 0.0,
            reverse=True,
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

        from app.trading.executor import EXECUTABLE_QUOTE_TOKENS
        executable_candidates = [
            s for s in profitable
            if not _is_pair_in_cooldown(db, s)
            # execute_signal refuse systématiquement tout signal dont le quote
            # n'est pas USDC (voir app.trading.executor.EXECUTABLE_QUOTE_TOKENS) ;
            # les exclure ICI évite de "gaspiller" la tentative du cycle (et le
            # cooldown qui s'ensuit) sur un signal qui ne pourra jamais
            # s'exécuter, au détriment d'un vrai candidat USDC moins rentable
            # mais réellement exécutable (bug observé en conditions réelles :
            # les paires croisées WETH/cbETH affichent souvent le net_profit_usd
            # le plus élevé mais ne sont jamais exécutables).
            and s["quote_address"].lower() in EXECUTABLE_QUOTE_TOKENS
        ]

        if not live or not executable_candidates or attempts >= _MAX_ATTEMPTS_PER_CYCLE:
            break

        from app.trading.executor import execute_signal
        best_signal = executable_candidates[0]
        exec_result = execute_signal(db, config, best_signal)
        attempts += 1
        if exec_result["executed"]:
            # Round-trip pleinement réussi : on boucle tout de suite (recovery
            # + nouveau scan) sans attendre le prochain tick.
            continue

        # Du capital n'a réellement été engagé que si l'achat a été diffusé
        # on-chain (tx_hash_buy renseigné) ; un refus de simulation (avant tout
        # envoi) ne coûte rien et mérite un cooldown bien plus court.
        capital_at_risk = bool(exec_result.get("tx_hash_buy"))
        cooldown_seconds = _PAIR_COOLDOWN_SECONDS if capital_at_risk else _PAIR_COOLDOWN_SECONDS_NO_CAPITAL_RISK
        _set_pair_cooldown(db, best_signal, cooldown_seconds)
        cooldown_label = f"{cooldown_seconds // 60} min" if cooldown_seconds >= 60 else f"{cooldown_seconds}s"
        failure_message = (
            f"Exécution d'arbitrage refusée/échouée ({best_signal['token_symbol']}/"
            f"{best_signal['quote_symbol']}) : {exec_result['error']} — "
            f"paire mise en pause {cooldown_label}."
        )
        # Persisté en base même sans Telegram configuré : sans ça, la raison du
        # refus n'était visible que dans les logs systemd (perdue dès que le
        # cycle suivant tourne), rendant le diagnostic impossible après coup.
        db.add_notification(
            title="⚠️ Arbitrage refusé/échoué", message=failure_message, level="warning",
        )
        if telegram and exec_result["error"]:
            telegram.send(f"⚠️ <b>{failure_message}</b>", level="warning")
        # Abandon/échec : pas de re-scan immédiat, on laisse le prochain tick
        # normal du scheduler reprendre la main (la paire fautive est de toute
        # façon en cooldown pendant ce temps).
        break

    return all_signals


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
