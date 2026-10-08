"""Tests de la logique pure (pas de réseau/RPC/scheduler APScheduler réel) de
app.scheduler : calcul du profit effectif utilisé pour classer les signaux
rentables entre eux (quel candidat tenter en premier dans un cycle)."""
from app.scheduler import _effective_profit_usd


def test_effective_profit_uses_net_profit_when_not_flashloan_eligible():
    signal = {
        "net_profit_usd": 0.05,
        "would_execute_flashloan": False,
        "flashloan_net_profit_usd": None,
    }
    assert _effective_profit_usd(signal) == 0.05


def test_effective_profit_prefers_flashloan_profit_when_eligible():
    # Cas réel observé : un signal marginal au micro-capital (0,02$) mais très
    # rentable à l'échelle flashloan (5,67$) doit être classé sur son profit
    # flashloan, pas sur son profit micro-capital.
    signal = {
        "net_profit_usd": 0.02,
        "would_execute_flashloan": True,
        "flashloan_net_profit_usd": 5.67,
    }
    assert _effective_profit_usd(signal) == 5.67


def test_effective_profit_falls_back_to_net_profit_if_flashloan_preview_missing():
    # would_execute_flashloan=True mais flashloan_net_profit_usd absent (ne
    # devrait normalement pas arriver, mais ne doit jamais faire planter le tri).
    signal = {
        "net_profit_usd": 0.03,
        "would_execute_flashloan": True,
        "flashloan_net_profit_usd": None,
    }
    assert _effective_profit_usd(signal) == 0.03


def test_effective_profit_sorting_ranks_flashloan_signal_first():
    small_reliable = {
        "net_profit_usd": 0.09,
        "would_execute_flashloan": False,
        "flashloan_net_profit_usd": None,
    }
    big_flashloan = {
        "net_profit_usd": 0.02,
        "would_execute_flashloan": True,
        "flashloan_net_profit_usd": 3.06,
    }
    ranked = sorted([small_reliable, big_flashloan], key=_effective_profit_usd, reverse=True)
    assert ranked[0] is big_flashloan
