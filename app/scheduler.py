"""Scheduler du bot d'arbitrage inter-DEX.

Orchestre le scan périodique des écarts de prix et leur exécution éventuelle.
Respecte le killswitch : si actif, aucun job ne s'exécute.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.database import Database
from app.killswitch import is_stopped
from app.monitoring.system_health import SystemHealthMonitor
from app.trading.arbitrage import collect_pairs_for_config, run_arbitrage_scan
from app.trading.block_ws import BlockWebSocketListener
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
# Refus structurel (aucun capital engagé, mais l'état on-chain qui le motive
# — pool sous le plancher de profondeur, écart implausible — ne change pas à
# l'échelle d'un scan) : cooldown long pour ne pas re-proposer le même
# candidat à chaque scan (410 refus identiques la nuit du 08 au 09/11/2026).
_STRUCTURAL_REFUSAL_COOLDOWN_SECONDS = 3600
_STRUCTURAL_REFUSAL_MARKERS = (
    "sous le plancher", "Profondeur on-chain indisponible", "Profondeur insuffisante",
    "trop peu profonde", "implausible", "Liquidité de pool insuffisante",
)
_SCAN_LOCK = threading.Lock()


def _is_structural_refusal(error: str | None) -> bool:
    """Refus dont la cause est un état on-chain stable (profondeur de pool,
    prix fantôme) plutôt qu'un aléa de marché (écart refermé, RPC, simulation)."""
    return bool(error) and any(marker in error for marker in _STRUCTURAL_REFUSAL_MARKERS)


# Même ensemble de candidats « rentables » re-détecté scan après scan (écart
# structurel) : une seule annonce par fenêtre, pas 392 par nuit.
_PROFITABLE_ANNOUNCE_COOLDOWN_SECONDS = 1800


def _profitable_signature(profitable: list[dict]) -> str:
    keys = sorted(_cooldown_key(signal) for signal in profitable)
    return "|".join(keys)


def _should_announce_profitable(db: Database, profitable: list[dict]) -> bool:
    """True si l'ensemble des candidats rentables diffère du dernier annoncé,
    ou si la dernière annonce identique date de plus de 30 min."""
    signature = _profitable_signature(profitable)
    now = time.time()
    last_signature = db.get_state("profitable_announce_signature")
    last_at = db.get_state("profitable_announce_at")
    try:
        recent = last_at is not None and now - float(last_at) < _PROFITABLE_ANNOUNCE_COOLDOWN_SECONDS
    except ValueError:
        recent = False
    if last_signature == signature and recent:
        return False
    db.set_state("profitable_announce_signature", signature)
    db.set_state("profitable_announce_at", str(now))
    return True


def _effective_profit_usd(signal: dict, config: dict | None = None) -> float:
    """Score de classement des signaux rentables (quel candidat tenter d'abord).
    La base reflète le profit estimé que le bot captera, pas seulement
    `net_profit_usd` (profit
    au micro-capital du wallet, quelques dollars) — un signal éligible
    flashloan (`would_execute_flashloan`, voir app.trading.flashloan) est
    exécuté avec un notionnel bien plus grand (jusqu'à `flashloan_notional_usd`,
    financé par Aave, sans risque de capital propre), donc avec un profit
    `flashloan_net_profit_usd` potentiellement 10-100x supérieur.

    Bug corrigé ici (observé en conditions réelles) : avant ce correctif, le tri
    se faisait uniquement sur `net_profit_usd`, si bien qu'une paire fiable mais
    marginale (ex: WETH/USDC, quelques centimes de profit par trade) gagnait
    systématiquement le tri et monopolisait les tentatives du cycle, empêchant
    de VRAIS gros profits flashloan (BRETT/AERO/DEGEN, jusqu'à plusieurs
    dollars par opportunité) d'être jamais tentés.

    Une priorité de token configurée peut ajuster l'ordre uniquement : elle ne
    rend jamais rentable un signal et ne modifie ni son montant ni ses seuils."""
    flashloan_profit = signal.get("flashloan_net_profit_usd")
    if signal.get("would_execute_flashloan") and flashloan_profit is not None:
        base_score = float(flashloan_profit)
    else:
        base_score = float(signal.get("net_profit_usd") or 0.0)

    trading_cfg = (config or {}).get("trading", {}) or {}
    multipliers = trading_cfg.get("token_priority_multipliers", {}) or {}
    addresses = {
        str(signal[key]).lower()
        for key in ("token_address", "token_a_address", "token_b_address", "token_c_address")
        if signal.get(key)
    }
    configured_multipliers = [float(multipliers.get(address, 1.0)) for address in addresses]
    if any(not math.isfinite(value) or value < 1.0 for value in configured_multipliers):
        raise ValueError("Token priority multipliers must be finite and at least 1.0.")
    multiplier = max(configured_multipliers, default=1.0)
    return base_score * multiplier


