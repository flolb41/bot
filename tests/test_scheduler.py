"""Tests de la logique pure (pas de réseau/RPC/scheduler APScheduler réel) de
app.scheduler : calcul du profit effectif utilisé pour classer les signaux
rentables entre eux (quel candidat tenter en premier dans un cycle)."""
from pathlib import Path
import logging
import threading

import pytest

from app.database import Database
from app.scheduler import _effective_profit_usd, run_arbitrage_scan_job


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


def test_brett_priority_only_changes_order_of_already_profitable_candidates():
    config = {
        "trading": {
            "token_priority_multipliers": {
                "0x532f27101965dd16442e59d40670faf5ebb142e4": 1.25,
            },
        },
    }
    brett = {
        "token_address": "0x532f27101965dd16442e59d40670faf5ebb142e4",
        "net_profit_usd": 1.0,
        "would_execute": True,
    }
    other_token = {"token_address": "0x" + "1" * 40, "net_profit_usd": 1.2, "would_execute": True}

    assert _effective_profit_usd(brett, config) == 1.25
    assert _effective_profit_usd(other_token, config) == 1.2
    ranked = sorted(
        (signal for signal in (other_token, brett) if signal["would_execute"]),
        key=lambda signal: _effective_profit_usd(signal, config),
        reverse=True,
    )
    assert ranked == [brett, other_token]
    assert brett["net_profit_usd"] == 1.0


def test_aero_priority_applies_to_triangular_signals_without_changing_profit():
    aero_address = "0x940181a94a35a4569e4529a3cdfb74e38fd98631"
    config = {"trading": {"token_priority_multipliers": {aero_address: 1.25}}}
    aero_cycle = {
        "token_b_address": aero_address,
        "net_profit_usd": 1.0,
        "would_execute": True,
    }

    assert _effective_profit_usd(aero_cycle, config) == 1.25
    assert aero_cycle["net_profit_usd"] == 1.0


