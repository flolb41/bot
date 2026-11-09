"""Moteur papier perps : simulation fidèle aux coûts du protocole, zéro ordre réel.

Conventions (voir `PairState`) : funding et rollover en % du notionnel par
heure, signe du point de vue du trader (positif = coût) ; le spread joue
contre le trader à l'ouverture et à la clôture ; les frais d'ouverture et de
clôture portent sur le notionnel ; une position est liquidée quand 85 % de
son collatéral est perdu (seuil Veranta, `compute.liquidation`). Le P&L net
d'un trade inclut TOUS ses coûts (frais d'ouverture compris), contrairement
à l'affichage « non réalisé » de l'interface qui déduit l'ouverture du
collatéral.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from app.perps.models import CarryConfig, PairState, PerpsConfig, TrendConfig
from app.perps.storage import PerpsStore

logger = logging.getLogger("app.perps.engine")

LIQUIDATION_THRESHOLD_PCT = 85.0
STRATEGY_CARRY = "funding_carry"
STRATEGY_TREND = "trend"


def gross_pnl_usd(side: str, entry_price: float, price: float, notional_usd: float) -> float:
    if entry_price <= 0:
        return 0.0
    move = price / entry_price - 1
    return notional_usd * (move if side == "long" else -move)


def liquidation_price(side: str, entry_price: float, collateral_usd: float, notional_usd: float,
                      fees_usd: float = 0.0) -> float:
    """Miroir de `veranta_sdk.compute.estimate_liquidation_price` :
    distance = prix × (collatéral × 85 % − frais) / notionnel."""
    if notional_usd <= 0:
        return 0.0
    distance = entry_price * (collateral_usd * LIQUIDATION_THRESHOLD_PCT / 100 - fees_usd) / notional_usd
    return entry_price - distance if side == "long" else entry_price + distance


def _utc_day_start(now: float) -> float:
    return datetime.fromtimestamp(now, timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


class PaperEngine:
    def __init__(self, store: PerpsStore, config: PerpsConfig):
        self.store = store
        self.config = config
        self.carry_cfg: CarryConfig = config.carry
        self.trend_cfg: TrendConfig = config.trend
        self._interval_seconds = config.interval_minutes * 60

    # ------------------------------------------------------------------ tick
    def process_tick(self, states: dict[str, PairState], now: float) -> dict[str, Any]:
        """Enregistre les observations, met à jour les positions ouvertes puis
        cherche de nouvelles entrées. Retourne un résumé (journal + dashboard)."""
        actions: list[str] = []
        for state in states.values():
            self.store.record_observation(state)

        for position in self.store.open_positions():
            state = states.get(position["symbol"])
            if state is None:
                continue
            self._accrue(position, state, now)
            closed = self._evaluate_exit(position, state, now)
            if closed:
                actions.append(closed)

        if self.carry_cfg.enabled:
            actions.extend(self._carry_entries(states, now))
        if self.trend_cfg.enabled:
            actions.extend(self._trend_entries(states, now))

        self.store.prune_observations(now - self.config.observation_retention_days * 86400)
        self.store.set_state("last_tick_at", str(now))
        return {"actions": actions, "open_positions": len(self.store.open_positions())}

    # --------------------------------------------------------------- accrual
    def _accrue(self, position: dict[str, Any], state: PairState, now: float) -> None:
        hours = max(0.0, (now - float(position["last_accrual_at"])) / 3600)
        if hours == 0:
            return
        notional = float(position["notional_usd"])
        funding = float(position["funding_usd"]) + notional * state.funding_pct_h(position["side"]) / 100 * hours
        rollover = float(position["rollover_usd"]) + notional * state.rollover_pct_h / 100 * hours
        # Les deux genres ont une jambe perp dont la liquidation se rapproche à
        # mesure que funding et rollover s'accumulent (ou s'éloigne si le funding
        # est encaissé) ; les carrys historiques à collatéral « capital » (2 ×
        # notionnel) gardent simplement une liquidation hors d'atteinte.
        liq = liquidation_price(
            position["side"], float(position["entry_price"]), float(position["collateral_usd"]), notional,
            fees_usd=float(position["open_fee_usd"]) + funding + rollover,
        )
        self.store.update_position(
            int(position["id"]), funding_usd=funding, rollover_usd=rollover, last_accrual_at=now, liquidation_price=liq,
        )
        position.update(funding_usd=funding, rollover_usd=rollover, last_accrual_at=now, liquidation_price=liq)

    # ----------------------------------------------------------------- exits
    def _evaluate_exit(self, position: dict[str, Any], state: PairState, now: float) -> str | None:
        if position["strategy"] == STRATEGY_CARRY:
            return self._evaluate_carry_exit(position, state, now)
        return self._evaluate_trend_exit(position, state, now)

    def _evaluate_trend_exit(self, position: dict[str, Any], state: PairState, now: float) -> str | None:
        side = position["side"]
        entry = float(position["entry_price"])
        price = state.price
        liq = position.get("liquidation_price")
        if liq is not None and ((side == "long" and price <= float(liq)) or (side == "short" and price >= float(liq))):
            return self._close_perp(position, state, now, exit_price=float(liq), reason="liquidation", liquidated=True)
        if not state.market_open:
            return None
        move = (price / entry - 1) * (1 if side == "long" else -1)
        if move <= -self.trend_cfg.stop_loss_pct / 100:
            return self._close_perp(position, state, now, reason="stop_loss")
        if move >= self.trend_cfg.take_profit_pct / 100:
            return self._close_perp(position, state, now, reason="take_profit")
        if (now - float(position["opened_at"])) / 3600 >= self.trend_cfg.max_hold_hours:
            return self._close_perp(position, state, now, reason="duree_max")
        return None

    def _close_perp(self, position: dict[str, Any], state: PairState, now: float, *, reason: str,
                    exit_price: float | None = None, liquidated: bool = False) -> str:
        side = position["side"]
        notional = float(position["notional_usd"])
        price = exit_price if exit_price is not None else state.execution_price(side, opening=False)
        close_fee = 0.0 if liquidated else notional * state.close_fee_pct / 100
        gross = gross_pnl_usd(side, float(position["entry_price"]), price, notional)
        costs = float(position["open_fee_usd"]) + close_fee + float(position["funding_usd"]) + float(position["rollover_usd"])
        net = -float(position["collateral_usd"]) if liquidated else gross - costs
        self.store.close_position(
            int(position["id"]), closed_at=now, exit_price=price, close_fee_usd=close_fee, exit_reason=reason,
            gross_pnl_usd=gross, net_pnl_usd=net,
        )
        message = (
            f"{position['strategy']} {position['symbol']} {side} fermé ({reason}) : entrée {float(position['entry_price']):.4f}, "
            f"sortie {price:.4f}, brut {gross:+.4f} $, net {net:+.4f} $ "
            f"(funding {float(position['funding_usd']):+.4f}, rollover {float(position['rollover_usd']):.4f}, "
            f"frais {float(position['open_fee_usd']) + close_fee:.4f})"
        )
        self.store.add_event(message, level="warning" if net < 0 else "info", strategy=position["strategy"],
                             symbol=position["symbol"], created_at=now)
        logger.info(message)
        return message

    def _evaluate_carry_exit(self, position: dict[str, Any], state: PairState, now: float) -> str | None:
        liq = position.get("liquidation_price")
        if liq is not None and float(liq) > 0 and state.price >= float(liq):
            return self._close_carry(position, state, now, reason="liquidation", liquidated=True)
        if not state.market_open:
            return None
        key = f"carry_low_since:{position['symbol']}"
        net_apr = state.net_carry_apr_pct("short")
        exit_threshold = self.carry_cfg.exit_apr_pct(self.carry_hurdle_apr_pct())
        if net_apr < exit_threshold:
            since = self.store.get_state(key)
            if not since:
                self.store.set_state(key, str(now))
            elif now - float(since) >= self.carry_cfg.exit_confirm_hours * 3600:
                self.store.set_state(key, "")
                return self._close_carry(
                    position, state, now,
                    reason=(f"funding_faible ({net_apr:+.1f} %/an net < {exit_threshold:.1f} % depuis "
                            f"{(now - float(since)) / 3600:.0f} h)"),
                )
        else:
            self.store.set_state(key, "")
        if (now - float(position["opened_at"])) / 3600 >= self.carry_cfg.max_hold_hours:
            return self._close_carry(position, state, now, reason="duree_max")
        return None

    def _close_carry(self, position: dict[str, Any], state: PairState, now: float, *, reason: str,
                     liquidated: bool = False) -> str:
        notional = float(position["notional_usd"])
        collateral = float(position["collateral_usd"])
        if liquidated:
            # Le short est liquidé : tout le collatéral perp est perdu, aucun frais
            # de clôture ; la jambe spot est revendue au prix courant (sa plus-value
            # compense une partie, pas la totalité, de la perte).
            exit_price = float(position["liquidation_price"])
            close_fee = 0.0
            perp_gross = -collateral
        else:
            exit_price = state.execution_price("short", opening=False)
            close_fee = notional * state.close_fee_pct / 100
            perp_gross = gross_pnl_usd("short", float(position["entry_price"]), exit_price, notional)
        spot_qty = float(position["spot_qty"] or 0)
        spot_gross = spot_qty * (state.price - float(position["spot_entry_price"] or state.price))
        spot_exit_costs = spot_qty * state.price * self.carry_cfg.spot_cost_pct / 100 + self.carry_cfg.spot_gas_usd
        spot_costs = float(position["spot_costs_usd"]) + spot_exit_costs
        gross = perp_gross + spot_gross
        net = gross - float(position["open_fee_usd"]) - close_fee - float(position["funding_usd"]) \
            - float(position["rollover_usd"]) - spot_costs
        self.store.close_position(
            int(position["id"]), closed_at=now, exit_price=exit_price, close_fee_usd=close_fee, exit_reason=reason,
            gross_pnl_usd=gross, net_pnl_usd=net, spot_costs_usd=spot_costs,
        )
        message = (
            f"funding_carry {position['symbol']} fermé ({reason}) : funding encaissé {-float(position['funding_usd']):+.4f} $, "
            f"rollover {float(position['rollover_usd']):.4f} $, frais perp+spot {float(position['open_fee_usd']) + close_fee + spot_costs:.4f} $, "
            f"base {gross:+.4f} $ → net {net:+.4f} $"
        )
        self.store.add_event(message, level="warning" if net < 0 else "info", strategy=STRATEGY_CARRY,
                             symbol=position["symbol"], created_at=now)
        logger.info(message)
        return message

    # --------------------------------------------------------------- entries
    def carry_reference_apr_pct(self) -> float | None:
        """Taux sans risque de marché du capital oisif : dernier taux Aave USDC
        observé par la trésorerie (ignoré s'il date de plus de 48 h)."""
        observation = self.store.latest_treasury_observation()
        if not observation or observation.get("aave_usdc_apr_pct") is None:
            return None
        if time.time() - float(observation["observed_at"]) > 48 * 3600:
            return None
        return float(observation["aave_usdc_apr_pct"])

    def carry_hurdle_apr_pct(self) -> float:
        return self.carry_cfg.hurdle_apr_pct(self.carry_reference_apr_pct())

    def _carry_entries(self, states: dict[str, PairState], now: float) -> list[str]:
        actions: list[str] = []
        open_symbols = {p["symbol"] for p in self.store.open_positions(STRATEGY_CARRY)}
        reference = self.carry_reference_apr_pct()
        hurdle = self.carry_cfg.hurdle_apr_pct(reference)
        for symbol in self.carry_cfg.markets:
            state = states.get(symbol)
            if state is None or symbol in open_symbols or not state.market_open:
                continue
            net_apr = state.net_carry_apr_pct("short")
            if net_apr < hurdle:
                continue
            if not self._carry_funding_persistent(symbol, now, hurdle):
                continue
            notional = max(self.carry_cfg.notional_usd, state.min_notional_usd)
            leverage = self.carry_cfg.leverage
            if state.max_leverage > 0:
                leverage = min(leverage, state.max_leverage)
            collateral = notional / leverage
            costs = self.carry_cfg.round_trip_costs_usd(notional, state.open_fee_pct, state.close_fee_pct)
            expected_income = notional * net_apr / 100 * self.carry_cfg.max_hold_hours / 8760
            if expected_income < self.carry_cfg.entry_min_edge_ratio * costs:
                logger.debug(
                    "carry %s : gain attendu %.3f $ sur %.0f h < %.1f × coûts %.3f $ — pas d'entrée.",
                    symbol, expected_income, self.carry_cfg.max_hold_hours, self.carry_cfg.entry_min_edge_ratio, costs,
                )
                continue
            breakeven_hours = costs / (notional * net_apr / 100 / 8760) if net_apr > 0 else float("inf")
            entry_price = state.execution_price("short", opening=True)
            open_fee = notional * state.open_fee_pct / 100
            spot_costs = notional * self.carry_cfg.spot_cost_pct / 100 + self.carry_cfg.spot_gas_usd
            spot_qty = (notional - notional * self.carry_cfg.spot_cost_pct / 100) / state.price
            reference_text = f"Aave {reference:.2f} %" if reference is not None else "Aave inconnu"
            position_id = self.store.insert_position({
                "strategy": STRATEGY_CARRY, "symbol": symbol, "side": "short", "kind": "carry",
                "opened_at": now, "entry_price": entry_price, "index_price_at_open": state.price,
                "notional_usd": notional, "leverage": leverage, "collateral_usd": collateral,
                "open_fee_usd": open_fee, "spot_qty": spot_qty, "spot_entry_price": state.price,
                "spot_costs_usd": spot_costs, "funding_usd": 0.0, "rollover_usd": 0.0, "last_accrual_at": now,
                "liquidation_price": liquidation_price("short", entry_price, collateral, notional, open_fee),
                "entry_reason": (
                    f"funding net {net_apr:+.1f} %/an (brut {state.funding_income_apr_pct('short'):+.1f}, "
                    f"rollover {state.rollover_apr_pct():.1f}) ≥ seuil {hurdle:.1f} % ({reference_text}, {leverage:g}x) ; "
                    f"amortissement {breakeven_hours / 24:.1f} j"
                ),
            })
            if position_id is None:
                continue
            message = (
                f"funding_carry {symbol} ouvert : short perp {notional:.0f} $ à {leverage:g}x @ {entry_price:.4f} + spot long "
                f"{spot_qty:.6f} @ {state.price:.4f} (capital {self.carry_cfg.capital_usd(notional):.0f} $) — funding net "
                f"{net_apr:+.1f} %/an ≥ seuil {hurdle:.1f} % ({reference_text}), coûts de rotation {costs:.3f} $ amortis en "
                f"{breakeven_hours / 24:.1f} j, gain attendu {expected_income:.2f} $ sur {self.carry_cfg.max_hold_hours / 24:.0f} j"
            )
            self.store.add_event(message, strategy=STRATEGY_CARRY, symbol=symbol, created_at=now)
            logger.info(message)
            actions.append(message)
        return actions

    def _carry_funding_persistent(self, symbol: str, now: float, threshold_apr_pct: float) -> bool:
        """Le rendement net doit avoir tenu sur les `entry_persistence_hours`
        dernières observations (médiane ≥ seuil, fenêtre couverte à 80 %) :
        un pic de funding d'un quart d'heure ne justifie pas 0,29 % de frais."""
        window_hours = self.carry_cfg.entry_persistence_hours
        if window_hours <= 0:
            return True
        history = self.store.carry_apr_history(symbol, now - window_hours * 3600)
        if len(history) < 3 or history[0]["observed_at"] > now - 0.8 * window_hours * 3600:
            return False
        net_aprs = sorted(
            (-row["funding_short_pct_h"] - row["rollover_pct_h"]) * 8760 for row in history
        )
        median = net_aprs[len(net_aprs) // 2]
        return median >= threshold_apr_pct

    def _trend_entries(self, states: dict[str, PairState], now: float) -> list[str]:
        actions: list[str] = []
        open_positions = self.store.open_positions(STRATEGY_TREND)
        open_symbols = {p["symbol"] for p in open_positions}
        open_count = len(open_positions)
        day_pnl = self.store.realized_pnl_since(STRATEGY_TREND, _utc_day_start(now))
        if day_pnl <= -self.trend_cfg.daily_loss_limit_usd:
            day_key = f"trend_daily_limit_notified:{int(_utc_day_start(now))}"
            if self.store.get_state(day_key) is None:
                self.store.set_state(day_key, "1")
                message = f"trend : perte journalière {day_pnl:+.2f} $ ≤ -{self.trend_cfg.daily_loss_limit_usd:.2f} $ — entrées suspendues jusqu'à minuit UTC."
                self.store.add_event(message, level="warning", strategy=STRATEGY_TREND, created_at=now)
                logger.warning(message)
                actions.append(message)
            return actions
        reference_max_age = 3 * self._interval_seconds + 60
        for symbol in self.trend_cfg.markets:
            if open_count >= self.trend_cfg.max_open_positions:
                break
            state = states.get(symbol)
            if state is None or symbol in open_symbols or not state.market_open:
                continue
            last_closed = self.store.last_closed_at(STRATEGY_TREND, symbol)
            if last_closed is not None and now - last_closed < self.trend_cfg.reentry_cooldown_minutes * 60:
                continue
            side = self._trend_signal(symbol, state.price, now, reference_max_age)
            if side is None:
                continue
            leverage = min(self.trend_cfg.leverage, state.max_leverage) if state.max_leverage > 0 else self.trend_cfg.leverage
            notional = max(self.trend_cfg.notional_usd, state.min_notional_usd)
            collateral = notional / leverage
            entry_price = state.execution_price(side, opening=True)
            open_fee = notional * state.open_fee_pct / 100
            position_id = self.store.insert_position({
                "strategy": STRATEGY_TREND, "symbol": symbol, "side": side, "kind": "perp",
                "opened_at": now, "entry_price": entry_price, "index_price_at_open": state.price,
                "notional_usd": notional, "leverage": leverage, "collateral_usd": collateral,
                "open_fee_usd": open_fee, "spot_qty": None, "spot_entry_price": None, "spot_costs_usd": 0.0,
                "funding_usd": 0.0, "rollover_usd": 0.0, "last_accrual_at": now,
                "liquidation_price": liquidation_price(side, entry_price, collateral, notional, open_fee),
                "entry_reason": f"momentum {self.trend_cfg.lookback_minutes} min",
            })
            if position_id is None:
                continue
            open_count += 1
            message = (
                f"trend {symbol} {side} ouvert : {notional:.0f} $ à {leverage:.1f}x @ {entry_price:.4f} "
                f"(stop {self.trend_cfg.stop_loss_pct:.1f} %, objectif {self.trend_cfg.take_profit_pct:.1f} %, "
                f"frais {open_fee:.4f} $)"
            )
            self.store.add_event(message, strategy=STRATEGY_TREND, symbol=symbol, created_at=now)
            logger.info(message)
            actions.append(message)
        return actions

    def _trend_signal(self, symbol: str, price: float, now: float, reference_max_age: float) -> str | None:
        reference = self.store.price_at_or_before(symbol, now - self.trend_cfg.lookback_minutes * 60, reference_max_age)
        confirm = self.store.price_at_or_before(symbol, now - self.trend_cfg.confirm_minutes * 60, reference_max_age)
        if not reference or not confirm:
            return None
        lookback_return = price / reference - 1
        confirm_return = price / confirm - 1
        threshold = self.trend_cfg.entry_pct / 100
        if lookback_return >= threshold and confirm_return > 0:
            return "long"
        if lookback_return <= -threshold and confirm_return < 0:
            return "short"
        return None

    # -------------------------------------------------------------- reporting
    def position_snapshot(self, position: dict[str, Any], state: PairState | None, now: float) -> dict[str, Any]:
        """Position ouverte enrichie de sa valorisation au dernier prix connu."""
        snapshot = dict(position)
        snapshot["hours_open"] = (now - float(position["opened_at"])) / 3600
        if state is None:
            snapshot["unrealized_net_usd"] = None
            return snapshot
        notional = float(position["notional_usd"])
        side = position["side"]
        exit_price = state.execution_price(side, opening=False)
        close_fee = notional * state.close_fee_pct / 100
        gross = gross_pnl_usd(side, float(position["entry_price"]), exit_price, notional)
        costs = float(position["open_fee_usd"]) + close_fee + float(position["funding_usd"]) + float(position["rollover_usd"])
        if position["kind"] == "carry":
            spot_qty = float(position["spot_qty"] or 0)
            gross += spot_qty * (state.price - float(position["spot_entry_price"] or state.price))
            costs += float(position["spot_costs_usd"]) + spot_qty * state.price * self.carry_cfg.spot_cost_pct / 100 \
                + self.carry_cfg.spot_gas_usd
        snapshot.update(
            mark_price=state.price,
            unrealized_gross_usd=gross,
            unrealized_net_usd=gross - costs,
            funding_income_apr_pct=state.funding_income_apr_pct(side),
        )
        return snapshot
