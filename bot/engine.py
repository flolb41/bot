"""Moteur principal : boucle de trading (paper ou live)."""
from __future__ import annotations

import logging
import signal
import time

from bot.config import Config
from bot.db import Database
from bot.exchange import ExchangeClient
from bot.notifier import TelegramNotifier
from bot.portfolio import PaperPortfolio
from bot.rewards import RewardsCollector
from bot.risk import RiskManager, RiskParams
from bot.strategies import Signal, get_strategy


class TradingEngine:
    def __init__(self, config: Config):
        self.config = config
        self.logger = logging.getLogger("bot.engine")
        self._stop = False

        trading_cfg = config["trading"]
        self.mode: str = trading_cfg["mode"]
        self.symbols: list[str] = trading_cfg["symbols"]
        self.timeframe: str = trading_cfg["timeframe"]
        self.poll_interval: int = int(trading_cfg["poll_interval_seconds"])
        self.quote_currency: str = trading_cfg["quote_currency"]
        self.allocation_pct: float = float(trading_cfg["allocation_pct"])
        self.fee_pct: float = float(trading_cfg.get("fee_pct", 0.1))

        exch_cfg = config["exchange"]
        self.exchange = ExchangeClient(
            name=exch_cfg["name"],
            api_key=exch_cfg.get("api_key", ""),
            api_secret=exch_cfg.get("api_secret", ""),
            sandbox=bool(exch_cfg.get("sandbox", True)),
        )

        self.db = Database(config["database"]["path"])

        strat_cfg = config["strategy"]
        self.strategy = get_strategy(strat_cfg["name"], strat_cfg.get("params", {}))

        risk_cfg = config["risk"]
        self.risk = RiskManager(
            RiskParams(
                max_open_positions=int(risk_cfg["max_open_positions"]),
                stop_loss_pct=float(risk_cfg["stop_loss_pct"]),
                take_profit_pct=float(risk_cfg["take_profit_pct"]),
                trailing_stop_pct=float(risk_cfg.get("trailing_stop_pct", 0)),
                max_daily_loss_pct=float(risk_cfg["max_daily_loss_pct"]),
            ),
            self.db,
        )

        tg_cfg = config.get("telegram", {})
        self.notifier = TelegramNotifier(
            enabled=bool(tg_cfg.get("enabled", False)),
            bot_token=tg_cfg.get("bot_token", ""),
            chat_id=tg_cfg.get("chat_id", ""),
        )

        self.portfolio = None
        if self.mode == "paper":
            self.portfolio = PaperPortfolio(self.db, float(trading_cfg["starting_balance"]))

        rewards_cfg = config.get("rewards", {}) or {}
        self.rewards: RewardsCollector | None = None
        self.rewards_interval = int(rewards_cfg.get("collect_interval_hours", 6)) * 3600
        self._last_collect = 0.0
        if rewards_cfg.get("enabled", False):
            self.rewards = RewardsCollector(
                db=self.db,
                mode=self.mode,
                exchange=self.exchange,
                notifier=self.notifier,
                asset=self.quote_currency,
                paper_apr_pct=float(rewards_cfg.get("paper_apr_pct", 5.0)),
                profit_skim_pct=float(rewards_cfg.get("profit_skim_pct", 30.0)),
                min_idle_to_subscribe=float(rewards_cfg.get("min_idle_to_subscribe", 50.0)),
                keep_free=float(rewards_cfg.get("keep_free", 100.0)),
                min_trading_balance=float(rewards_cfg.get("min_trading_balance", 50.0)),
                dust_enabled=bool(rewards_cfg.get("dust_enabled", True)),
            )

        signal.signal(signal.SIGINT, self._handle_stop)
        signal.signal(signal.SIGTERM, self._handle_stop)

    def _handle_stop(self, signum, frame) -> None:  # noqa: ARG002
        self.logger.info("Signal d'arrêt reçu, extinction propre du bot...")
        self._stop = True

    # ------------------------------------------------------------------
    def _get_balance(self) -> float:
        if self.mode == "paper":
            return self.portfolio.balance
        return self.exchange.fetch_balance(self.quote_currency)

    def _open_positions_value(self) -> float:
        total = 0.0
        for pos in self.db.get_all_positions():
            try:
                price = self.exchange.fetch_ticker_price(pos["symbol"])
            except Exception:
                price = pos["entry_price"]
            total += price * pos["amount"]
        return total

    def _execute_buy(self, symbol: str, price: float, amount: float) -> None:
        cost = price * amount
        fee = cost * (self.fee_pct / 100.0)
        if self.mode == "live":
            self.exchange.create_market_order(symbol, "buy", amount)
        else:
            self.portfolio.apply_buy(cost + fee)

        stop_loss, take_profit = self.risk.compute_stop_and_target(price)
        self.db.open_position(symbol, "long", price, amount, stop_loss, take_profit)
        self.db.record_trade(symbol, "buy", price, amount, cost, self.mode, fee=fee, reason="signal")
        msg = f"🟢 ACHAT {symbol} @ {price:.4f} (qty {amount:.6f}) SL={stop_loss:.4f} TP={take_profit:.4f}"
        self.logger.info(msg)
        self.notifier.send(msg)

    def _execute_sell(self, symbol: str, price: float, position, reason: str) -> None:
        amount = position["amount"]
        cost = price * amount
        fee = cost * (self.fee_pct / 100.0)
        pnl = (price - position["entry_price"]) * amount - fee

        if self.mode == "live":
            self.exchange.create_market_order(symbol, "sell", amount)
        else:
            self.portfolio.apply_sell(cost - fee)

        self.db.close_position(symbol)
        self.db.record_trade(symbol, "sell", price, amount, cost, self.mode, fee=fee, pnl=pnl, reason=reason)

        if self.rewards is not None and pnl > 0:
            skimmed = self.rewards.skim_profit(pnl)
            if skimmed > 0 and self.mode == "paper":
                self.portfolio.apply_buy(skimmed)  # débite le solde de trading vers le coffre

        emoji = "🔴" if pnl < 0 else "✅"
        msg = f"{emoji} VENTE {symbol} @ {price:.4f} PnL={pnl:.4f} ({reason})"
        self.logger.info(msg)
        self.notifier.send(msg)

    # ------------------------------------------------------------------
    def _manage_treasury(self) -> None:
        """Gère le coffre : collecte périodique, recharge du capital, placement du solde inactif."""
        if self.rewards is None:
            return
        try:
            balance = self._get_balance()

            refilled = self.rewards.refill_if_needed(balance)
            if refilled > 0 and self.mode == "paper":
                self.portfolio.apply_sell(refilled)

            if time.time() - self._last_collect >= self.rewards_interval:
                self.rewards.collect()
                subscribed = self.rewards.subscribe_idle(self._get_balance())
                if subscribed > 0 and self.mode == "paper":
                    self.portfolio.apply_buy(subscribed)
                self._last_collect = time.time()
        except Exception:
            self.logger.exception("Erreur dans la gestion du coffre")

    def process_symbol(self, symbol: str) -> None:
        df = self.exchange.fetch_ohlcv_df(symbol, self.timeframe, limit=max(200, self.strategy.min_candles + 10))
        current_price = float(df["close"].iloc[-1])
        position = self.db.get_position(symbol)

        if position is not None:
            highest = max(position["entry_price"], current_price)
            trailing = self.risk.updated_trailing_stop(current_price, highest, position["trailing_stop"])
            if trailing != position["trailing_stop"]:
                self.db.update_trailing_stop(symbol, trailing)

            exit_reason = self.risk.should_exit(
                position["entry_price"], current_price, position["stop_loss"], position["take_profit"], trailing
            )
            if exit_reason is None:
                signal_ = self.strategy.generate_signal(df)
                if signal_ == Signal.SELL:
                    exit_reason = "signal"

            if exit_reason:
                self._execute_sell(symbol, current_price, position, exit_reason)
            return

        # Pas de position ouverte : chercher une opportunité d'achat
        balance = self._get_balance()
        open_count = len(self.db.get_all_positions())
        if not self.risk.can_open_new_position(open_count, balance):
            return

        signal_ = self.strategy.generate_signal(df)
        if signal_ == Signal.BUY:
            amount = self.risk.position_size(balance, current_price, self.allocation_pct)
            if amount > 0:
                self._execute_buy(symbol, current_price, amount)

    def run(self) -> None:
        self.logger.info(
            "Démarrage du bot | mode=%s | exchange=%s | symbols=%s | timeframe=%s",
            self.mode,
            self.config["exchange"]["name"],
            self.symbols,
            self.timeframe,
        )
        self.notifier.send(f"🤖 Bot démarré en mode {self.mode}")

        while not self._stop:
            cycle_start = time.time()
            for symbol in self.symbols:
                if self._stop:
                    break
                try:
                    self.process_symbol(symbol)
                except Exception:
                    self.logger.exception("Erreur lors du traitement de %s", symbol)
                    self.notifier.send(f"⚠️ Erreur sur {symbol}, voir les logs")

            if self.mode == "paper" and self.portfolio is not None:
                self.portfolio.snapshot(self._open_positions_value())

            self._manage_treasury()

            elapsed = time.time() - cycle_start
            sleep_time = max(1.0, self.poll_interval - elapsed)
            for _ in range(int(sleep_time)):
                if self._stop:
                    break
                time.sleep(1)

        self.notifier.send("🛑 Bot arrêté")
        self.logger.info("Bot arrêté proprement.")
