"""Tests de la logique PURE (aucun accès DB/réseau) d'app.trading.pair_health :
calcul d'échecs consécutifs, taux de réussite, désactivation temporaire et
multiplicateur de seuil adaptatif. Les wrappers `pair_health`/`triangular_health`
(accès DB) sont couverts indirectement par tests/test_database.py."""
from app.trading import pair_health


def _success(detected_at: float) -> dict:
    return {"detected_at": detected_at, "tx_hash_sell": "0xok", "execution_error": None}


def _failure(detected_at: float) -> dict:
    return {"detected_at": detected_at, "tx_hash_sell": None, "execution_error": "revert"}


def test_consecutive_failures_stops_at_first_success():
    history = [_failure(30), _failure(20), _success(10), _failure(5)]
    assert pair_health.consecutive_failures(history, "tx_hash_sell") == 2


def test_consecutive_failures_all_success_is_zero():
    history = [_success(30), _success(20)]
    assert pair_health.consecutive_failures(history, "tx_hash_sell") == 0


def test_consecutive_failures_empty_history_is_zero():
    assert pair_health.consecutive_failures([], "tx_hash_sell") == 0


def test_success_rate_none_without_history():
    assert pair_health.success_rate([], "tx_hash_sell") is None


def test_success_rate_computed_correctly():
    history = [_success(30), _failure(20), _success(10), _failure(5)]
    assert pair_health.success_rate(history, "tx_hash_sell") == 0.5


def test_is_disabled_now_true_within_cooldown():
    now = 10_000.0
    history = [_failure(now - 60), _failure(now - 120), _failure(now - 180)]
    disabled, minutes_left = pair_health.is_disabled_now(
        history, "tx_hash_sell", failure_threshold=3, cooldown_minutes=120, now=now,
    )
    assert disabled is True
    assert minutes_left is not None and minutes_left > 0


def test_is_disabled_now_false_after_cooldown_expires():
    now = 10_000.0
    history = [_failure(now - 3 * 3600), _failure(now - 4 * 3600), _failure(now - 5 * 3600)]
    disabled, minutes_left = pair_health.is_disabled_now(
        history, "tx_hash_sell", failure_threshold=3, cooldown_minutes=120, now=now,
    )
    assert disabled is False
    assert minutes_left is None


def test_is_disabled_now_false_below_failure_threshold():
    now = 10_000.0
    history = [_failure(now - 60), _failure(now - 120)]
    disabled, _ = pair_health.is_disabled_now(
        history, "tx_hash_sell", failure_threshold=3, cooldown_minutes=120, now=now,
    )
    assert disabled is False


def test_adaptive_profit_threshold_multiplier_neutral_without_enough_samples():
    history = [_failure(30), _failure(20)]
    multiplier = pair_health.adaptive_profit_threshold_multiplier(
        history, "tx_hash_sell", min_samples=5, penalty_factor=2.0,
    )
    assert multiplier == 1.0


def test_adaptive_profit_threshold_multiplier_neutral_for_good_track_record():
    # 90% de réussite (9/10), au-dessus du seuil de bonne fiabilité (80%).
    history = [_success(i) for i in range(9)] + [_failure(9)]
    multiplier = pair_health.adaptive_profit_threshold_multiplier(
        history, "tx_hash_sell", min_samples=5, penalty_factor=2.0,
    )
    assert multiplier == 1.0


def test_adaptive_profit_threshold_multiplier_max_penalty_for_total_failure():
    history = [_failure(i) for i in range(10)]
    multiplier = pair_health.adaptive_profit_threshold_multiplier(
        history, "tx_hash_sell", min_samples=5, penalty_factor=2.0,
    )
    assert multiplier == 2.0


def test_adaptive_profit_threshold_multiplier_scales_between_bounds():
    # 40% de réussite -> malus partiel entre 1.0 (80%+) et 2.0 (0%).
    history = [_success(0), _success(1), _success(2), _success(3)] + [_failure(i) for i in range(4, 10)]
    multiplier = pair_health.adaptive_profit_threshold_multiplier(
        history, "tx_hash_sell", min_samples=5, penalty_factor=2.0,
    )
    assert 1.0 < multiplier < 2.0
