"""Scheduler du bot d'arbitrage inter-DEX.

Orchestre le scan périodique des écarts de prix et leur exécution éventuelle.
Respecte le killswitch : si actif, aucun job ne s'exécute.
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.database import Database
from app.killswitch import is_stopped
from app.trading.arbitrage import run_arbitrage_scan
from app.trading.triangular import run_cross_dex_triangular_scan, run_triangular_scan

logger = logging.getLogger("app.scheduler")


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


def _triangular_cooldown_key(signal: dict) -> str:
    return (
        f"triangular_cooldown:{signal['chain']}:{signal['dex']}:"
        f"{signal['token_a_address']}:{signal['token_b_address']}:{signal['token_c_address']}"
    )


def _cooldown_key(signal: dict) -> str:
    """Clé de cooldown adaptée au type de signal (voir `signal['kind']`,
    ajouté par `run_arbitrage_scan_job` à la détection — absent des signaux
    tels que retournés directement par `run_arbitrage_scan`/`run_triangular_scan`)."""
    return _triangular_cooldown_key(signal) if signal.get("kind") == "triangular" else _pair_cooldown_key(signal)


def _is_pair_in_cooldown(db: Database, signal: dict) -> bool:
    import time
    until = db.get_state(_cooldown_key(signal))
    return bool(until) and time.time() < float(until)


def _set_pair_cooldown(db: Database, signal: dict, seconds: int = _PAIR_COOLDOWN_SECONDS) -> None:
    import time
    db.set_state(_cooldown_key(signal), str(time.time() + seconds))


def run_arbitrage_scan_job(db: Database, telegram=None, config: dict | None = None) -> list[dict]:
    """Scanne les écarts de prix inter-DEX classiques (voir
    app/trading/arbitrage.py) ET les cycles triangulaires mono-DEX (voir
    app/trading/triangular.py). Les signaux rentables (would_execute=True) sont
    toujours notifiés. Si et SEULEMENT SI le trading live est activé (tous les
    garde-fous de `app.trading.guardrails.is_trading_live` réunis) :
    1. Tente d'abord de clôturer d'éventuelles positions restées ouvertes d'un
       cycle précédent (`app.trading.executor.recover_open_positions`) — priorité
       à la fermeture de l'exposition existante avant d'en ouvrir une nouvelle.
       Ne concerne que l'arbitrage classique (2 jambes) : un cycle triangulaire
       est atomique (1 seule transaction), il ne peut jamais laisser de
       position résiduelle ouverte.
    2. Exécute le signal rentable le PLUS rentable (net_profit_usd le plus élevé,
       classique OU triangulaire confondus) parmi ceux dont la clé de cooldown
       n'est pas active, via `app.trading.executor.execute_signal` (classique)
       ou `execute_triangular_signal` (triangulaire) — ou, si éligible et activé
       (`trading.flashloan_enabled`, voir `app.trading.flashloan`), via le
       contrat `FlashArbitrage` financé par flashloan Aave V3 (montant
       indépendant du solde réel du wallet).
    3. Si cette exécution aboutit PLEINEMENT, relance immédiatement un nouveau
       cycle complet (recovery + scan + exécution) SANS attendre le prochain
       tick du scheduler — jusqu'à ce qu'il n'y ait plus d'opportunité rentable
       ou que `_MAX_ATTEMPTS_PER_CYCLE` soit atteint. Si elle est abandonnée ou
       échoue, le candidat concerné est mis en cooldown (`_PAIR_COOLDOWN_SECONDS`)
       et la boucle s'arrête pour laisser le prochain tick normal du scheduler
       reprendre la main (pas de ré-essai immédiat sur un échec, pour éviter de
       marteler un écart illusoire).
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

        pair_signals = run_arbitrage_scan(db, config)
        for s in pair_signals:
            s["kind"] = "pair"
        triangular_signals = run_triangular_scan(db, config)
        for s in triangular_signals:
            s["kind"] = "triangular"
        signals = pair_signals + triangular_signals

        # Scan cross-DEX informationnel (voir
        # `app.trading.triangular.run_cross_dex_triangular_scan`) : jamais
        # exécutable, jamais inclus dans `signals`/`profitable` ci-dessous —
        # journalisé en base uniquement pour inspection manuelle. Isolé dans
        # son propre try/except pour qu'une erreur ici ne puisse jamais
        # empêcher le scan/l'exécution réels de tourner.
        try:
            run_cross_dex_triangular_scan(db, config)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Cross-DEX triangular scan (informationnel) en échec : %s", exc)
        all_signals.extend(signals)
        # Toujours le plus rentable d'abord (net_profit_usd décroissant), tous
        # types confondus — pas l'ordre de détection, qui dépend juste de
        # l'ordre des seed tokens/DEX.
        profitable = sorted(
            (s for s in signals if s["would_execute"]),
            key=lambda s: s["net_profit_usd"] or 0.0,
            reverse=True,
        )

        if profitable and telegram:
            tag = "LIVE" if live else "SIMULATION"
            lines = [
                (
                    f"• {s['token_symbol']}/{s['quote_symbol']} : achat {s['buy_dex']} → vente {s['sell_dex']} "
                    f"(spread {s['spread_pct']:.2f}%, net≈{s['net_profit_usd']:.2f}$)"
                ) if s["kind"] == "pair" else (
                    f"• 🔺 {s['token_a_symbol']}→{s['token_b_symbol']}→{s['token_c_symbol']}→{s['token_a_symbol']} "
                    f"sur {s['dex']} (spread {s['spread_pct']:.2f}%, net≈{s['net_profit_usd']:.2f}$)"
                )
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
            # execute_signal refuse systématiquement tout signal "pair" dont le
            # quote n'est pas USDC ou WETH (voir app.trading.executor.EXECUTABLE_QUOTE_TOKENS) ;
            # les exclure ICI évite de "gaspiller" la tentative du cycle (et le
            # cooldown qui s'ensuit) sur un signal qui ne pourra jamais
            # s'exécuter, au détriment d'un vrai candidat USDC/WETH moins
            # rentable mais réellement exécutable (bug observé en conditions
            # réelles : les paires croisées cbETH/AERO/cbBTC/USDbC affichent
            # souvent le net_profit_usd le plus élevé mais ne sont jamais
            # exécutables). Les signaux "triangulaire" partent/reviennent déjà
            # toujours dans USDC/WETH par construction (voir
            # app.trading.triangular.run_triangular_scan), rien à filtrer ici.
            and (s["kind"] == "triangular" or s["quote_address"].lower() in EXECUTABLE_QUOTE_TOKENS)
        ]

        if not live or not executable_candidates or attempts >= _MAX_ATTEMPTS_PER_CYCLE:
            break

        from app.trading.executor import execute_signal, execute_triangular_signal
        from app.trading.flashloan import (
            execute_pair_signal_via_flashloan,
            execute_triangular_signal_via_flashloan,
            pair_signal_is_flashloan_eligible,
            triangular_signal_is_flashloan_eligible,
        )
        best_signal = executable_candidates[0]
        # Flashloan (point 8, voir app/trading/flashloan.py) : exécution
        # financée par Aave V3, indépendante du solde réel du wallet. Essayé
        # EN PRIORITÉ quand éligible (config + DEX/quote compatibles) ; repli
        # automatique sur l'exécution classique (auto-financée) sinon — aucun
        # changement de comportement par défaut (`flashloan_enabled: false`).
        if best_signal["kind"] == "triangular":
            if triangular_signal_is_flashloan_eligible(config or {}, best_signal):
                exec_result = execute_triangular_signal_via_flashloan(db, config, best_signal)
            else:
                exec_result = execute_triangular_signal(db, config, best_signal)
        else:
            if pair_signal_is_flashloan_eligible(config or {}, best_signal):
                exec_result = execute_pair_signal_via_flashloan(db, config, best_signal)
            else:
                exec_result = execute_signal(db, config, best_signal)
        attempts += 1
        if exec_result["executed"]:
            # Exécution pleinement réussie : on boucle tout de suite (recovery
            # + nouveau scan) sans attendre le prochain tick.
            continue

        # Du capital n'a réellement été engagé que si une transaction a été
        # diffusée on-chain (achat classique ou swap triangulaire) ; un refus
        # de simulation/garde-fou (avant tout envoi) ne coûte rien et mérite
        # un cooldown bien plus court.
        capital_at_risk = bool(exec_result.get("tx_hash_buy") or exec_result.get("tx_hash"))
        cooldown_seconds = _PAIR_COOLDOWN_SECONDS if capital_at_risk else _PAIR_COOLDOWN_SECONDS_NO_CAPITAL_RISK
        _set_pair_cooldown(db, best_signal, cooldown_seconds)
        cooldown_label = f"{cooldown_seconds // 60} min" if cooldown_seconds >= 60 else f"{cooldown_seconds}s"
        if best_signal["kind"] == "triangular":
            candidate_label = (
                f"{best_signal['token_a_symbol']}→{best_signal['token_b_symbol']}→"
                f"{best_signal['token_c_symbol']}→{best_signal['token_a_symbol']} sur {best_signal['dex']}"
            )
        else:
            candidate_label = f"{best_signal['token_symbol']}/{best_signal['quote_symbol']}"
        failure_message = (
            f"Exécution d'arbitrage refusée/échouée ({candidate_label}) : {exec_result['error']} — "
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
    arbitrage_scan_interval_minutes: int = 5,
    config: dict | None = None,
) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone="UTC")

    scheduler.add_job(
        run_arbitrage_scan_job, IntervalTrigger(minutes=arbitrage_scan_interval_minutes),
        args=[db, telegram, config], id="arbitrage_scan", replace_existing=True,
    )
    return scheduler
