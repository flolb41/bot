#!/usr/bin/env python3
"""Grille de backtests : compare timeframes et paramètres de risque sur plusieurs symboles.

Usage : python scripts/compare_backtests.py --days 90
Les données sont téléchargées une fois par (symbole, timeframe) puis réutilisées.
"""
from __future__ import annotations

import argparse
import itertools
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tabulate import tabulate  # noqa: E402

from bot.backtester import fetch_historical, run_backtest  # noqa: E402
from bot.config import load_config  # noqa: E402
from bot.db import Database  # noqa: E402
from bot.exchange import ExchangeClient  # noqa: E402
from bot.risk import RiskManager, RiskParams  # noqa: E402
from bot.strategies import get_strategy  # noqa: E402

logging.basicConfig(level=logging.WARNING)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--symbols", default="BTC/USDT,ETH/USDT")
    parser.add_argument("--timeframes", default="15m,1h,4h")
    parser.add_argument("--trailing", default="1.5,3,0", help="valeurs de trailing_stop_pct (0 = désactivé)")
    parser.add_argument("--stop", default="2", help="valeurs de stop_loss_pct")
    parser.add_argument("--tp", default="4", help="valeurs de take_profit_pct")
    parser.add_argument("--ema", default="", help="paires ema_fast/ema_slow, ex: 9/21,12/26,20/50 (défaut: config)")
    parser.add_argument("--strategy", default="", help="stratégies à comparer, ex: ema_rsi,bollinger_rsi (défaut: config)")
    parser.add_argument("--trend", default="0", help="valeurs de trend_ema (filtre de tendance, 0 = off), ex: 0,100,200")
    parser.add_argument("--atr", default="0", help="triplets stop/tp/trailing en multiples d'ATR, ex: 0,2/4/1.5,3/6/2 (0 = %% fixes)")
    parser.add_argument("--alloc", default="", help="valeurs d'allocation_pct, ex: 10,20,30 (défaut: config)")
    parser.add_argument("--fee", type=float, default=None, help="frais %% par ordre (défaut: config ; 0.075 = frais payés en BNB)")
    parser.add_argument("--no-cache", action="store_true", help="ignore le cache disque des bougies")
    args = parser.parse_args()

    config = load_config()
    trading = config["trading"]
    strat_cfg = config["strategy"]
    exchange = ExchangeClient(name=config["exchange"]["name"], sandbox=False)
    db = Database(":memory:")

    symbols = args.symbols.split(",")
    timeframes = args.timeframes.split(",")
    trailings = [float(x) for x in args.trailing.split(",")]
    stops = [float(x) for x in args.stop.split(",")]
    tps = [float(x) for x in args.tp.split(",")]
    trends = [int(x) for x in args.trend.split(",")]
    atrs = [None if x == "0" else tuple(float(v) for v in x.split("/")) for x in args.atr.split(",")]
    allocs = [float(x) for x in args.alloc.split(",")] if args.alloc else [float(trading["allocation_pct"])]
    fee_pct = args.fee if args.fee is not None else float(trading.get("fee_pct", 0.1))
    base_params = dict(strat_cfg.get("params", {}))
    strategies = args.strategy.split(",") if args.strategy else [strat_cfg["name"]]
    emas = [tuple(int(v) for v in pair.split("/")) for pair in args.ema.split(",")] if args.ema else [None]

    cache_dir = Path(__file__).resolve().parent.parent / "data" / "ohlcv_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    data: dict[tuple[str, str], object] = {}
    for symbol, tf in itertools.product(symbols, timeframes):
        cache_file = cache_dir / f"{symbol.replace('/', '_')}_{tf}_{args.days}d.parquet"
        if cache_file.exists() and not args.no_cache:
            import pandas as pd

            data[(symbol, tf)] = pd.read_parquet(cache_file)
            continue
        print(f"Téléchargement {symbol} {tf} ({args.days} j)...", file=sys.stderr)
        df = fetch_historical(exchange, symbol, tf, args.days)
        data[(symbol, tf)] = df
        try:
            df.to_parquet(cache_file)
        except Exception:
            pass  # pyarrow absent : pas de cache, pas grave

    rows = []
    combos = itertools.product(strategies, timeframes, trailings, stops, tps, emas, trends, atrs, allocs)
    for strat_name, tf, trailing, stop, tp, ema, trend, atr_cfg, alloc in combos:
        rp = RiskParams(stop_loss_pct=stop, take_profit_pct=tp, trailing_stop_pct=trailing)
        if atr_cfg:
            rp.atr_stop_mult, rp.atr_tp_mult, rp.atr_trailing_mult = atr_cfg
        risk = RiskManager(rp, db)
        # Les params de la config ne s'appliquent qu'à la stratégie configurée ; les autres prennent leurs défauts
        params = dict(base_params) if strat_name == strat_cfg["name"] else {}
        if ema:
            params.update(ema_fast=ema[0], ema_slow=ema[1])
        params["trend_ema"] = trend
        strategy = get_strategy(strat_name, params)
        ema_label = f"{ema[0]}/{ema[1]}" if ema else "-"
        atr_label = "/".join(f"{v:g}" for v in atr_cfg) if atr_cfg else "-"
        total_ret, total_trades, wins, closed, max_dd = 0.0, 0, 0, 0, 0.0
        per_symbol = []
        for symbol in symbols:
            r = run_backtest(
                data[(symbol, tf)], symbol, strategy, risk,
                starting_balance=float(trading["starting_balance"]),
                allocation_pct=alloc,
                fee_pct=fee_pct,
            )
            total_ret += r.total_return_pct
            total_trades += r.num_trades
            sells = [t for t in r.trades if t["side"] == "sell"]
            wins += sum(1 for t in sells if t["pnl"] > 0)
            closed += len(sells)
            max_dd = max(max_dd, r.max_drawdown_pct)
            per_symbol.append(f"{r.total_return_pct:+.2f}")
        rows.append([
            strat_name, tf, ema_label, trend or "off", atr_label, trailing or "off", stop, tp, f"{alloc:g}",
            f"{total_ret / len(symbols):+.2f} %",
            " / ".join(per_symbol),
            total_trades,
            f"{(wins / closed * 100) if closed else 0:.0f} %",
            f"{max_dd:.2f} %",
        ])

    rows.sort(key=lambda r: float(r[9].split()[0]), reverse=True)
    print(f"\nGrille sur {args.days} jours — {', '.join(symbols)} — frais {fee_pct:g} %\n")
    print(tabulate(rows, headers=["Stratégie", "TF", "EMA", "Trend", "ATR s/t/tr", "Trail %", "SL %", "TP %", "Alloc", "Rdt moyen", "Rdt par symbole", "Trades", "Win", "DD max"]))


if __name__ == "__main__":
    main()
