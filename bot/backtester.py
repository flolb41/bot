"""Backtest vectoriel/séquentiel sur données historiques réelles (via ccxt), sans risque financier."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import pandas as pd

from bot.exchange import ExchangeClient
from bot.risk import RiskManager, RiskParams
from bot.strategies import Signal, Strategy, get_strategy


@dataclass
class BacktestResult:
    symbol: str
    trades: list[dict] = field(default_factory=list)
    final_balance: float = 0.0
    starting_balance: float = 0.0

    @property
    def total_return_pct(self) -> float:
        if self.starting_balance == 0:
            return 0.0
        return (self.final_balance - self.starting_balance) / self.starting_balance * 100.0

    @property
    def win_rate_pct(self) -> float:
        closed = [t for t in self.trades if t["side"] == "sell"]
        if not closed:
            return 0.0
        wins = sum(1 for t in closed if t["pnl"] > 0)
        return wins / len(closed) * 100.0

    @property
    def num_trades(self) -> int:
        return sum(1 for t in self.trades if t["side"] == "sell")

    @property
    def max_drawdown_pct(self) -> float:
        if not self.trades:
            return 0.0
        equity_curve = []
        balance = self.starting_balance
        for t in self.trades:
            if t["side"] == "sell":
                balance += t["pnl"]
            equity_curve.append(balance)
        peak = equity_curve[0]
        max_dd = 0.0
        for value in equity_curve:
            peak = max(peak, value)
            dd = (peak - value) / peak * 100.0 if peak > 0 else 0.0
            max_dd = max(max_dd, dd)
        return max_dd


def fetch_historical(exchange: ExchangeClient, symbol: str, timeframe: str, days: int) -> pd.DataFrame:
    """Récupère `days` jours de bougies en paginant les appels ccxt (limite ~1000 par appel)."""
    timeframe_minutes = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}
    minutes = timeframe_minutes.get(timeframe, 15)
    candles_needed = int(days * 24 * 60 / minutes)

    all_rows: list[list] = []
    since = exchange.exchange.milliseconds() - candles_needed * minutes * 60_000
    while len(all_rows) < candles_needed:
        batch = exchange.exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=1000)
        if not batch:
            break
        all_rows.extend(batch)
        since = batch[-1][0] + 1
        if len(batch) < 1000:
            break

    df = pd.DataFrame(all_rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df = df.drop_duplicates(subset="timestamp").reset_index(drop=True)
    return df.tail(candles_needed).reset_index(drop=True)


def run_backtest(
    df: pd.DataFrame,
    symbol: str,
    strategy: Strategy,
    risk: RiskManager,
    starting_balance: float,
    allocation_pct: float,
    fee_pct: float,
) -> BacktestResult:
    result = BacktestResult(symbol=symbol, starting_balance=starting_balance)
    balance = starting_balance
    position: dict | None = None
    highest_since_entry = 0.0

    min_candles = strategy.min_candles
    # Fenêtre bornée : les EMA (adjust=False) convergent bien avant 10x la période max,
    # inutile de recalculer sur tout l'historique à chaque bougie.
    lookback = max(300, min_candles * 10)
    for i in range(min_candles, len(df)):
        window = df.iloc[max(0, i + 1 - lookback) : i + 1]
        price = float(window["close"].iloc[-1])

        if position is not None:
            highest_since_entry = max(highest_since_entry, price)
            trailing = risk.updated_trailing_stop(price, highest_since_entry, position["trailing_stop"])
            position["trailing_stop"] = trailing
            reason = risk.should_exit(position["entry_price"], price, position["stop_loss"], position["take_profit"], trailing)
            if reason is None and strategy.generate_signal(window) == Signal.SELL:
                reason = "signal"
            if reason:
                cost = price * position["amount"]
                fee = cost * (fee_pct / 100.0)
                pnl = (price - position["entry_price"]) * position["amount"] - fee
                balance += cost - fee
                result.trades.append({"side": "sell", "price": price, "pnl": pnl, "reason": reason, "timestamp": window["timestamp"].iloc[-1]})
                position = None
            continue

        signal_ = strategy.generate_signal(window)
        if signal_ == Signal.BUY:
            amount = risk.position_size(balance, price, allocation_pct)
            if amount > 0:
                cost = price * amount
                fee = cost * (fee_pct / 100.0)
                balance -= cost + fee
                stop_loss, take_profit = risk.compute_stop_and_target(price)
                position = {
                    "entry_price": price,
                    "amount": amount,
                    "stop_loss": stop_loss,
                    "take_profit": take_profit,
                    "trailing_stop": None,
                }
                highest_since_entry = price
                result.trades.append({"side": "buy", "price": price, "pnl": 0.0, "reason": "signal", "timestamp": window["timestamp"].iloc[-1]})

    # Clôture forcée de la position ouverte à la fin de la période pour calculer le solde final
    if position is not None:
        price = float(df["close"].iloc[-1])
        cost = price * position["amount"]
        fee = cost * (fee_pct / 100.0)
        pnl = (price - position["entry_price"]) * position["amount"] - fee
        balance += cost - fee
        result.trades.append({"side": "sell", "price": price, "pnl": pnl, "reason": "end_of_backtest", "timestamp": df["timestamp"].iloc[-1]})

    result.final_balance = balance
    return result


def print_report(result: BacktestResult) -> None:
    logger = logging.getLogger("bot.backtest")
    logger.info("=" * 50)
    logger.info("Résultats backtest pour %s", result.symbol)
    logger.info("Solde initial     : %.2f", result.starting_balance)
    logger.info("Solde final       : %.2f", result.final_balance)
    logger.info("Rendement total   : %.2f %%", result.total_return_pct)
    logger.info("Nombre de trades  : %d", result.num_trades)
    logger.info("Taux de réussite  : %.2f %%", result.win_rate_pct)
    logger.info("Drawdown max      : %.2f %%", result.max_drawdown_pct)
    logger.info("=" * 50)