def _pair_cooldown_key(signal: dict) -> str:
    return f"arbitrage_cooldown:{signal['chain']}:{signal['token_address']}:{signal['quote_address']}"


def _triangular_cooldown_key(signal: dict) -> str:
    return (
        f"triangular_cooldown:{signal['chain']}:{signal['dex']}:"
        f"{signal['token_a_address']}:{signal['token_b_address']}:{signal['token_c_address']}"
    )


def _cross_dex_triangular_cooldown_key(signal: dict) -> str:
    return (
        f"cross_dex_triangular_cooldown:{signal['chain']}:"
        f"{signal['token_a_address']}:{signal['token_b_address']}:{signal['token_c_address']}:"
        f"{signal['dex_ab']}:{signal['dex_bc']}:{signal['dex_ca']}"
    )


def _cooldown_key(signal: dict) -> str:
    """Clé de cooldown adaptée au type de signal (voir `signal['kind']`,
    ajouté par `run_arbitrage_scan_job` à la détection — absent des signaux
    tels que retournés directement par `run_arbitrage_scan`/`run_triangular_scan`)."""
    if signal.get("kind") == "triangular":
        return _triangular_cooldown_key(signal)
    if signal.get("kind") == "cross_dex_triangular":
        return _cross_dex_triangular_cooldown_key(signal)
    return _pair_cooldown_key(signal)


def _is_pair_in_cooldown(db: Database, signal: dict) -> bool:
    import time
    until = db.get_state(_cooldown_key(signal))
    return bool(until) and time.time() < float(until)


def _set_pair_cooldown(db: Database, signal: dict, seconds: int = _PAIR_COOLDOWN_SECONDS) -> None:
    import time
    db.set_state(_cooldown_key(signal), str(time.time() + seconds))


def _candidate_label(signal: dict) -> str:
    """Libellé lisible d'un candidat (paire ou cycle triangulaire) pour les logs/notifications."""
    if signal["kind"] in ("triangular", "cross_dex_triangular"):
        route_label = (
            "→".join((signal["dex_ab"], signal["dex_bc"], signal["dex_ca"]))
            if signal["kind"] == "cross_dex_triangular"
            else signal["dex"]
        )
        return (
            f"{signal['token_a_symbol']}→{signal['token_b_symbol']}→"
            f"{signal['token_c_symbol']}→{signal['token_a_symbol']} sur {route_label}"
        )
    return f"{signal['token_symbol']}/{signal['quote_symbol']}"


def _report_flashloan_fallback(db: Database, candidate_label: str, flashloan_result: dict) -> None:
    """Rend visible un refus/échec de la voie flashloan avant le repli classique.

    Le résultat flashloan était auparavant écrasé par celui du repli : ni log
    ni notification, impossible de savoir après coup pourquoi un signal
    estimé à 17$ en flashloan (ICNT/USDC, 08/11/2026) avait fini en
    round-trip wallet de 0,54$."""
    message = (
        f"Flashloan refusé/échoué pour {candidate_label} : "
        f"{flashloan_result.get('error') or 'raison inconnue'} — repli sur l'exécution classique."
    )
    if flashloan_result.get("tx_hash"):
        message += f" Transaction flashloan : {flashloan_result['tx_hash']}."
    logger.warning(message)
    db.add_notification(title="↩️ Repli flashloan → classique", message=message, level="warning")


