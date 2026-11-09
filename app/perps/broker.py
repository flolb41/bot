"""Prérequis d'exécution live des perps Veranta — AUCUN ordre n'est câblé.

Ce module prépare le terrain sans l'emprunter : une description d'ordre
validée AVANT tout envoi (`validate_order`), une porte à trois verrous
(`live_gate` : mode, phrase de confirmation, clé déléguée distincte du wallet
+ plafonds), et deux brokers derrière le même protocole — `PaperBroker`
(ne fait rien) et `VerantaBroker` (SDK, avec TP/SL posés côté venue). Le
moteur papier n'appelle aucun des deux : brancher les ordres est une décision
séparée, à prendre une fois la porte verte et le papier convaincant.

Modèle de clés recommandé : un wallet « trader » dédié qui ne détient que le
collatéral qu'on accepte de perdre, et une clé **déléguée** enregistrée sur
Veranta (peut signer des ordres, ne peut pas retirer les fonds) — seule cette
clé déléguée vit sur le Pi (`VERANTA_PRIVATE_KEY`), l'adresse du trader étant
donnée par `VERANTA_TRADER_ADDRESS`.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from app.perps.engine import liquidation_price
from app.perps.models import LiveConfig, PairState, PerpsConfig

logger = logging.getLogger("app.perps.broker")

LIVE_CONFIRMATION_ENV = "PERPS_LIVE_CONFIRMED"
LIVE_CONFIRMATION_PHRASE = "JE COMPRENDS QUE LES PERPS LIVE PEUVENT PERDRE TOUT LE COLLATERAL"
MAX_SLIPPAGE_PCT = 5.0


@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    side: str                       # "long" | "short"
    notional_usd: float
    leverage: float
    take_profit_price: float | None = None
    stop_loss_price: float | None = None
    slippage_pct: float = 1.0

    @property
    def collateral_usd(self) -> float:
        return self.notional_usd / self.leverage if self.leverage > 0 else float("inf")


@dataclass(frozen=True)
class VenuePosition:
    symbol: str
    side: str
    pair_index: int
    trade_index: int
    collateral_usd: float
    leverage: float
    entry_price: float
    take_profit: float | None
    stop_loss: float | None
    liquidation_price: float | None
    rollover_fee_usd: float
    funding_fee_usd: float
    opened_at: int

    @property
    def notional_usd(self) -> float:
        return self.collateral_usd * self.leverage


@dataclass(frozen=True)
class GateCheck:
    name: str
    ok: bool
    detail: str


# ------------------------------------------------------------- validation
def validate_order(order: OrderRequest, state: PairState, live: LiveConfig, *, open_collateral_usd: float = 0.0,
                   open_fee_usd: float | None = None) -> list[str]:
    """Raisons de refus (liste vide = ordre acceptable). Tout ce qui est vérifié
    ici le serait aussi par le venue, mais après avoir payé le gas et le
    spread ; mieux vaut refuser avant."""
    reasons: list[str] = []
    if order.side not in ("long", "short"):
        reasons.append(f"sens inconnu {order.side!r}")
    if not state.market_open:
        reasons.append(f"marché {state.symbol} fermé")
    if order.notional_usd < state.min_notional_usd:
        reasons.append(f"notionnel {order.notional_usd:.2f} $ < minimum venue {state.min_notional_usd:.0f} $")
    if order.leverage < 1:
        reasons.append(f"levier {order.leverage} < 1")
    if state.max_leverage > 0 and order.leverage > state.max_leverage:
        reasons.append(f"levier {order.leverage} > maximum venue {state.max_leverage}")
    if order.leverage > live.max_leverage:
        reasons.append(f"levier {order.leverage} > plafond configuré {live.max_leverage}")
    collateral = order.collateral_usd
    if live.max_collateral_usd <= 0:
        reasons.append("perps.live.max_collateral_usd non défini (0) : porte fermée")
    elif collateral > live.max_collateral_usd:
        reasons.append(f"collatéral {collateral:.2f} $ > plafond par position {live.max_collateral_usd:.2f} $")
    if live.max_total_collateral_usd <= 0:
        reasons.append("perps.live.max_total_collateral_usd non défini (0) : porte fermée")
    elif open_collateral_usd + collateral > live.max_total_collateral_usd:
        reasons.append(
            f"collatéral total {open_collateral_usd + collateral:.2f} $ > plafond global {live.max_total_collateral_usd:.2f} $"
        )
    if not 0 < order.slippage_pct <= MAX_SLIPPAGE_PCT:
        reasons.append(f"glissement {order.slippage_pct} % hors de ]0, {MAX_SLIPPAGE_PCT}]")
    if order.side in ("long", "short") and state.price > 0:
        sign = 1 if order.side == "long" else -1
        fee = open_fee_usd if open_fee_usd is not None else order.notional_usd * state.open_fee_pct / 100
        liq = liquidation_price(order.side, state.price, collateral, order.notional_usd, fee) if order.leverage > 0 else None
        if order.stop_loss_price is not None:
            if (order.stop_loss_price - state.price) * sign >= 0:
                reasons.append("stop du mauvais côté du prix")
            elif liq is not None and (order.stop_loss_price - liq) * sign <= 0:
                reasons.append(f"stop {order.stop_loss_price:.4f} au-delà du prix de liquidation {liq:.4f}")
        if order.take_profit_price is not None and (order.take_profit_price - state.price) * sign <= 0:
            reasons.append("objectif du mauvais côté du prix")
    return reasons


# -------------------------------------------------------------------- porte
def signer_address(private_key: str) -> str | None:
    try:
        from eth_account import Account  # dépendance du SDK Veranta (venv perps)

        return Account.from_key(private_key).address
    except Exception:  # noqa: BLE001 - clé invalide ou eth_account absent
        return None


def live_gate(config: PerpsConfig, env: Mapping[str, str] | None = None) -> list[GateCheck]:
    """Trois verrous indépendants ; tous doivent être verts pour qu'un ordre
    puisse un jour partir. Aucun n'est contournable par la config seule."""
    env = os.environ if env is None else env
    live = config.live
    checks = [GateCheck("mode", config.mode == "live", f"perps.mode = {config.mode!r}")]

    phrase = env.get(LIVE_CONFIRMATION_ENV, "")
    checks.append(GateCheck(
        "confirmation", phrase.strip() == LIVE_CONFIRMATION_PHRASE,
        f"{LIVE_CONFIRMATION_ENV} {'correcte' if phrase.strip() == LIVE_CONFIRMATION_PHRASE else 'absente ou différente de la phrase attendue'}",
    ))

    key = env.get("VERANTA_PRIVATE_KEY", "")
    trader = env.get("VERANTA_TRADER_ADDRESS", "")
    signer = signer_address(key) if key else None
    if not key:
        detail, ok = "VERANTA_PRIVATE_KEY absente", False
    elif signer is None:
        detail, ok = "VERANTA_PRIVATE_KEY invalide (ou eth_account indisponible)", False
    elif not trader:
        detail, ok = "VERANTA_TRADER_ADDRESS absente : la clé du Pi serait le wallet lui-même (peut retirer les fonds)", False
    elif trader.lower() == signer.lower():
        detail, ok = f"la clé du Pi EST le trader {trader} : utiliser une clé déléguée distincte", False
    else:
        detail, ok = f"clé déléguée {signer[:10]}… pour le trader {trader[:10]}…", True
    checks.append(GateCheck("cle_deleguee", ok, detail))

    caps_ok = 0 < live.max_collateral_usd <= live.max_total_collateral_usd
    checks.append(GateCheck(
        "plafonds", caps_ok,
        f"max_collateral_usd={live.max_collateral_usd:g}, max_total_collateral_usd={live.max_total_collateral_usd:g}, "
        f"max_leverage={live.max_leverage:g}",
    ))
    trend_collateral = config.trend.notional_usd / config.trend.leverage if config.trend.leverage > 0 else float("inf")
    checks.append(GateCheck(
        "coherence_strategies", trend_collateral <= live.max_collateral_usd and config.trend.leverage <= live.max_leverage,
        f"trend : collatéral {trend_collateral:.2f} $ à {config.trend.leverage:g}x vs plafonds",
    ))
    return checks


