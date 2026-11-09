"""Tests du harnais de backtest tendance (parité avec le moteur papier, cache, walk-forward)."""
from __future__ import annotations

import asyncio
import json
import math
import random
from pathlib import Path

import pytest

from app.perps import backtest as bt
from app.perps.engine import STRATEGY_TREND, PaperEngine
from app.perps.models import PairState, PerpsConfig
from app.perps.storage import PerpsStore

T0 = 1_780_000_000.0 - 1_780_000_000.0 % 86400   # minuit UTC
HOUR = 3600.0


def _flat_candles(closes: list[float], start: float = T0, bar: float = HOUR) -> list[bt.Candle]:
    """Bougies sans mèche : open = high = low = close -> intra-bougie == clôture."""
    return [bt.Candle(start + i * bar, c, c, c, c) for i, c in enumerate(closes)]


def _random_walk(n: int, seed: int = 7, start_price: float = 2000.0, vol: float = 0.004) -> list[bt.Candle]:
    rng = random.Random(seed)
    candles, price = [], start_price
    for i in range(n):
        o = price
        c = o * (1 + rng.gauss(0, vol))
        hi = max(o, c) * (1 + abs(rng.gauss(0, vol / 2)))
        lo = min(o, c) * (1 - abs(rng.gauss(0, vol / 2)))
        candles.append(bt.Candle(T0 + i * HOUR, o, hi, lo, c))
        price = c
    return candles


def _state(price: float, now: float, costs: bt.CostModel) -> PairState:
    return PairState(
        symbol="ETH/USD", price=price, spread_pct=costs.spread_pct, open_fee_pct=costs.open_fee_pct,
        close_fee_pct=costs.close_fee_pct, funding_long_pct_h=costs.funding_long_pct_h,
        funding_short_pct_h=costs.funding_short_pct_h, rollover_pct_h=costs.rollover_pct_h, oi_long_usd=1e6,
        oi_short_usd=1e6, min_notional_usd=100.0, max_leverage=50.0, market_open=True, observed_at=now,
    )


def test_parity_with_paper_engine_on_close_only_mode(tmp_path):
    """Le simulateur « clôture seule » doit reproduire trade pour trade le moteur
    papier alimenté par les mêmes clôtures (un tick par bougie horaire)."""
    rng = random.Random(3)
    closes, price = [], 2000.0
    for _ in range(400):
        price *= 1 + rng.gauss(0.0005, 0.008)   # dérive + bruit : déclenche longs, shorts, stops, objectifs, durée max
        closes.append(price)
    candles = _flat_candles(closes)
    costs = bt.CostModel(spread_pct=0.03, open_fee_pct=0.045, close_fee_pct=0.045,
                         funding_long_pct_h=0.0012, funding_short_pct_h=-0.0012, rollover_pct_h=0.0005)
    config = PerpsConfig.from_config({"perps": {
        "interval_minutes": 60, "markets": ["ETH/USD"], "funding_carry": {"enabled": False},
        "trend": {"markets": ["ETH/USD"], "lookback_minutes": 240, "confirm_minutes": 60, "entry_pct": 1.0,
                  "stop_loss_pct": 1.5, "take_profit_pct": 3.0, "max_hold_hours": 24, "reentry_cooldown_minutes": 120,
                  "daily_loss_limit_usd": 5, "notional_usd": 100, "leverage": 2},
    }})
    store = PerpsStore(tmp_path / "parity.db")
    engine = PaperEngine(store, config)
    for candle in candles:
        now = candle.time + HOUR
        engine.process_tick({"ETH/USD": _state(candle.close, now, costs)}, now)
    engine_trades = store.closed_positions(limit=1000)
    engine_trades.sort(key=lambda p: p["opened_at"])

    result = bt.simulate_trend(candles, bt.TrendParams.from_trend_config(config.trend), costs, intrabar=False)

    assert len(result.trades) >= 5, "le scénario doit produire plusieurs trades pour que la parité ait un sens"
    assert len(result.trades) == len(engine_trades)
    for sim, eng in zip(result.trades, engine_trades):
        assert sim.side == eng["side"] and sim.exit_reason == eng["exit_reason"]
        assert sim.opened_at == float(eng["opened_at"]) and sim.closed_at == float(eng["closed_at"])
        assert sim.entry_price == pytest.approx(float(eng["entry_price"]), rel=1e-12)
        assert sim.exit_price == pytest.approx(float(eng["exit_price"]), rel=1e-12)
        assert sim.net_pnl_usd == pytest.approx(float(eng["net_pnl_usd"]), abs=1e-9)
    reasons = {t.exit_reason for t in result.trades}
    assert reasons & {"stop_loss", "take_profit", "duree_max"}


