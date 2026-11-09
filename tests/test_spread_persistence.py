"""Tests de scripts/spread_persistence.py (analyse pure, sans base réelle)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "spread_persistence", Path(__file__).resolve().parents[1] / "scripts" / "spread_persistence.py",
)
sp = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sp)

T0 = 1_800_000_000.0


def _row(t: float, spread: float, *, token="BRETT", quote="WETH", buy="aerodrome", sell="pancakeswap",
         net=-0.05, we=0, liq=600_000.0, rid=None) -> dict:
    return {
        "id": rid or int(t), "detected_at": T0 + t, "token_symbol": token, "quote_symbol": quote, "buy_dex": buy,
        "sell_dex": sell, "spread_pct": spread, "net_profit_usd": net, "would_execute": we,
        "buy_liquidity_usd": liq, "sell_liquidity_usd": liq,
    }


def test_episodes_split_on_gap_and_threshold():
    rows = [
        _row(0, 1.0), _row(30, 1.1), _row(60, 0.5), _row(90, 1.2),          # 0.5 coupe l'épisode (sous le seuil)
        _row(1000, 0.9),                                                     # gap > max-gap : nouvel épisode
        _row(0, 2.0, token="ICNT", quote="USDC", buy="uniswap", sell="pancakeswap", we=1, net=0.02),
    ]
    episodes = sp.build_episodes(rows, threshold_pct=0.7, max_gap_seconds=180)
    by_route = {e["route"]: [] for e in episodes}
    for e in episodes:
        by_route[e["route"]].append(e)
    brett = sorted(by_route["BRETT/WETH aerodrome→pancakeswap"], key=lambda e: e["started_at"])
    assert [e["scans"] for e in brett] == [2, 1, 1]
    assert brett[0]["duration_seconds"] == 30 and brett[0]["spread_max"] == 1.1
    assert by_route["ICNT/USDC uniswap→pancakeswap"][0]["would_execute"] == 1


def test_buckets_and_route_verdicts():
    structural = [_row(i * 30, 1.0 + (0.01 if i % 2 else 0.0)) for i in range(100)]   # 50 min, écart figé
    fleeting = [_row(10_000 + i * 1000, 1.5, token="AERO", quote="USDC", buy="uniswap", sell="aerodrome") for i in range(3)]
    episodes = sp.build_episodes(structural + fleeting, threshold_pct=0.7, max_gap_seconds=180)
    buckets = sp.bucket_episodes(episodes)
    assert buckets["1 scan"]["episodes"] == 3 and buckets[">20 scans"]["episodes"] == 1
    assert buckets["1 scan"]["share_pct"] == pytest.approx(75.0)

    routes = {r["route"]: r for r in sp.route_table(episodes)}
    assert routes["BRETT/WETH aerodrome→pancakeswap"]["verdict"].startswith("structurel")
    assert routes["AERO/USDC uniswap→aerodrome"]["verdict"].startswith("fugace")
    assert routes["BRETT/WETH aerodrome→pancakeswap"]["time_above_threshold_h"] == pytest.approx(99 * 30 / 3600)


def test_next_scan_survival_counts_follow_ups():
    rows = [
        _row(0, 1.5, we=1), _row(30, 1.4, we=1), _row(60, 0.2),             # 1er survit, 2e disparaît
        _row(5000, 1.5, we=1),                                               # pas de scan suivant proche
    ]
    survival = sp.next_scan_survival(rows, threshold_pct=0.7, max_gap_seconds=180)
    assert survival == {
        "would_execute_signals": 3, "still_above_threshold_next_scan": 1, "gone_next_scan": 1,
        "no_follow_up_scan": 1, "survival_pct": 50.0,
    }


def test_live_reality_parses_fresh_spread_and_outcomes():
    live = [
        {"id": 1, "token_symbol": "ICNT", "spread_pct": 4.59, "tx_hash": None, "tx_hash_buy": "a", "tx_hash_sell": "b",
         "execution_error": "Écart refermé avant la 2e jambe (0.531% < 0.550% de frais minimum) — ...", "realized_pnl_usd": -0.0098},
        {"id": 2, "token_symbol": "ICNT", "spread_pct": 1.36, "tx_hash": None, "tx_hash_buy": "c", "tx_hash_sell": None,
         "execution_error": "Jambe achat échouée : Transaction échouée on-chain (status=0)", "realized_pnl_usd": -0.0024},
        {"id": 3, "token_symbol": "WETH", "spread_pct": 1.5, "tx_hash": None, "tx_hash_buy": "d", "tx_hash_sell": "e",
         "execution_error": None, "realized_pnl_usd": 0.03},
        {"id": 5, "token_symbol": "BRETT", "spread_pct": 1.1, "tx_hash": None, "tx_hash_buy": None, "tx_hash_sell": None,
         "execution_error": "Flashloan échoué (revert complet, aucun fonds déplacé) : Simulation échouée", "realized_pnl_usd": None},
        {"id": 4, "token_symbol": "AERO", "spread_pct": 0.9, "tx_hash": None, "tx_hash_buy": None, "tx_hash_sell": None,
         "execution_error": "Écart refermé avant la jambe d'achat (-0.185% < 0.550% de frais minimum) — aucune transaction envoyée.",
         "realized_pnl_usd": None},
    ]
    result = sp.live_reality(live)
    assert result["attempts"] == 5
    assert result["outcomes"] == {"écart refermé": 2, "revert on-chain (gas perdu)": 1, "succès complet": 1,
                                  "refus en simulation (aucune tx)": 1}
    assert [c["fresh_spread_pct"] for c in result["detected_vs_fresh"]] == [0.531, -0.185]
    assert result["avg_fresh_minus_detected_pct"] == pytest.approx(((0.531 - 4.59) + (-0.185 - 0.9)) / 2)
    assert result["realized_pnl_total_usd"] == pytest.approx(-0.0098 - 0.0024 + 0.03)
    assert result["realized_winning_trades"] == 1


def test_report_and_text_rendering_end_to_end():
    rows = [_row(i * 30, 1.0) for i in range(80)] + [_row(10_000, 1.2, token="AERO", quote="USDC", buy="uniswap", sell="aerodrome", we=1, net=0.01)]
    report = sp.build_report(rows, [], threshold_pct=0.7, max_gap_seconds=180, days=30)
    text = sp.render_text(report)
    assert report["signals_total"] == 81 and report["episodes_total"] == 2
    assert "structurel" in text and "fugace" in text
    assert "Réalité des tentatives live (0)" in text


def test_load_rows_reads_sqlite_read_only(tmp_path):
    from app.database import Database

    db = Database(tmp_path / "signals.db")
    db.add_arbitrage_signal({
        "chain": "base", "token_symbol": "AERO", "token_address": "0x" + "a" * 40, "quote_symbol": "USDC",
        "quote_address": "0x" + "b" * 40, "buy_dex": "uniswap", "buy_price_usd": 1.0, "buy_liquidity_usd": 1e5,
        "sell_dex": "aerodrome", "sell_price_usd": 1.01, "sell_liquidity_usd": 1e5, "spread_pct": 1.0,
        "trade_size_usd": 2.0, "gross_profit_usd": 0.02, "fees_usd": 0.01, "slippage_buffer_usd": 0.0,
        "gas_cost_usd": 0.0, "net_profit_usd": 0.01, "would_execute": 1, "mode": "simulation",
    })
    rows, live = sp.load_rows(str(tmp_path / "signals.db"), since=0)
    assert len(rows) == 1 and rows[0]["token_symbol"] == "AERO" and live == []
