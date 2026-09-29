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
    base_params = dict(strat_cfg.get("params", {}))
    strategies = args.strategy.split(",") if args.strategy else [strat_cfg["name"]]
    emas = [tuple(int(v) for v in pair.split("/")) for pair in args.ema.split(",")] if args.ema else [None]

    data: dict[tuple[str, str], object] = {}
    for symbol, tf in itertools.product(symbols, timeframes):
        print(f"Téléchargement {symbol} {tf} ({args.days} j)...", file=sys.stderr)
        data[(symbol, tf)] = fetch_historical(exchange, symbol, tf, args.days)

    rows = []
    for strat_name, tf, trailing, stop, tp, ema in itertools.product(strategies, timeframes, trailings, stops, tps, emas):
        risk = RiskManager(RiskParams(stop_loss_pct=stop, take_profit_pct=tp, trailing_stop_pct=trailing), db)
        # Les params de la config ne s'appliquent qu'à la stratégie configurée ; les autres prennent leurs défauts
        params = dict(base_params) if strat_name == strat_cfg["name"] else {}
        if ema:
            params.update(ema_fast=ema[0], ema_slow=ema[1])
        strategy = get_strategy(strat_name, params)
        ema_label = f"{ema[0]}/{ema[1]}" if ema else "-"
        total_ret, total_trades, wins, closed, max_dd = 0.0, 0, 0, 0, 0.0
        per_symbol = []
        for symbol in symbols:
            r = run_backtest(
                data[(symbol, tf)], symbol, strategy, risk,
                starting_balance=float(trading["starting_balance"]),
                allocation_pct=float(trading["allocation_pct"]),
                fee_pct=float(trading.get("fee_pct", 0.1)),
            )
            total_ret += r.total_return_pct
            total_trades += r.num_trades
            sells = [t for t in r.trades if t["side"] == "sell"]
            wins += sum(1 for t in sells if t["pnl"] > 0)
            closed += len(sells)
            max_dd = max(max_dd, r.max_drawdown_pct)
            per_symbol.append(f"{r.total_return_pct:+.2f}")
        rows.append([
            strat_name, tf, ema_label, trailing or "off", stop, tp,
            f"{total_ret / len(symbols):+.2f} %",
            " / ".join(per_symbol),
            total_trades,
            f"{(wins / closed * 100) if closed else 0:.0f} %",
            f"{max_dd:.2f} %",
        ])

    rows.sort(key=lambda r: float(r[6].split()[0]), reverse=True)
    print(f"\nGrille sur {args.days} jours — {', '.join(symbols)}\n")
    print(tabulate(rows, headers=["Stratégie", "TF", "EMA", "Trailing %", "SL %", "TP %", "Rdt moyen", "Rdt par symbole", "Trades", "Win", "DD max"]))


if __name__ == "__main__":
    main()
