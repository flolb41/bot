"""Moteur principal : boucle de trading (paper ou live)."""
from __future__ import annotations

import logging
import os
import signal
import time
from dataclasses import dataclass

import pandas as pd

from bot.config import Config
from bot.db import Database
from bot.dex import PancakeQuoter
from bot.exchange import ExchangeClient
from bot.indicators import atr
from bot.notifier import TelegramNotifier
from bot.portfolio import PaperPortfolio
from bot.rewards import RewardsCollector
from bot.risk import RiskManager, RiskParams
from bot.strategies import Signal, Strategy, get_strategy
from bot.treasury import Treasury
from bot.wallet import Wallet


@dataclass
class StrategySlot:
    """Une stratégie active avec son propre risque, timeframe, symboles et allocation."""

    name: str
    strategy: Strategy
    risk: RiskManager
    timeframe: str
    symbols: list[str]
    allocation_pct: float


def build_strategy_slots(config: Config, db: Database) -> list[StrategySlot]:
    """Construit les slots depuis `strategies:` (liste) ou, à défaut, `strategy:` (unique)."""
    trading_cfg = config["trading"]
    base_risk = config["risk"]
    entries = config.get("strategies")
    if not entries:
        entries = [config["strategy"]]

    slots: list[StrategySlot] = []
    for entry in entries:
        if not entry.get("enabled", True):
            continue
        risk_over = entry.get("risk", {}) or {}
        risk = RiskManager(
            RiskParams(
                max_open_positions=int(risk_over.get("max_open_positions", base_risk["max_open_positions"])),
                stop_loss_pct=float(risk_over.get("stop_loss_pct", base_risk["stop_loss_pct"])),
                take_profit_pct=float(risk_over.get("take_profit_pct", base_risk["take_profit_pct"])),
                trailing_stop_pct=float(risk_over.get("trailing_stop_pct", base_risk.get("trailing_stop_pct", 0))),
                max_daily_loss_pct=float(risk_over.get("max_daily_loss_pct", base_risk["max_daily_loss_pct"])),
                atr_stop_mult=float(risk_over.get("atr_stop_mult", base_risk.get("atr_stop_mult", 0))),
                atr_tp_mult=float(risk_over.get("atr_tp_mult", base_risk.get("atr_tp_mult", 0))),
                atr_trailing_mult=float(risk_over.get("atr_trailing_mult", base_risk.get("atr_trailing_mult", 0))),
            ),
            db,
        )
        slots.append(
            StrategySlot(
                name=entry["name"],
                strategy=get_strategy(entry["name"], entry.get("params", {}) or {}),
                risk=risk,
                timeframe=entry.get("timeframe", trading_cfg["timeframe"]),
                symbols=list(entry.get("symbols") or trading_cfg["symbols"]),
                allocation_pct=float(entry.get("allocation_pct", trading_cfg["allocation_pct"])),
            )
        )
    if not slots:
        raise ValueError("Aucune stratégie active dans la configuration.")
    names = [s.name for s in slots]
    if len(names) != len(set(names)):
        raise ValueError(f"Noms de stratégies dupliqués: {names} (les positions sont indexées par nom).")
    return slots


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
        # Ordres live : limite IOC bornée à ±max_slippage_pct du dernier prix (0 = ordres market)
        self.max_slippage_pct: float = float(trading_cfg.get("max_slippage_pct", 0.5))
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

        self.slots = build_strategy_slots(config, self.db)
        # Limite globale de positions ouvertes toutes stratégies confondues
        self.max_open_positions = int(config["risk"]["max_open_positions"])
        self._ohlcv_cache: dict[tuple[str, str], pd.DataFrame] = {}

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
                asset=rewards_cfg.get("asset") or self.quote_currency,
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

    def _place_live_order(self, symbol: str, side: str, amount: float, ref_price: float) -> tuple[float, float, float]:
        """Passe l'ordre live et retourne (quantité exécutée, prix moyen, frais)."""
        if self.max_slippage_pct > 0:
            order = self.exchange.create_guarded_order(symbol, side, amount, ref_price, self.max_slippage_pct)
        else:
            order = self.exchange.create_market_order(symbol, side, amount)
        filled = float(order.get("filled") or 0.0)
        avg = float(order.get("average") or ref_price)
        fee = float((order.get("fee") or {}).get("cost") or (avg * filled * self.fee_pct / 100.0))
        if filled < amount:
            self.logger.warning(
                "[%s %s] exécution partielle: %.8f / %.8f (garde-fou slippage %.2f%%)",
                symbol, side, filled, amount, self.max_slippage_pct,
            )
        return filled, avg, fee

    def _execute_buy(self, slot: StrategySlot, symbol: str, price: float, amount: float, cur_atr: float | None = None) -> None:
        if self.mode == "live":
            amount = self.exchange.amount_to_precision(symbol, amount)
            if amount <= 0:
                self.logger.warning("Quantité trop faible pour %s après arrondi, achat ignoré", symbol)
                return
        cost = price * amount
        fee = cost * (self.fee_pct / 100.0)
        if self.mode == "live":
            filled, avg, fee = self._place_live_order(symbol, "buy", amount, price)
            if filled <= 0:
                self.logger.warning("[%s] achat %s non exécuté (prix hors garde-fou), on réessaiera au prochain cycle", slot.name, symbol)
                return
            amount, price, cost = filled, avg, avg * filled
        else:
            self.portfolio.apply_buy(cost + fee)

        stop_loss, take_profit = slot.risk.compute_stop_and_target(price, cur_atr)
        self.db.open_position(symbol, "long", price, amount, stop_loss, take_profit, strategy=slot.name)
        self.db.record_trade(symbol, "buy", price, amount, cost, self.mode, fee=fee, reason="signal", strategy=slot.name)
        msg = f"🟢 [{slot.name}] ACHAT {symbol} @ {price:.4f} (qty {amount:.6f}) SL={stop_loss:.4f} TP={take_profit:.4f}"
        self.logger.info(msg)
        self.notifier.send(msg)

    def _execute_sell(self, slot: StrategySlot, symbol: str, price: float, position, reason: str) -> None:
        amount = position["amount"]
        if self.mode == "live":
            amount = self.exchange.amount_to_precision(symbol, amount)
        requested = amount
        cost = price * amount
        fee = cost * (self.fee_pct / 100.0)

        if self.mode == "live":
            filled, avg, fee = self._place_live_order(symbol, "sell", amount, price)
            if filled <= 0:
                self.logger.warning("[%s] vente %s non exécutée (prix hors garde-fou), position conservée", slot.name, symbol)
                return
            amount, price, cost = filled, avg, avg * filled
        else:
            self.portfolio.apply_sell(cost - fee)
        pnl = (price - position["entry_price"]) * amount - fee

        if self.mode == "live" and amount < requested:
            self.db.update_position_amount(symbol, requested - amount, strategy=slot.name)
        else:
            self.db.close_position(symbol, strategy=slot.name)
        self.db.record_trade(symbol, "sell", price, amount, cost, self.mode, fee=fee, pnl=pnl, reason=reason, strategy=slot.name)

        if self.rewards is not None and pnl > 0:
            skimmed = self.rewards.skim_profit(pnl)
            if skimmed > 0 and self.mode == "paper":
                self.portfolio.apply_buy(skimmed)  # débite le solde de trading vers le coffre

        emoji = "\U0001f534" if pnl < 0 else "\u2705"
        msg = f"{emoji} [{slot.name}] VENTE {symbol} @ {price:.4f} PnL={pnl:.4f} ({reason})"
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

    def _ohlcv(self, symbol: str, timeframe: str, min_candles: int) -> pd.DataFrame:
        """Bougies mises en cache pour le cycle courant (partagées entre stratégies)."""
        key = (symbol, timeframe)
        df = self._ohlcv_cache.get(key)
        if df is None or len(df) < min_candles:
            df = self.exchange.fetch_ohlcv_df(symbol, timeframe, limit=max(200, min_candles + 10))
            self._ohlcv_cache[key] = df
        return df

    def process_slot_symbol(self, slot: StrategySlot, symbol: str) -> None:
        df = self._ohlcv(symbol, slot.timeframe, slot.strategy.min_candles)
        current_price = float(df["close"].iloc[-1])
        cur_atr = float(atr(df, 14).iloc[-1]) if slot.risk.uses_atr() else None
        position = self.db.get_position(symbol, strategy=slot.name)

        if position is not None:
            highest = max(position["entry_price"], current_price)
            trailing = slot.risk.updated_trailing_stop(current_price, highest, position["trailing_stop"], cur_atr)
            if trailing != position["trailing_stop"]:
                self.db.update_trailing_stop(symbol, trailing, strategy=slot.name)

            exit_reason = slot.risk.should_exit(
                position["entry_price"], current_price, position["stop_loss"], position["take_profit"], trailing
            )
            if exit_reason is None and slot.strategy.generate_signal(df) == Signal.SELL:
                exit_reason = "signal"

            if exit_reason:
                self._execute_sell(slot, symbol, current_price, position, exit_reason)
            return

        # Pas de position ouverte pour cette stratégie : chercher une opportunité d'achat
        balance = self._get_balance()
        open_count = len(self.db.get_all_positions())
        if open_count >= self.max_open_positions or not slot.risk.can_open_new_position(open_count, balance):
            return

        if slot.strategy.generate_signal(df) == Signal.BUY:
            amount = slot.risk.position_size(balance, current_price, slot.allocation_pct)
            if amount > 0:
                self._execute_buy(slot, symbol, current_price, amount, cur_atr)

    def run_cycle(self) -> None:
        """Un passage complet : chaque stratégie sur chacun de ses symboles."""
        self._ohlcv_cache.clear()
        spread_done: set[str] = set()
        for slot in self.slots:
            for symbol in slot.symbols:
                if self._stop:
                    return
                try:
                    self.process_slot_symbol(slot, symbol)
                    if symbol not in spread_done:
                        df = self._ohlcv_cache.get((symbol, slot.timeframe))
                        if df is not None:
                            self._record_spread(symbol, float(df["close"].iloc[-1]))
                        spread_done.add(symbol)
                except Exception:
                    self.logger.exception("Erreur [%s] lors du traitement de %s", slot.name, symbol)
                    self.notifier.send(f"⚠️ Erreur [{slot.name}] sur {symbol}, voir les logs")

    def run(self) -> None:
        self.logger.info(
            "Démarrage du bot | mode=%s | exchange=%s | stratégies=%s",
            self.mode,
            self.config["exchange"]["name"],
            [f"{s.name}@{s.timeframe} {s.symbols} trailing={s.risk.params.trailing_stop_pct}" for s in self.slots],
        )
        self.notifier.send(
            f"🤖 Bot démarré en mode {self.mode} — {len(self.slots)} stratégie(s): {', '.join(s.name for s in self.slots)}"
        )

        while not self._stop:
            cycle_start = time.time()
            self.run_cycle()

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