def gate_open(checks: list[GateCheck]) -> bool:
    return all(check.ok for check in checks)


# ------------------------------------------------------------------ brokers
class Broker(Protocol):
    async def open(self, order: OrderRequest) -> dict[str, Any]: ...

    async def close(self, position: VenuePosition, collateral_to_close: float | None = None) -> dict[str, Any]: ...

    async def positions(self) -> list[VenuePosition]: ...

    async def usdc_balance(self) -> float | None: ...

    async def aclose(self) -> None: ...


class PaperBroker:
    """Ne touche à rien : accuse réception et ne tient aucune position venue."""

    async def open(self, order: OrderRequest) -> dict[str, Any]:
        return {"route": "paper", "accepted": True, "order": order.__dict__}

    async def close(self, position: VenuePosition, collateral_to_close: float | None = None) -> dict[str, Any]:
        return {"route": "paper", "accepted": True, "trade_index": position.trade_index}

    async def positions(self) -> list[VenuePosition]:
        return []

    async def usdc_balance(self) -> float | None:
        return None

    async def aclose(self) -> None:
        return None


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def venue_position_from_sdk(pos: Any, symbol_by_pair_index: Mapping[int, str] | None = None) -> VenuePosition:
    """Aplatissement d'une `Position` du SDK (propriétés dérivées des champs
    `*_raw` ; repli sur les bruts / 1e6 ou 1e10 si une propriété manque)."""
    def pick(name: str, raw_scale: float) -> float:
        value = getattr(pos, name, None)
        if value is None:
            value = _num(getattr(pos, f"{name}_raw", 0)) / raw_scale
        return _num(value)

    pair_index = int(getattr(pos, "pair_index", 0))
    base = getattr(pos, "base_symbol", None)
    symbol = (symbol_by_pair_index or {}).get(pair_index) or (f"{base}/USD" if base else f"pair#{pair_index}")
    tp, sl = pick("tp", 1e10), pick("sl", 1e10)
    return VenuePosition(
        symbol=symbol, side="long" if bool(getattr(pos, "buy", True)) else "short", pair_index=pair_index,
        trade_index=int(getattr(pos, "index", 0)), collateral_usd=pick("collateral", 1e6), leverage=pick("leverage", 1e10),
        entry_price=pick("open_price", 1e10), take_profit=tp or None, stop_loss=sl or None,
        liquidation_price=pick("liquidation_price", 1e10) or None, rollover_fee_usd=pick("rollover_fee", 1e6),
        funding_fee_usd=pick("unrealised_funding_fee", 1e6), opened_at=int(getattr(pos, "opened_at", 0) or 0),
    )