def test_intrabar_stop_first_and_gap_fill():
    costs = bt.CostModel(spread_pct=0.0, open_fee_pct=0.0, close_fee_pct=0.0, funding_long_pct_h=0.0, funding_short_pct_h=0.0)
    params = bt.TrendParams(lookback_minutes=60, confirm_minutes=60, entry_pct=1.0, stop_loss_pct=1.0, take_profit_pct=2.0,
                            max_hold_hours=48, reentry_cooldown_minutes=0, daily_loss_limit_usd=1e9)
    # Barre 0 : 100 ; barre 1 : 101,5 (+1,5 % -> long à la clôture) ; barre 2 : touche stop ET objectif -> stop d'abord.
    candles = [
        bt.Candle(T0, 100, 100, 100, 100), bt.Candle(T0 + HOUR, 100, 101.5, 100, 101.5),
        bt.Candle(T0 + 2 * HOUR, 101.5, 104.0, 100.0, 103.0),
    ]
    result = bt.simulate_trend(candles, params, costs, intrabar=True)
    assert [t.exit_reason for t in result.trades] == ["stop_loss"]
    assert result.trades[0].exit_price == pytest.approx(101.5 * 0.99)
    # Gap à l'ouverture sous le stop : exécuté au prix d'ouverture, pas au niveau du stop.
    candles[2] = bt.Candle(T0 + 2 * HOUR, 99.0, 99.5, 98.0, 99.0)
    result = bt.simulate_trend(candles, params, costs, intrabar=True)
    assert result.trades[0].exit_reason == "stop_loss" and result.trades[0].exit_price == pytest.approx(99.0)
    # Objectif seul touché -> exécuté au niveau de l'objectif.
    candles[2] = bt.Candle(T0 + 2 * HOUR, 101.5, 104.0, 101.2, 103.0)
    result = bt.simulate_trend(candles, params, costs, intrabar=True)
    assert result.trades[0].exit_reason == "take_profit" and result.trades[0].exit_price == pytest.approx(101.5 * 1.02)


def test_liquidation_costs_whole_collateral():
    costs = bt.CostModel(spread_pct=0.0, open_fee_pct=0.0, close_fee_pct=0.0, funding_long_pct_h=0.0, funding_short_pct_h=0.0)
    params = bt.TrendParams(lookback_minutes=60, confirm_minutes=60, entry_pct=1.0, stop_loss_pct=50.0, take_profit_pct=90.0,
                            max_hold_hours=48, reentry_cooldown_minutes=0, daily_loss_limit_usd=1e9, leverage=10)
    candles = [bt.Candle(T0, 100, 100, 100, 100), bt.Candle(T0 + HOUR, 100, 102, 100, 102),
               bt.Candle(T0 + 2 * HOUR, 102, 102, 90, 95)]   # −8,5 % à 10x = liquidation
    result = bt.simulate_trend(candles, params, costs, intrabar=True)
    assert result.trades[0].exit_reason == "liquidation"
    assert result.trades[0].net_pnl_usd == pytest.approx(-10.0)   # collatéral 100/10
    assert result.metrics()["liquidations"] == 1


def test_metrics_profit_factor_and_drawdown():
    mk = lambda net, reason="stop_loss": bt.Trade("long", T0, T0 + HOUR, 1, 1, 100, 50, net, 0, 0, 0, net, reason, 1)  # noqa: E731
    metrics = bt.compute_metrics([mk(2.0, "take_profit"), mk(-1.0), mk(-3.0), mk(4.0, "take_profit")], bars=100, start=T0, end=T0 + 30 * 86400)
    assert metrics["trades"] == 4 and metrics["win_rate_pct"] == 50
    assert metrics["profit_factor"] == pytest.approx(6 / 4)
    assert metrics["net_pnl_usd"] == pytest.approx(2.0) and metrics["max_drawdown_usd"] == pytest.approx(4.0)
    assert metrics["exits"] == {"take_profit": 2, "stop_loss": 2} and metrics["exposure_pct"] == 4
    assert bt.compute_metrics([], 10, T0, T0 + HOUR)["profit_factor"] is None
    assert bt.compute_metrics([mk(1.0)], 10, T0, T0 + HOUR)["profit_factor"] == math.inf


