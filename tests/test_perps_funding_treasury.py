"""Tests du balayage de funding et de l'observation de trésorerie (perps papier)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.perps.funding_scan import rank_funding, run_funding_scan
from app.perps.market_data import funding_rows_from_snapshot
from app.perps.models import FundingScanConfig, PerpsConfig, TreasuryConfig
from app.perps.service import run_periodic_jobs
from app.perps.storage import PerpsStore
from app.perps.treasury import build_observation, decode_aave_liquidity_rate, decode_uint, run_treasury_observation

T0 = 1_800_000_000.0
H = 24 * 365


def _row(symbol, short_h, long_h, rollover_h=0.0, oi=(1000.0, 1000.0), market_open=True, at=T0):
    return {
        "observed_at": at, "symbol": symbol, "funding_long_pct_h": long_h, "funding_short_pct_h": short_h,
        "rollover_pct_h": rollover_h, "oi_long_usd": oi[0], "oi_short_usd": oi[1], "market_open": market_open,
    }


def test_funding_rows_from_snapshot_flattens_sdk_pairs():
    pair = SimpleNamespace(
        symbol="ETH/USD", funding_rate=SimpleNamespace(long=0.0012, short=-0.0012),
        margin_fee=SimpleNamespace(long=0.0, short=0.0), open_interest=SimpleNamespace(long=347_168.0, short=356_805.0),
        coin_oi=SimpleNamespace(long=164.0, short=168.0),
        is_market_open=True,
    )
    broken = SimpleNamespace(symbol="X/USD")   # pas de funding_rate -> ignoré
    coins_only = SimpleNamespace(symbol="Y/USD", funding_rate=SimpleNamespace(long=0.0, short=0.0),
                                 coin_oi=SimpleNamespace(long=1.0, short=1.0))   # pas de prix dans le snapshot -> 0 $
    rows = funding_rows_from_snapshot(SimpleNamespace(pairs=[pair, broken, coins_only]), observed_at=T0)
    assert len(rows) == 2
    assert rows[0]["symbol"] == "ETH/USD" and rows[0]["funding_short_pct_h"] == -0.0012 and rows[0]["oi_long_usd"] == 347_168.0
    assert rows[1]["oi_long_usd"] == 0.0


def test_rank_funding_uses_net_apr_and_flags_spot_hedgeable():
    config = FundingScanConfig(spot_hedgeable=("ETH/USD", "AERO/USD"))
    rows = [
        _row("ETH/USD", short_h=-0.0012, long_h=0.0012),                       # short +10,5 % net
        _row("AERO/USD", short_h=-0.00125, long_h=0.00125, rollover_h=0.0017),  # 11 % − 15 % = −4 % net
        _row("XMR/USD", short_h=-0.0126, long_h=0.011),                         # short +110 %, pas de spot
    ]
    ranked = rank_funding(rows, config)
    assert [r["symbol"] for r in ranked] == ["XMR/USD", "ETH/USD", "AERO/USD"]
    assert ranked[0]["spot_hedgeable"] is False and ranked[1]["spot_hedgeable"] is True
    assert ranked[2]["best_net_apr_pct"] == pytest.approx((0.00125 - 0.0017) * H)
    assert ranked[1]["best_side"] == "short"


def test_run_funding_scan_alerts_once_per_cooldown(tmp_path):
    store = PerpsStore(tmp_path / "scan.db")
    config = FundingScanConfig(alert_min_net_apr_pct=15, alert_cooldown_hours=12, alert_min_oi_usd=500, spot_hedgeable=("ETH/USD",))
    rows = [_row("XMR/USD", short_h=-0.0126, long_h=0.011), _row("ETH/USD", short_h=-0.0012, long_h=0.0012),
            _row("CLOSED/USD", short_h=-0.05, long_h=0.05, market_open=False),
            _row("DEAD/USD", short_h=-0.05, long_h=0.05, oi=(0.0, 0.0))]   # funding énorme mais marché vide

    alerts = run_funding_scan(store, config, rows, T0)
    assert len(alerts) == 1 and "XMR/USD" in alerts[0] and "biais directionnel" in alerts[0]
    assert "moyenne 7 j +110" in alerts[0]                                           # 1 balayage : moyenne = instantané
    assert run_funding_scan(store, config, rows, T0 + 3600) == []                    # cooldown
    assert len(run_funding_scan(store, config, rows, T0 + 13 * 3600)) == 1           # expiré
    assert len(store.latest_funding_scan()) == 4
    assert store.get_state("last_funding_scan_at") == str(T0 + 13 * 3600)
    assert len([e for e in store.recent_events() if e["strategy"] == "funding_scan"]) == 2


def test_rank_funding_reports_rolling_average_of_best_side(tmp_path):
    store = PerpsStore(tmp_path / "avg.db")
    config = FundingScanConfig()
    # Trois balayages : le short ETH encaissait 0,0006 puis 0,0009 puis 0,0012 %/h.
    for i, short_h in enumerate((-0.0006, -0.0009, -0.0012)):
        store.record_funding_scan([_row("ETH/USD", short_h=short_h, long_h=-short_h, at=T0 + i * 1800)])
    averages = store.funding_scan_averages(T0 - 1)
    assert averages["ETH/USD"]["samples"] == 3
    ranked = rank_funding(store.latest_funding_scan(), config, averages)
    assert ranked[0]["best_net_apr_pct"] == pytest.approx(0.0012 * H)       # instantané : 10,5 %
    assert ranked[0]["avg_net_apr_pct"] == pytest.approx(0.0009 * H)        # moyenne : 7,9 %
    assert ranked[0]["avg_samples"] == 3 and ranked[0]["oi_total_usd"] == 2000.0
    # Fenêtre trop récente : pas de moyenne.
    assert rank_funding(store.latest_funding_scan(), config, store.funding_scan_averages(T0 + 10 * 86400))[0]["avg_net_apr_pct"] is None


def test_aave_and_balance_decoders():
    rate_ray = 48786662290443916770045446
    data = "0x" + "00" * 32 + "00" * 32 + rate_ray.to_bytes(32, "big").hex() + "00" * 32 * 12
    assert decode_aave_liquidity_rate(data) == pytest.approx(4.8787, rel=1e-3)
    assert decode_uint("0x" + (10_347_899).to_bytes(32, "big").hex()) == 10_347_899
    with pytest.raises(ValueError):
        decode_aave_liquidity_rate("0x00")


def test_build_observation_accumulates_missed_yield(tmp_path):
    store = PerpsStore(tmp_path / "treasury.db")
    first = build_observation(store, T0, idle_usdc=10.0, aave_apr=4.88, vault={"7d": 25.6, "30d": 25.5, "12m": 8.9},
                              max_gap_seconds=7200)
    assert first["best_venue"] == "veranta_vault_30d" and first["missed_yield_usd"] == 0.0
    store.record_treasury_observation(first)
    second = build_observation(store, T0 + 3600, idle_usdc=10.0, aave_apr=4.88, vault={"7d": None, "30d": None, "12m": None},
                               max_gap_seconds=7200)
    # Une heure à 4,88 % sur 10 USDC (le vault est indisponible : Aave devient la référence).
    assert second["best_venue"] == "aave_usdc"
    assert second["missed_yield_usd"] == pytest.approx(10 * 0.0488 * 3600 / (365 * 86400))
    store.record_treasury_observation(second)
    # Coupure de 3 jours : l'écart est plafonné à max_gap pour ne pas gonfler le cumul.
    third = build_observation(store, T0 + 3600 + 3 * 86400, idle_usdc=10.0, aave_apr=4.88, vault={}, max_gap_seconds=7200)
    assert third["missed_yield_usd"] - second["missed_yield_usd"] == pytest.approx(10 * 0.0488 * 7200 / (365 * 86400))


class _FakeTreasury:
    async def aave_usdc_apr_pct(self):
        return 4.88

    async def vault_returns_pct(self):
        return {"7d": 25.6, "30d": 25.5, "12m": 8.9}

    async def idle_usdc(self):
        return 10.35

    async def deposited_usdc(self):
        return 8.0


class _FakeMarket:
    def __init__(self):
        self.snapshot_calls = 0

    async def pair_state(self, symbol):
        raise AssertionError("non utilisé ici")

    async def funding_snapshot(self):
        self.snapshot_calls += 1
        return [_row("ETH/USD", short_h=-0.0012, long_h=0.0012)]

    async def close(self):
        pass


def test_periodic_jobs_respect_their_own_cadence(tmp_path):
    store = PerpsStore(tmp_path / "periodic.db")
    config = PerpsConfig.from_config({"perps": {"funding_scan": {"interval_minutes": 30}, "treasury": {"interval_minutes": 60}}})
    market = _FakeMarket()

    first = asyncio.run(run_periodic_jobs(store, config, market, _FakeTreasury(), now=T0))
    assert first["funding_pairs"] == 1 and first["treasury"]["idle_usdc"] == 10.35
    again = asyncio.run(run_periodic_jobs(store, config, market, _FakeTreasury(), now=T0 + 600))
    assert again == {} and market.snapshot_calls == 1                       # trop tôt pour les deux
    later = asyncio.run(run_periodic_jobs(store, config, market, _FakeTreasury(), now=T0 + 1900))
    assert "funding_pairs" in later and "treasury" not in later             # 31 min : funding oui, trésorerie non
    assert store.latest_treasury_observation()["best_venue"] == "veranta_vault_30d"


def test_treasury_config_and_observation_run(tmp_path):
    store = PerpsStore(tmp_path / "treasury-run.db")
    config = TreasuryConfig.from_dict({"wallet_address": "0x032d16Ec1F4382287cBBFbB87Dd8163934A01803", "interval_minutes": 60})
    row = asyncio.run(run_treasury_observation(store, config, _FakeTreasury(), T0))
    assert row["aave_usdc_apr_pct"] == 4.88 and row["vault_12m_apr_pct"] == 8.9 and row["deposited_usdc"] == 8.0
    latest = store.latest_treasury_observation()
    assert latest["idle_usdc"] == 10.35 and latest["deposited_usdc"] == 8.0


def test_treasury_table_migration_adds_deposited_column(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE perps_treasury_observations (observed_at REAL PRIMARY KEY, idle_usdc REAL, aave_usdc_apr_pct REAL, "
        "vault_7d_apr_pct REAL, vault_30d_apr_pct REAL, vault_12m_apr_pct REAL, best_apr_pct REAL, best_venue TEXT, "
        "missed_yield_usd REAL NOT NULL DEFAULT 0);"
        "INSERT INTO perps_treasury_observations (observed_at, idle_usdc, missed_yield_usd) VALUES (1, 10.0, 0.5);"
    )
    conn.commit(); conn.close()
    store = PerpsStore(path)                                    # applique la migration sans perdre la ligne
    latest = store.latest_treasury_observation()
    assert latest["idle_usdc"] == 10.0 and latest["deposited_usdc"] is None
    store.record_treasury_observation({"observed_at": 2, "idle_usdc": 2.0, "deposited_usdc": 8.0, "missed_yield_usd": 0.5})
    assert store.latest_treasury_observation()["deposited_usdc"] == 8.0