def test_scan_job_applies_runtime_settings_to_plain_config_mapping(tmp_path: Path, monkeypatch):
    import app.scheduler as scheduler
    import app.trading.paper_strategies as paper_strategies

    db = Database(tmp_path / "scheduler.db")
    config = {"trading": {"enabled": False}}
    monkeypatch.setattr(scheduler, "collect_pairs_for_config", lambda _config: {})
    monkeypatch.setattr(scheduler, "run_arbitrage_scan", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(scheduler, "run_triangular_scan", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(scheduler, "run_cross_dex_triangular_scan", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(paper_strategies, "run_paper_experiments", lambda *_args, **_kwargs: [])

    assert run_arbitrage_scan_job(db, config=config) == []
    assert db.get_state("last_scan_completed_at") is not None


def test_concurrent_scan_trigger_is_skipped_until_active_scan_finishes(monkeypatch, tmp_path):
    import app.scheduler as scheduler

    started = threading.Event()
    release = threading.Event()
    calls = []

    def blocked_scan(*_args, **_kwargs):
        calls.append(True)
        started.set()
        assert release.wait(timeout=5)
        return [{"scan": "complete"}]

    monkeypatch.setattr(scheduler, "_run_arbitrage_scan_job", blocked_scan)
    db = Database(tmp_path / "concurrent-scan.db")
    first_result = []
    first = threading.Thread(target=lambda: first_result.extend(
        scheduler.run_arbitrage_scan_job(db)
    ))
    first.start()
    assert started.wait(timeout=5)

    try:
        assert scheduler.run_arbitrage_scan_job(db) == []
        assert calls == [True]
    finally:
        release.set()
        first.join(timeout=5)

    assert not first.is_alive()
    assert first_result == [{"scan": "complete"}]


def _cross_dex_signal(db: Database) -> dict:
    signal = {
        "chain": "base",
        "token_a_symbol": "WETH", "token_a_address": "0x4200000000000000000000000000000000000006",
        "token_b_symbol": "B", "token_b_address": "0x" + "1" * 40,
        "token_c_symbol": "C", "token_c_address": "0x" + "2" * 40,
        "dex_ab": "sushiswap", "dex_bc": "baseswap", "dex_ca": "swapbased",
        "cycle_multiplier": 1.1, "spread_pct": 10.0, "min_liquidity_usd": 100_000,
        "token_b_usd_price": 1.0, "token_c_usd_price": 0.5,
    }
    signal["id"] = db.add_cross_dex_triangular_signal(signal)
    return signal


def _prepare_cross_dex_scan(monkeypatch, signal):
    import app.scheduler as scheduler
    import app.trading.directional_live as directional_live
    import app.trading.executor as executor
    import app.trading.paper_strategies as paper_strategies

    monkeypatch.setattr(scheduler, "collect_pairs_for_config", lambda _config: {})
    monkeypatch.setattr(scheduler, "run_arbitrage_scan", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(scheduler, "run_triangular_scan", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(paper_strategies, "run_paper_experiments", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(directional_live, "run_live_directional_strategy", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(executor, "recover_open_positions", lambda *_args: [])
    monkeypatch.setattr(scheduler, "is_stopped", lambda _db: False)
    monkeypatch.setattr(
        "app.trading.runtime_settings.apply_runtime_settings",
        lambda _db, config: config,
    )
    monkeypatch.setattr(
        "app.trading.flashloan.estimate_cross_dex_triangular_flashloan_preview",
        lambda *_args: {"notional_usd": 20.0, "net_profit_usd": 1.0},
    )


def test_cross_dex_scheduler_keeps_live_execution_disabled_by_default(tmp_path: Path, monkeypatch):
    import app.scheduler as scheduler
    import app.trading.guardrails as guardrails

    db = Database(tmp_path / "cross-dex-disabled.db")
    signal = _cross_dex_signal(db)
    _prepare_cross_dex_scan(monkeypatch, signal)
    monkeypatch.setattr(
        scheduler, "run_cross_dex_triangular_scan",
        lambda *_args, **_kwargs: [signal],
    )
    monkeypatch.setattr(guardrails, "is_trading_live", lambda _config: True)
    monkeypatch.setattr(
        "app.trading.flashloan.execute_cross_dex_triangular_signal_via_flashloan",
        lambda *_args: pytest.fail("cross-DEX execution must remain opt-in"),
    )

    returned = run_arbitrage_scan_job(
        db,
        config={"trading": {"flashloan_enabled": True, "min_liquidity_usd": 10_000}},
    )

    assert returned == []


def test_cross_dex_scheduler_routes_only_after_dedicated_opt_in(tmp_path: Path, monkeypatch):
    import app.scheduler as scheduler
    import app.trading.executor as executor
    import app.trading.guardrails as guardrails

    db = Database(tmp_path / "cross-dex-enabled.db")
    signal = _cross_dex_signal(db)
    _prepare_cross_dex_scan(monkeypatch, signal)
    scan_results = iter(([signal], []))
    monkeypatch.setattr(
        scheduler, "run_cross_dex_triangular_scan",
        lambda *_args, **_kwargs: next(scan_results),
    )
    monkeypatch.setattr(guardrails, "is_trading_live", lambda _config: True)
    monkeypatch.setattr(executor, "recover_open_positions", lambda *_args: [])
    executions = []
    monkeypatch.setattr(
        "app.trading.flashloan.execute_cross_dex_triangular_signal_via_flashloan",
        lambda *_args: executions.append(signal) or {
            "executed": True, "tx_hash": "0xsimulated", "error": None,
        },
    )
    config = {
        "trading": {
            "flashloan_enabled": True,
            "cross_dex_flashloan_enabled": True,
            "min_liquidity_usd": 10_000,
        },
    }

    returned = run_arbitrage_scan_job(db, config=config)

    assert executions == [signal]
    assert returned == [signal]
    stored = db.fetch_one(
        "SELECT flashloan_notional_usd, flashloan_net_profit_usd "
        "FROM cross_dex_triangular_signals WHERE id = ?",
        (signal["id"],),
    )
    assert stored["flashloan_notional_usd"] == 20.0
    assert stored["flashloan_net_profit_usd"] == 1.0


def test_cross_dex_scheduler_enforces_relative_flashloan_profit_floor(tmp_path: Path, monkeypatch):
    import app.scheduler as scheduler
    import app.trading.guardrails as guardrails

    db = Database(tmp_path / "cross-dex-relative-floor.db")
    signal = _cross_dex_signal(db)
    _prepare_cross_dex_scan(monkeypatch, signal)
    monkeypatch.setattr(
        scheduler, "run_cross_dex_triangular_scan",
        lambda *_args, **_kwargs: [signal],
    )
    monkeypatch.setattr(guardrails, "is_trading_live", lambda _config: True)
    monkeypatch.setattr(
        "app.trading.flashloan.estimate_cross_dex_triangular_flashloan_preview",
        lambda *_args: {"notional_usd": 1_000.0, "net_profit_usd": 4.0},
    )
    monkeypatch.setattr(
        "app.trading.flashloan.execute_cross_dex_triangular_signal_via_flashloan",
        lambda *_args: pytest.fail("cross-DEX must meet the relative profit floor"),
    )

    returned = run_arbitrage_scan_job(
        db,
        config={
            "trading": {
                "flashloan_enabled": True,
                "cross_dex_flashloan_enabled": True,
                "flashloan_min_net_profit_usd": 0.25,
                "min_net_profit_pct": 0.5,
                "min_liquidity_usd": 10_000,
            },
        },
    )

    assert returned == [signal]
    assert signal["would_execute_flashloan"] is False


def _pair_live_signal(db: Database) -> dict:
    signal = {
        "chain": "base", "token_symbol": "ICNT", "token_address": "0x" + "e" * 40,
        "quote_symbol": "USDC", "quote_address": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
        "buy_dex": "uniswap", "buy_price_usd": 0.38, "buy_liquidity_usd": 100_000.0,
        "sell_dex": "pancakeswap", "sell_price_usd": 0.398, "sell_liquidity_usd": 100_000.0,
        "spread_pct": 4.5, "trade_size_usd": 0.54, "gross_profit_usd": 0.02, "fees_usd": 0.003,
        "slippage_buffer_usd": 0.001, "gas_cost_usd": 0.004, "net_profit_usd": 0.016,
        "would_execute": True, "would_execute_classic": True, "would_execute_flashloan": True,
        "flashloan_net_profit_usd": 17.0, "mode": "live",
    }
    signal["id"] = db.add_arbitrage_signal(signal)
    return signal


def _prepare_pair_live_scan(monkeypatch, signal):
    import app.scheduler as scheduler
    import app.trading.directional_live as directional_live
    import app.trading.executor as executor
    import app.trading.guardrails as guardrails
    import app.trading.paper_strategies as paper_strategies

    monkeypatch.setattr(scheduler, "collect_pairs_for_config", lambda _config: {})
    monkeypatch.setattr(scheduler, "run_arbitrage_scan", lambda *_args, **_kwargs: [signal])
    monkeypatch.setattr(scheduler, "run_triangular_scan", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(scheduler, "run_cross_dex_triangular_scan", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(paper_strategies, "run_paper_experiments", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(directional_live, "run_live_directional_strategy", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(executor, "recover_open_positions", lambda *_args: [])
    monkeypatch.setattr(scheduler, "is_stopped", lambda _db: False)
    monkeypatch.setattr(guardrails, "is_trading_live", lambda _config: True)
    monkeypatch.setattr("app.trading.runtime_settings.apply_runtime_settings", lambda _db, config: config)
    monkeypatch.setattr("app.trading.flashloan.pair_signal_is_flashloan_eligible", lambda _config, _signal: True)


def test_flashloan_refusal_is_logged_and_persisted_before_classic_fallback(tmp_path: Path, monkeypatch, caplog):
    db = Database(tmp_path / "flashloan-fallback.db")
    signal = _pair_live_signal(db)
    _prepare_pair_live_scan(monkeypatch, signal)
    calls = []
    monkeypatch.setattr(
        "app.trading.flashloan.execute_pair_signal_via_flashloan",
        lambda *_args: calls.append("flashloan") or {
            "executed": False, "tx_hash": None,
            "error": "Profit net estimé après prime Aave+gas (0.0100$) < seuil flashloan (0.2500$)",
        },
    )
    monkeypatch.setattr(
        "app.trading.executor.execute_signal",
        lambda *_args: calls.append("classic") or {
            "executed": False, "tx_hash_buy": None, "tx_hash_sell": None,
            "error": "Écart refermé avant la jambe d'achat (0.531% < 0.550% de frais minimum) — aucune transaction envoyée.",
        },
    )

    with caplog.at_level(logging.INFO, logger="app.scheduler"):
        returned = run_arbitrage_scan_job(db, config={"trading": {"flashloan_enabled": True}})

    assert returned == [signal]
    assert calls == ["flashloan", "classic"]
    titles = [n["title"] for n in db.list_notifications(limit=10)]
    assert "↩️ Repli flashloan → classique" in titles
    assert "⚠️ Arbitrage refusé/échoué" in titles
    messages = [record.getMessage() for record in caplog.records]
    assert any("Tentative d'exécution LIVE : ICNT/USDC" in m for m in messages)
    assert any("Flashloan refusé/échoué pour ICNT/USDC" in m and "seuil flashloan" in m for m in messages)
    assert any(
        "Exécution d'arbitrage refusée/échouée (ICNT/USDC)" in m and "Écart refermé avant la jambe d'achat" in m
        for m in messages
    )
    # Refus avant tout envoi : cooldown court, aucun capital engagé.
    assert not any("Capital engagé on-chain" in m for m in messages)


def test_successful_live_execution_is_logged(tmp_path: Path, monkeypatch, caplog):
    import app.scheduler as scheduler

    db = Database(tmp_path / "live-success.db")
    signal = _pair_live_signal(db)
    _prepare_pair_live_scan(monkeypatch, signal)
    monkeypatch.setattr("app.trading.flashloan.pair_signal_is_flashloan_eligible", lambda _config, _signal: False)
    monkeypatch.setattr(scheduler, "_MAX_ATTEMPTS_PER_CYCLE", 1)
    monkeypatch.setattr(
        "app.trading.executor.execute_signal",
        lambda *_args: {"executed": True, "tx_hash_buy": "buyhash", "tx_hash_sell": "sellhash", "error": None},
    )

    with caplog.at_level(logging.INFO, logger="app.scheduler"):
        run_arbitrage_scan_job(db, config={"trading": {}})

    messages = [record.getMessage() for record in caplog.records]
    assert any("Exécution LIVE réussie : ICNT/USDC" in m and "buyhash" in m for m in messages)