def test_load_candles_uses_cache_then_fetches_only_the_tail(tmp_path):
    calls: list[tuple] = []

    async def fetcher(symbol, resolution, start, end):
        calls.append((symbol, resolution, start, end))
        return [{"time": (start - start % HOUR + i * HOUR) * 1000, "open": 1, "high": 2, "low": 0.5, "close": 1.5}
                for i in range(int((end - start) // HOUR) + 2)]   # inclut une bougie en cours

    now = T0 + 200 * HOUR
    first = asyncio.run(bt.load_candles("ETH/USD", "60", 7, now=now, cache_dir=tmp_path, fetcher=fetcher))
    assert calls[0][0] == "Crypto.ETH/USD" and calls[0][2] == int(now - 7 * 86400)
    assert all(c.time + HOUR <= now for c in first) and len(first) == 7 * 24
    cached = json.loads(bt.cache_path("ETH/USD", "60", tmp_path).read_text())
    assert cached and cached[0]["time"] > 1e11   # millisecondes, format du feed

    # Même fenêtre : rien à aller chercher.
    second = asyncio.run(bt.load_candles("ETH/USD", "60", 7, now=now, cache_dir=tmp_path, fetcher=fetcher))
    assert len(calls) == 1 and len(second) == len(first)
    # Six heures plus tard : seule la fin manque.
    third = asyncio.run(bt.load_candles("ETH/USD", "60", 7, now=now + 6 * HOUR, cache_dir=tmp_path, fetcher=fetcher))
    assert len(calls) == 2 and calls[1][2] >= int(now - 2 * HOUR) and len(third) == 7 * 24
    # Cache seul.
    offline = asyncio.run(bt.load_candles("ETH/USD", "60", 3, now=now + 6 * HOUR, cache_dir=tmp_path, fetcher=None))
    assert len(calls) == 2 and len(offline) == 3 * 24
    with pytest.raises(ValueError):
        asyncio.run(bt.load_candles("ETH/USD", "15", 1, now=now, cache_dir=tmp_path, fetcher=None))


def test_walk_forward_reports_baseline_and_best_out_of_sample():
    candles = _random_walk(1500, seed=11)
    base = bt.TrendParams(reentry_cooldown_minutes=0, daily_loss_limit_usd=1e9)
    grid = {"entry_pct": (0.5, 1.0), "stop_loss_pct": (1.0, 2.0)}
    report = bt.walk_forward(candles, base, bt.CostModel(), symbol="ETH/USD", resolution="60", grid=grid, min_trades=1)
    assert report["combinations"] == 4 and len(report["top"]) == 4
    assert report["train_period"][1] == report["test_period"][0] == candles[int(1500 * 0.7)].time
    assert report["baseline"]["train"]["trades"] > 0 and "test" in report["best"]
    # Les trades de test commencent après la coupure (la chauffe n'en produit pas).
    assert report["best"]["test"]["bars"] == 1500 - int(1500 * 0.7)
    text = bt.verdict(report)
    assert "ETH/USD 60" in text and "Verdict" in text
    assert report["top"] == sorted(report["top"], key=lambda r: r["train"]["net_pnl_usd"], reverse=True)


def test_grid_default_size_and_params_overrides():
    assert len(bt.grid_combinations()) == 3 * 4 * 4 * 4
    params = bt.TrendParams().with_overrides(entry_pct=2.5)
    assert params.entry_pct == 2.5 and params.stop_loss_pct == 1.5
    assert bt.feed_symbol("BTC/USD") == "Crypto.BTC/USD" and bt.feed_symbol("Crypto.BTC/USD") == "Crypto.BTC/USD"
    assert bt.cache_path("Crypto.ETH/USD", "D", Path("x")) == Path("x") / "Crypto_ETH-USD_D.json"
