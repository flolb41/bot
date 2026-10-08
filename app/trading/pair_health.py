"""Historique d'exécution RÉELLE par paire (succès/échecs), utilisé par les
scans (`app.trading.arbitrage.run_arbitrage_scan`,
`app.trading.triangular.run_triangular_scan`) pour :

1. **Seuil de profit adaptatif** : une paire dont les tentatives récentes ont
   un fort taux d'échec (ex: le spread se referme systématiquement avant la
   confirmation on-chain) voit son seuil `min_net_profit_usd` MULTIPLIÉ par un
   facteur de prudence — elle reste détectable mais doit afficher une marge
   plus large pour être retenue. Le seuil n'est JAMAIS abaissé en dessous de
   sa valeur configurée (pas de prise de risque supplémentaire même pour une
   paire historiquement fiable).
2. **Désactivation automatique** : après N échecs consécutifs d'affilée sur
   une même paire, elle est totalement exclue de `would_execute` pendant une
   période de "cooldown" configurable — évite de perpétuellement payer du gas
   pour retenter une paire structurellement non-exécutable (ex: liquidité
   trop fine, latence RPC trop grande pour ce token précis).

Ce module ne touche JAMAIS à l'exécution elle-même (aucune transaction, aucun
accès wallet) — uniquement des lectures de l'historique en base et des
calculs purs, appliqués en amont par les scans pour ajuster `would_execute`."""
from __future__ import annotations

import time

from app.database import Database

DEFAULT_LOOKBACK = 20
DEFAULT_MIN_SAMPLES = 5
DEFAULT_PENALTY_FACTOR = 2.0
DEFAULT_AUTO_DISABLE_FAILURES = 3
DEFAULT_AUTO_DISABLE_COOLDOWN_MINUTES = 120.0
# Taux de réussite à partir duquel aucun malus n'est appliqué (historique jugé
# suffisamment fiable) — en dessous, le malus croît linéairement jusqu'à
# `DEFAULT_PENALTY_FACTOR` à 0 % de réussite.
_GOOD_SUCCESS_RATE = 0.8


def pair_health_enabled(config: dict) -> bool:
    return bool((config.get("trading") or {}).get("pair_health_enabled", True))


def adaptive_lookback(config: dict) -> int:
    return int((config.get("trading") or {}).get("adaptive_profit_lookback", DEFAULT_LOOKBACK))


def adaptive_min_samples(config: dict) -> int:
    return int((config.get("trading") or {}).get("adaptive_profit_min_samples", DEFAULT_MIN_SAMPLES))


def adaptive_penalty_factor(config: dict) -> float:
    return float((config.get("trading") or {}).get("adaptive_profit_penalty_factor", DEFAULT_PENALTY_FACTOR))


def auto_disable_consecutive_failures(config: dict) -> int:
    return int((config.get("trading") or {}).get("auto_disable_consecutive_failures", DEFAULT_AUTO_DISABLE_FAILURES))


def auto_disable_cooldown_minutes(config: dict) -> float:
    return float(
        (config.get("trading") or {}).get("auto_disable_cooldown_minutes", DEFAULT_AUTO_DISABLE_COOLDOWN_MINUTES)
    )


def _is_success(row: dict, success_column: str) -> bool:
    return bool(row.get(success_column)) and not row.get("execution_error")


def consecutive_failures(history: list[dict], success_column: str) -> int:
    """Nombre d'échecs consécutifs en partant de la tentative la PLUS
    RÉCENTE (`history[0]`) — `history` doit être trié `detected_at` DESC.
    S'arrête au premier succès rencontré (ou à la fin de l'historique)."""
    count = 0
    for row in history:
        if _is_success(row, success_column):
            break
        count += 1
    return count


def success_rate(history: list[dict], success_column: str) -> float | None:
    """`None` si aucune tentative enregistrée (pas encore assez d'historique
    pour juger — voir `adaptive_min_samples` côté appelant)."""
    if not history:
        return None
    successes = sum(1 for row in history if _is_success(row, success_column))
    return successes / len(history)


def disabled_until(
    history: list[dict], success_column: str, *, failure_threshold: int, cooldown_minutes: float,
) -> float | None:
    """Timestamp Unix jusqu'auquel la paire doit être exclue de
    `would_execute`, ou `None` si elle n'est pas (ou plus) en cooldown."""
    failures = consecutive_failures(history, success_column)
    if failures < failure_threshold:
        return None
    most_recent_attempt_at = float(history[0]["detected_at"])
    return most_recent_attempt_at + cooldown_minutes * 60


