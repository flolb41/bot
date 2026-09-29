"""Moteur principal : boucle de trading (paper ou live)."""
from __future__ import annotations

import logging
import os
import signal
import time

from bot.config import Config
from bot.db import Database
from bot.dex import PancakeQuoter
from bot.exchange import ExchangeClient
from bot.notifier import TelegramNotifier
from bot.portfolio import PaperPortfolio
from bot.rewards import RewardsCollector
from bot.risk import RiskManager, RiskParams
from bot.strategies import Signal, get_strategy
from bot.treasury import Treasury
from bot.wallet import Wallet


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
        # En live, plafonne le capital considéré par le bot (0 = pas de plafond)
        self.max_capital: float = float(trading_cfg.get("max_capital", 0) or 0)

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

        dex_cfg = config.get("dex", {}) or {}
        wallet_cfg = config.get("wallet", {}) or {}
        self.quoter: PancakeQuoter | None = None
        self.dex_notional = float(dex_cfg.get("notional", 100.0))
        self.dex_alert_pct = float(dex_cfg.get("alert_spread_pct", 1.0))
        if dex_cfg.get("enabled", False):
            self.quoter = PancakeQuoter(
                rpc_url=wallet_cfg.get("rpc_url", "https://bsc-dataseed.binance.org"),
                router=dex_cfg.get("router", "0x10ED43C718714eb63d5aA57B78B54704E256024E"),
                tokens=dex_cfg.get("tokens") or {},
                quote_currency=self.quote_currency,
            )

        sweep_cfg = wallet_cfg.get("auto_sweep", {}) or {}
        self.treasury: Treasury | None = None
        self._last_sweep_check = 0.0
        if sweep_cfg.get("enabled", False):
            rpc = wallet_cfg.get("rpc_url", "https://bsc-dataseed.binance.org")
            chain_id = int(wallet_cfg.get("chain_id", 56))
            self.treasury = Treasury(
                bot_wallet=Wallet(wallet_cfg.get("keystore_path", "data/keystore.json"), rpc, chain_id),
                vault_wallet=Wallet(wallet_cfg.get("vault_keystore_path", "data/keystore_vault.json"), rpc, chain_id),
                usdt_contract=wallet_cfg.get("usdt_contract", "0x55d398326f99059fF775485246999027B3197955"),
                usdt_decimals=int(wallet_cfg.get("usdt_decimals", 18)),
                sweep_threshold=float(sweep_cfg.get("threshold", 200.0)),
                keep_on_bot=float(sweep_cfg.get("keep_on_bot", 50.0)),
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
        balance = self.exchange.fetch_balance(self.quote_currency)
        if self.max_capital > 0:
            invested = sum(p["entry_price"] * p["amount"] for p in self.db.get_all_positions())
            balance = min(balance, max(0.0, self.max_capital - invested))
        return balance

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
        if self.mode == "live":
            amount = self.exchange.amount_to_precision(symbol, amount)
            if amount <= 0:
                self.logger.warning("Quantité trop faible pour %s après arrondi, achat ignoré", symbol)
                return
        cost = price * amount
        fee = cost * (self.fee_pct / 100.0)
        if self.mode == "live":
            order = self.exchange.create_market_order(symbol, "buy", amount)
            filled = float(order.get("filled") or amount)
            avg = float(order.get("average") or price)
            fee = float((order.get("fee") or {}).get("cost") or fee)
            amount, price, cost = filled, avg, avg * filled
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
        if self.mode == "live":
            amount = self.exchange.amount_to_precision(symbol, amount)
        cost = price * amount
        fee = cost * (self.fee_pct / 100.0)

        if self.mode == "live":
            order = self.exchange.create_market_order(symbol, "sell", amount)
            filled = float(order.get("filled") or amount)
            avg = float(order.get("average") or price)
            fee = float((order.get("fee") or {}).get("cost") or fee)
            amount, price, cost = filled, avg, avg * filled
        else:
            self.portfolio.apply_sell(cost - fee)
        pnl = (price - position["entry_price"]) * amount - fee

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

    def _record_spread(self, symbol: str, cex_price: float) -> None:
        """Cote le prix DEX et enregistre l'écart CEX/DEX (lecture seule, aucun gas)."""
        if self.quoter is None:
            return
        try:
            dex_price = self.quoter.quote_price(symbol, self.dex_notional)
            spread = self.quoter.spread_pct(cex_price, dex_price)
            self.db.set_spread(symbol, cex_price, dex_price, spread)
            if abs(spread) >= self.dex_alert_pct:
                msg = f"📊 Écart CEX/DEX {symbol}: {spread:+.2f}% (CEX {cex_price:.2f} / DEX {dex_price:.2f})"
                self.logger.info(msg)
                self.notifier.send(msg)
        except Exception as exc:
            self.logger.debug("Cotation DEX indisponible pour %s: %s", symbol, exc)

    def _manage_onchain_treasury(self) -> None:
        """Toutes les heures : déplace l'excédent USDT du wallet BOT vers le VAULT."""
        if self.treasury is None or time.time() - self._last_sweep_check < 3600:
            return
        self._last_sweep_check = time.time()
        password = os.environ.get("WALLET_PASSWORD", "")
        if not password or not self.treasury.ready():
            return
        try:
            tx = self.treasury.sweep_to_vault(password)
            if tx:
                self.notifier.send(f"🔐 Excédent USDT sécurisé dans le VAULT — tx {tx}")
        except Exception:
            self.logger.exception("Erreur lors du transfert vers le vault")

    def process_symbol(self, symbol: str) -> None:
        df = self.exchange.fetch_ohlcv_df(symbol, self.timeframe, limit=max(200, self.strategy.min_candles + 10))
        current_price = float(df["close"].iloc[-1])
        self._record_spread(symbol, current_price)
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

            self._manage_treasury()
            self._manage_onchain_treasury()

            if self.mode == "paper" and self.portfolio is not None:
                self.portfolio.snapshot(self._open_positions_value())
            elif self.mode == "live":
                try:
                    balance = self._get_balance()
                    self.db.record_equity(balance, balance + self._open_positions_value())
                except Exception:
                    self.logger.exception("Snapshot equity impossible")

            elapsed = time.time() - cycle_start
            sleep_time = max(1.0, self.poll_interval - elapsed)
            for _ in range(int(sleep_time)):
                if self._stop:
                    break
                time.sleep(1)

        self.notifier.send("🛑 Bot arrêté")
        self.logger.info("Bot arrêté proprement.")