class VerantaBroker:
    """Exécution réelle via le SDK (clé déléguée lue dans l'environnement par
    le SDK lui-même). TP/SL sont transmis au venue à l'ouverture : ils tiennent
    même si le Pi s'éteint, contrairement aux stops évalués au tick du papier."""

    def __init__(self, live: LiveConfig):
        self._live = live
        self._client = None

    async def _get_client(self):
        if self._client is None:
            from veranta_sdk import AsyncVeranta

            self._client = AsyncVeranta()
            await self._client.__aenter__()
        return self._client

    async def verify(self) -> dict[str, Any]:
        """Identité et délégation telles que vues par le venue (lecture seule)."""
        client = await self._get_client()
        signer = getattr(client.signer, "address", None)
        trader = client.account.trader
        status: dict[str, Any] | None = None
        error: str | None = None
        try:
            await client.account.verify_delegation()
            if signer and signer.lower() != trader.lower():
                status = await client.account.delegation_status(signer)
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
        return {"signer": signer, "trader": trader, "delegate_mode": bool(signer and signer.lower() != trader.lower()),
                "delegation": status, "ok": error is None and bool(signer), "error": error}

    async def usdc_balance(self) -> float | None:
        client = await self._get_client()
        try:
            return float(await client.account.usdc_balance())
        except Exception as exc:  # noqa: BLE001
            logger.info("Solde USDC venue indisponible : %s", exc)
            return None

    async def positions(self) -> list[VenuePosition]:
        client = await self._get_client()
        data = await client.account.positions()
        return [venue_position_from_sdk(pos) for pos in (getattr(data, "positions", None) or [])]

    async def open(self, order: OrderRequest) -> dict[str, Any]:
        client = await self._get_client()
        receipt = await client.trade.market_open(
            order.symbol, order.side, order.collateral_usd, order.leverage, take_profit=order.take_profit_price,
            stop_loss=order.stop_loss_price, slippage_percent=min(order.slippage_pct, self._live.slippage_pct),
        )
        return _receipt(receipt)

    async def close(self, position: VenuePosition, collateral_to_close: float | None = None) -> dict[str, Any]:
        client = await self._get_client()
        receipt = await client.trade.market_close(
            position.symbol, position.trade_index, collateral_to_close or position.collateral_usd,
            open_timestamp=position.opened_at or None,
        )
        return _receipt(receipt)

    async def aclose(self) -> None:
        if self._client is not None:
            try:
                await self._client.__aexit__(None, None, None)
            finally:
                self._client = None


def _receipt(receipt: Any) -> dict[str, Any]:
    return {key: getattr(receipt, key, None) for key in ("route", "tx_hash", "request_id", "tracking_id", "order_id", "description")}