def _run_arbitrage_scan_job(db: Database, telegram=None, config: dict | None = None) -> list[dict]:
    """Scanne les écarts de prix inter-DEX classiques (voir
    app/trading/arbitrage.py), les cycles mono-DEX et, si ses deux
    commutateurs dédiés sont activés, les cycles triangulaires cross-DEX. Les
    signaux rentables (would_execute=True) sont
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
       ou l'exécuteur flashloan atomique cross-DEX — ou, si éligible et activé
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

    from app.trading.runtime_settings import apply_runtime_settings
    config = apply_runtime_settings(db, config or {})

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
            for r in recovery_results:
                if not r["recovered"] and r["error"]:
                    logger.warning(
                        "Récupération de position résiduelle en attente (%s) : %s",
                        r["token_symbol"], r["error"],
                    )
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

        # Récupère les pools DexScreener UNE SEULE FOIS pour ce cycle, partagées
        # entre les 3 scans ci-dessous (voir le docstring de
        # `collect_pairs_for_config` : ils portaient auparavant chacun sur les
        # mêmes tokens/whitelist/liquidité et déclenchaient 3 fetches DexScreener
        # indépendants, ce qui faisait déjà dépasser la durée d'un cycle
        # au-delà de l'intervalle configuré — un fetch unique permet un cycle
        # ~3x plus rapide, condition nécessaire avant de pouvoir réduire
        # l'intervalle de scan sans effet nul (ticks sautés).
        scan_started_at = time.time()
        db.set_state("last_scan_started_at", str(scan_started_at))
        shared_groups = collect_pairs_for_config(config)

        pair_signals = run_arbitrage_scan(db, config, groups=shared_groups)
        for s in pair_signals:
            s["kind"] = "pair"
        triangular_signals = run_triangular_scan(db, config, groups=shared_groups)
        for s in triangular_signals:
            s["kind"] = "triangular"
        signals = pair_signals + triangular_signals
        try:
            from app.trading.paper_strategies import run_paper_experiments

            run_paper_experiments(db, config or {}, shared_groups)
        except Exception:
            logger.exception("Paper research strategy update failed.")
        try:
            from app.trading.directional_live import run_live_directional_strategy

            run_live_directional_strategy(db, config or {}, shared_groups)
        except Exception:
            logger.exception("Live directional strategy update failed.")

        # Le cross-DEX ne devient candidat qu'avec ses garde-fous dédiés et un
        # aperçu flashloan au-dessus du minimum absolu et relatif. L'exécution
        # relira ensuite toutes les profondeurs on-chain et simulera le cycle
        # complet avant diffusion.
        try:
            from app.trading.flashloan import (
                cross_dex_triangular_signal_is_flashloan_eligible,
                estimate_cross_dex_triangular_flashloan_preview,
                flashloan_min_net_profit_usd,
            )

            cross_dex_signals = run_cross_dex_triangular_scan(db, config, groups=shared_groups)
            for signal in cross_dex_signals:
                signal["kind"] = "cross_dex_triangular"
                signal["would_execute_classic"] = False
                signal["would_execute_flashloan"] = False
                signal["would_execute"] = False
                preview = estimate_cross_dex_triangular_flashloan_preview(config or {}, signal)
                if preview is not None:
                    signal["flashloan_notional_usd"] = preview["notional_usd"]
                    signal["flashloan_net_profit_usd"] = preview["net_profit_usd"]
                    db.update_cross_dex_signal_preview(
                        signal["id"],
                        notional_usd=preview["notional_usd"],
                        net_profit_usd=preview["net_profit_usd"],
                    )
                    signal["would_execute_flashloan"] = (
                        preview["net_profit_usd"] > flashloan_min_net_profit_usd(
                            config or {}, preview["notional_usd"],
                        )
                    )
                    signal["would_execute"] = signal["would_execute_flashloan"]
                if cross_dex_triangular_signal_is_flashloan_eligible(config or {}, signal):
                    signals.append(signal)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Scan/préfiltre cross-DEX en échec : %s", exc)
        scan_finished_at = time.time()
        db.set_state("last_scan_completed_at", str(scan_finished_at))
        db.set_state("last_scan_duration_seconds", str(scan_finished_at - scan_started_at))
        all_signals.extend(signals)
        # Toujours le plus rentable d'abord, tous types confondus — pas l'ordre
        # de détection, qui dépend juste de l'ordre des seed tokens/DEX. Trié
        # sur `_effective_profit_usd` (pas directement `net_profit_usd`) :
        # un signal éligible flashloan doit être comparé à son profit à
        # l'échelle flashloan (potentiellement 10-100x supérieur au profit au
        # micro-capital du wallet), sans quoi une paire fiable mais marginale
        # (ex: WETH/USDC, quelques centimes) monopolise systématiquement le
        # tri et empêche les vraies grosses opportunités flashloan (BRETT/
        # AERO/DEGEN, jusqu'à plusieurs dollars) d'être jamais tentées — bug
        # confirmé en conditions réelles (45 opportunités flashloan ≥0,10$
        # jamais exécutées sur ~23h, ~44$ de profit manqué).
        profitable = sorted(
            (s for s in signals if s["would_execute"]),
            key=lambda signal: _effective_profit_usd(signal, config),
            reverse=True,
        )

        if profitable and telegram and _should_announce_profitable(db, profitable):
            tag = "LIVE" if live else "SIMULATION"
            lines = [
                (
                    f"• {s['token_symbol']}/{s['quote_symbol']} : achat {s['buy_dex']} → vente {s['sell_dex']} "
                    f"(spread {s['spread_pct']:.2f}%, net≈{s['net_profit_usd']:.2f}$)"
                ) if s["kind"] == "pair" else (
                    f"• 🔺 {s['token_a_symbol']}→{s['token_b_symbol']}→{s['token_c_symbol']}→{s['token_a_symbol']} "
                    f"({s['dex_ab']}→{s['dex_bc']}→{s['dex_ca']}, "
                    f"spread {s['spread_pct']:.2f}%, flashloan≈{s['flashloan_net_profit_usd']:.2f}$)"
                ) if s["kind"] == "cross_dex_triangular" else (
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
            and (
                s["kind"] in ("triangular", "cross_dex_triangular")
                or s["quote_address"].lower() in EXECUTABLE_QUOTE_TOKENS
            )
        ]

        if not live or not executable_candidates or attempts >= _MAX_ATTEMPTS_PER_CYCLE:
            break

        from app.trading.executor import execute_signal, execute_triangular_signal
        from app.trading.flashloan import (
            execute_pair_signal_via_flashloan,
            execute_triangular_signal_via_flashloan,
            execute_cross_dex_triangular_signal_via_flashloan,
            pair_signal_is_flashloan_eligible,
            triangular_signal_is_flashloan_eligible,
        )
        best_signal = executable_candidates[0]
        candidate_label = _candidate_label(best_signal)
        logger.info(
            "Tentative d'exécution LIVE : %s (type %s, net estimé %.4f$, flashloan estimé %s).",
            candidate_label, best_signal["kind"], float(best_signal.get("net_profit_usd") or 0.0),
            (f"{best_signal['flashloan_net_profit_usd']:.4f}$"
             if best_signal.get("flashloan_net_profit_usd") is not None else "n/a"),
        )
        # Flashloan (point 8, voir app/trading/flashloan.py) : exécution
        # financée par Aave V3, indépendante du solde réel du wallet. Essayé
        # EN PRIORITÉ quand éligible (config + DEX/quote compatibles) ET
        # effectivement rentable à l'échelle flashloan (`would_execute_flashloan`,
        # voir app.trading.arbitrage/triangular) ; repli automatique sur
        # l'exécution classique (auto-financée) sinon — un signal rentable
        # UNIQUEMENT au capital propre (`would_execute_classic`) ne doit pas
        # être routé vers le flashloan (qui le refuserait de toute façon via
        # son propre garde-fou, gaspillant la tentative/le cooldown du cycle).
        if best_signal["kind"] == "cross_dex_triangular":
            exec_result = execute_cross_dex_triangular_signal_via_flashloan(db, config, best_signal)
        elif best_signal["kind"] == "triangular":
            use_flashloan = (
                triangular_signal_is_flashloan_eligible(config or {}, best_signal)
                and best_signal.get("would_execute_flashloan", best_signal.get("would_execute"))
            )
            if use_flashloan:
                exec_result = execute_triangular_signal_via_flashloan(db, config, best_signal)
                # Repli classique si le flashloan échoue (simulation revert,
                # prix refermé, etc.) et que le cycle est rentable au capital
                # propre — évite de mettre le cycle en cooldown pour rien.
                if (not exec_result["executed"]
                        and best_signal.get("would_execute_classic")):
                    _report_flashloan_fallback(db, candidate_label, exec_result)
                    exec_result = execute_triangular_signal(db, config, best_signal)
            else:
                exec_result = execute_triangular_signal(db, config, best_signal)
        else:
            use_flashloan = (
                pair_signal_is_flashloan_eligible(config or {}, best_signal)
                and best_signal.get("would_execute_flashloan", best_signal.get("would_execute"))
            )
            if use_flashloan:
                exec_result = execute_pair_signal_via_flashloan(db, config, best_signal)
                # Repli classique si le flashloan échoue et que le signal est
                # rentable au capital propre — évite le cooldown inutile.
                if (not exec_result["executed"]
                        and best_signal.get("would_execute_classic")):
                    _report_flashloan_fallback(db, candidate_label, exec_result)
                    exec_result = execute_signal(db, config, best_signal)
            else:
                exec_result = execute_signal(db, config, best_signal)
        attempts += 1
        if exec_result["executed"]:
            logger.info(
                "Exécution LIVE réussie : %s — tx %s.",
                candidate_label,
                exec_result.get("tx_hash") or f"achat {exec_result.get('tx_hash_buy')} / vente {exec_result.get('tx_hash_sell')}",
            )
            # Exécution pleinement réussie : on boucle tout de suite (recovery
            # + nouveau scan) sans attendre le prochain tick.
            continue

        # Du capital n'a réellement été engagé que si une transaction a été
        # diffusée on-chain (achat classique ou swap triangulaire) ; un refus
        # de simulation/garde-fou (avant tout envoi) ne coûte rien et mérite
        # un cooldown bien plus court — SAUF s'il est structurel (pool sous le
        # plancher de profondeur, écart implausible) : l'état on-chain ne
        # change pas en 90 s, le candidat reviendrait à l'identique à chaque
        # scan (410 refus en boucle la nuit du 08 au 09/11/2026).
        capital_at_risk = bool(exec_result.get("tx_hash_buy") or exec_result.get("tx_hash"))
        structural_refusal = _is_structural_refusal(exec_result.get("error"))
        if capital_at_risk:
            cooldown_seconds = _PAIR_COOLDOWN_SECONDS
        elif structural_refusal:
            cooldown_seconds = _STRUCTURAL_REFUSAL_COOLDOWN_SECONDS
        else:
            cooldown_seconds = _PAIR_COOLDOWN_SECONDS_NO_CAPITAL_RISK
        _set_pair_cooldown(db, best_signal, cooldown_seconds)
        cooldown_label = f"{cooldown_seconds // 60} min" if cooldown_seconds >= 60 else f"{cooldown_seconds}s"
        failure_message = (
            f"Exécution d'arbitrage refusée/échouée ({candidate_label}) : {exec_result['error']} — "
            f"paire mise en pause {cooldown_label}."
        )
        # Journalisé ET persisté en base (même sans Telegram configuré) : la
        # raison du refus n'était auparavant visible que dans la table
        # `notifications`, jamais dans app.log, rendant le diagnostic après
        # coup laborieux. Un refus structurel (aucun capital engagé, état
        # on-chain connu) reste dans le journal mais ne crée pas de
        # notification : il en produisait 820 par nuit.
        logger.warning("%s%s", failure_message, " Capital engagé on-chain." if capital_at_risk else "")
        if not structural_refusal:
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


def run_arbitrage_scan_job(db: Database, telegram=None, config: dict | None = None) -> list[dict]:
    if not _SCAN_LOCK.acquire(blocking=False):
        logger.info("Scan ignoré : un autre scan est déjà en cours.")
        return []
    try:
        return _run_arbitrage_scan_job(db, telegram, config)
    finally:
        _SCAN_LOCK.release()


def build_scheduler(
    db: Database,
    telegram=None,
    arbitrage_scan_interval_minutes: int = 5,
    config: dict | None = None,
    ws_urls: Optional[list[str]] = None,
) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone="UTC")
    min_scan_interval_seconds = float((config or {}).get("trading", {}).get("ws_min_scan_interval_seconds", 30.0))

    def _fallback_scan() -> None:
        """Scan planifié = REPLI quand le WebSocket est coupé : s'efface si un
        scan a démarré il y a moins de `ws_min_scan_interval_seconds`. Sans ce
        garde, le tick planifié s'ajoutait aux scans déclenchés par bloc et la
        cadence réelle dépassait celle configurée (RPC 429 en continu)."""
        last_started = db.get_state("last_scan_started_at")
        try:
            if last_started and time.time() - float(last_started) < min_scan_interval_seconds:
                return
        except ValueError:
            pass
        run_arbitrage_scan_job(db, telegram, config)

    scheduler.add_job(
        _fallback_scan, IntervalTrigger(minutes=arbitrage_scan_interval_minutes),
        id="arbitrage_scan", replace_existing=True,
    )

    # --- WebSocket newHeads : déclenche un scan dès un nouveau bloc, avec
    #     l'intervalle scheduler comme fallback si le WS est coupé -----------
    def _on_new_block(block_number: int) -> None:
        threading.Thread(
            target=run_arbitrage_scan_job,
            args=(db, telegram, config),
            name=f"arbitrage-scan-block-{block_number}",
            daemon=True,
        ).start()

    ws_listener = BlockWebSocketListener(
        on_new_block=_on_new_block,
        ws_urls=ws_urls,
        min_scan_interval_seconds=min_scan_interval_seconds,
    )
    ws_listener.start()
    scheduler.ws_listener = ws_listener  # type: ignore[attr-defined] — pour arrêt propre dans main.py
    logger.info("WebSocket block listener attaché au scheduler (scan sur nouveau bloc + interval %d min en fallback).",
                arbitrage_scan_interval_minutes)

    # --- Santé du Pi (température + alimentation) : vérification périodique, active
    #     le killswitch si un seuil est franchi (évite le gel nocturne du Pi3) --
    def _check_system_health() -> None:
        try:
            health = SystemHealthMonitor(
                db,
                max_temp_celsius=float((config or {}).get("system_health", {}).get("max_temperature_celsius", 80.0)),
                max_memory_percent=float((config or {}).get("system_health", {}).get("max_memory_percent", 90.0)),
                check_interval_seconds=float((config or {}).get("system_health", {}).get("check_interval_seconds", 60.0)),
                telegram=telegram,
            )
            if health.should_check():
                health.check()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Vérification santé du Pi en échec : %s", exc)

    if (config or {}).get("system_health", {}).get("enabled", True):
        interval_sec = float((config or {}).get("system_health", {}).get("check_interval_seconds", 60.0))
        scheduler.add_job(
            _check_system_health, IntervalTrigger(seconds=interval_sec),
            id="system_health", replace_existing=True,
        )
        logger.info("Health check du Pi attaché au scheduler (interval %ds).", int(interval_sec))

    retention_days = int((config or {}).get("database", {}).get("retention_days", 90))
    if retention_days > 0:
        def _archive_expired_scan_data() -> None:
            try:
                archived = db.archive_expired_scan_data(retention_days=retention_days)
                if archived:
                    logger.info(
                        "Archivage/rétention SQLite : %d lignes archivées avant purge.",
                        sum(archived.values()),
                    )
            except Exception:
                logger.exception("Archivage/rétention SQLite en échec.")

        scheduler.add_job(
            _archive_expired_scan_data, IntervalTrigger(days=1),
            id="database_retention", replace_existing=True, max_instances=1,
            coalesce=True,
        )
        logger.info("Rétention SQLite configurée à %d jours.", retention_days)

    # --- Trésorerie : l'USDC oisif du wallet ne doit jamais dormir ; balayage
    #     périodique vers Aave V3 au-dessus d'un flottant (voir treasury_ops) --
    treasury_cfg = (config or {}).get("treasury", {}) or {}
    if treasury_cfg.get("auto_deposit", False):
        def _treasury_sweep() -> None:
            try:
                from app.trading.treasury_ops import auto_sweep

                auto_sweep(db, config or {})
            except Exception:
                logger.exception("Balayage de trésorerie en échec.")

        sweep_minutes = max(5.0, float(treasury_cfg.get("check_interval_minutes", 60)))
        scheduler.add_job(
            _treasury_sweep, IntervalTrigger(minutes=sweep_minutes),
            id="treasury_sweep", replace_existing=True, max_instances=1, coalesce=True,
        )
        logger.info(
            "Balayage de trésorerie attaché au scheduler (toutes les %d min, flottant %.2f USDC, minimum %.2f USDC).",
            int(sweep_minutes), float(treasury_cfg.get("keep_float_usdc", 0.0)), float(treasury_cfg.get("min_sweep_usdc", 5.0)),
        )
    return scheduler