def is_disabled_now(
    history: list[dict], success_column: str, *, failure_threshold: int, cooldown_minutes: float,
    now: float | None = None,
) -> tuple[bool, float | None]:
    """Retourne `(disabled, minutes_restantes)` — `minutes_restantes` est
    `None` si `disabled` est `False`."""
    until = disabled_until(
        history, success_column, failure_threshold=failure_threshold, cooldown_minutes=cooldown_minutes
    )
    if until is None:
        return False, None
    now = now if now is not None else time.time()
    if now >= until:
        return False, None
    return True, (until - now) / 60


def adaptive_profit_threshold_multiplier(
    history: list[dict], success_column: str, *, min_samples: int, penalty_factor: float,
) -> float:
    """Multiplicateur (>= 1.0) à appliquer au seuil `min_net_profit_usd`
    configuré pour cette paire — 1.0 si pas assez d'historique ou historique
    jugé fiable (taux de réussite >= `_GOOD_SUCCESS_RATE`), jusqu'à
    `penalty_factor` si la paire échoue systématiquement (0 % de réussite)."""
    if len(history) < min_samples:
        return 1.0
    rate = success_rate(history, success_column)
    if rate is None or rate >= _GOOD_SUCCESS_RATE:
        return 1.0
    shortfall = 1.0 - rate / _GOOD_SUCCESS_RATE
    return 1.0 + (penalty_factor - 1.0) * shortfall


def pair_health(db: Database, config: dict, *, token_symbol: str, buy_dex: str, sell_dex: str) -> dict:
    """Agrège les décisions de santé (désactivation + seuil adaptatif) pour une
    paire classique buy/sell — à appeler une fois par signal candidat dans
    `app.trading.arbitrage.run_arbitrage_scan`."""
    if not pair_health_enabled(config):
        return {"disabled": False, "disabled_reason": None, "threshold_multiplier": 1.0}
    history = db.pair_execution_history(
        token_symbol=token_symbol, buy_dex=buy_dex, sell_dex=sell_dex, limit=adaptive_lookback(config)
    )
    disabled, minutes_left = is_disabled_now(
        history, "tx_hash_sell",
        failure_threshold=auto_disable_consecutive_failures(config),
        cooldown_minutes=auto_disable_cooldown_minutes(config),
    )
    reason = (
        f"paire {token_symbol} {buy_dex}→{sell_dex} désactivée temporairement "
        f"({auto_disable_consecutive_failures(config)} échecs consécutifs, encore {minutes_left:.0f} min de pause)"
        if disabled else None
    )
    multiplier = adaptive_profit_threshold_multiplier(
        history, "tx_hash_sell", min_samples=adaptive_min_samples(config), penalty_factor=adaptive_penalty_factor(config)
    )
    return {"disabled": disabled, "disabled_reason": reason, "threshold_multiplier": multiplier}


def triangular_health(
    db: Database, config: dict, *, dex: str, token_a_symbol: str, token_b_symbol: str, token_c_symbol: str,
) -> dict:
    """Équivalent triangulaire de `pair_health`."""
    if not pair_health_enabled(config):
        return {"disabled": False, "disabled_reason": None, "threshold_multiplier": 1.0}
    history = db.triangular_execution_history(
        dex=dex, token_a_symbol=token_a_symbol, token_b_symbol=token_b_symbol, token_c_symbol=token_c_symbol,
        limit=adaptive_lookback(config),
    )
    disabled, minutes_left = is_disabled_now(
        history, "tx_hash",
        failure_threshold=auto_disable_consecutive_failures(config),
        cooldown_minutes=auto_disable_cooldown_minutes(config),
    )
    reason = (
        f"cycle {token_a_symbol}→{token_b_symbol}→{token_c_symbol} sur {dex} désactivé temporairement "
        f"({auto_disable_consecutive_failures(config)} échecs consécutifs, encore {minutes_left:.0f} min de pause)"
        if disabled else None
    )
    multiplier = adaptive_profit_threshold_multiplier(
        history, "tx_hash", min_samples=adaptive_min_samples(config), penalty_factor=adaptive_penalty_factor(config)
    )
    return {"disabled": disabled, "disabled_reason": reason, "threshold_multiplier": multiplier}
