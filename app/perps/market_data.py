"""Lecture du marché Veranta (perps sur Base) — aucune clé, aucun ordre.

Le SDK officiel `veranta-sdk` est importé paresseusement : il n'est installé
que dans le venv du service perps (`.venv-perps`, voir requirements-perps.txt)
et jamais dans celui du bot d'arbitrage (contraintes de dépendances
incompatibles avec web3). Les tests utilisent un fournisseur factice."""
from __future__ import annotations

import logging
import time
from typing import Protocol

from app.perps.models import PairState

logger = logging.getLogger("app.perps.market_data")


class MarketDataProvider(Protocol):
    async def pair_state(self, symbol: str) -> PairState: ...

    async def funding_snapshot(self) -> list[dict]: ...

    async def close(self) -> None: ...


def _as_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def funding_rows_from_snapshot(snapshot, observed_at: float | None = None) -> list[dict]:
    """Aplatit `markets.snapshot()` en lignes de funding/rollover/OI par paire
    (fonction pure, testable). Les paires sans `funding_rate` sont ignorées."""
    pairs = getattr(snapshot, "pairs", None) or getattr(snapshot, "pair_infos", None) or []
    items = pairs.values() if isinstance(pairs, dict) else pairs
    now = observed_at if observed_at is not None else time.time()
    rows: list[dict] = []
    for pair in items:
        funding = getattr(pair, "funding_rate", None)
        if funding is None or not getattr(pair, "symbol", None):
            continue
        rollover = getattr(pair, "margin_fee", None)
        oi = getattr(pair, "coin_oi", None) or getattr(pair, "open_interest", None)
        rows.append({
            "observed_at": now,
            "symbol": str(pair.symbol),
            "funding_long_pct_h": _as_float(getattr(funding, "long", 0)),
            "funding_short_pct_h": _as_float(getattr(funding, "short", 0)),
            "rollover_pct_h": max(_as_float(getattr(rollover, "long", 0)), _as_float(getattr(rollover, "short", 0))),
            "oi_long_usd": _as_float(getattr(oi, "long", 0)),
            "oi_short_usd": _as_float(getattr(oi, "short", 0)),
            "market_open": bool(getattr(pair, "is_market_open", True)),
        })
    return rows


def pair_state_from_sdk(symbol: str, pair, price, spread_pct: float | None, observed_at: float | None = None) -> PairState:
    """Convertit les objets du SDK (`markets.pair`, `markets.price`,
    `markets.spread()["spreadPct"]`) en `PairState` ; fonction pure, testable."""
    leverages = getattr(pair, "leverages", None)
    funding = getattr(pair, "funding_rate", None)
    rollover = getattr(pair, "margin_fee", None)
    oi = getattr(pair, "coin_oi", None) or getattr(pair, "open_interest", None)
    # Le rollover est symétrique chez Veranta ; on retient le pire des deux côtés par prudence.
    rollover_pct_h = max(_as_float(getattr(rollover, "long", 0)), _as_float(getattr(rollover, "short", 0)))
    return PairState(
        symbol=symbol,
        price=_as_float(price),
        spread_pct=_as_float(spread_pct, _as_float(getattr(pair, "spread_p", 0))),
        open_fee_pct=_as_float(getattr(pair, "open_fee_p", 0)),
        close_fee_pct=_as_float(getattr(pair, "close_fee_p", 0)),
        funding_long_pct_h=_as_float(getattr(funding, "long", 0)),
        funding_short_pct_h=_as_float(getattr(funding, "short", 0)),
        rollover_pct_h=rollover_pct_h,
        oi_long_usd=_as_float(getattr(oi, "long", 0)),
        oi_short_usd=_as_float(getattr(oi, "short", 0)),
        min_notional_usd=_as_float(getattr(pair, "min_lev_pos_usdc", 0)),
        max_leverage=_as_float(getattr(leverages, "max_leverage", 0)),
        market_open=bool(getattr(pair, "is_market_open", True)),
        observed_at=observed_at if observed_at is not None else time.time(),
    )


class VerantaMarketData:
    """Fournisseur réel : `AsyncVeranta()` sans identifiants (les lectures de
    marché sont publiques). Le spread dynamique est demandé pour une position
    de référence (notionnel/levier de la config) afin de refléter le coût
    d'exécution réellement coté par le moteur de risque v2."""

    def __init__(self, *, reference_notional_usd: float = 100.0, reference_leverage: float = 2.0):
        self._reference_notional_usd = reference_notional_usd
        self._reference_leverage = reference_leverage
        self._client = None

    async def _get_client(self):
        if self._client is None:
            from veranta_sdk import AsyncVeranta  # import paresseux : venv perps uniquement

            self._client = AsyncVeranta()
            await self._client.__aenter__()
        return self._client

    async def pair_state(self, symbol: str) -> PairState:
        client = await self._get_client()
        pair = await client.markets.pair(symbol)
        price = await client.markets.price(symbol)
        spread_pct = None
        try:
            quote = await client.markets.spread(
                symbol,
                collateral=self._reference_notional_usd / self._reference_leverage,
                leverage=self._reference_leverage,
                is_long=False,
            )
            spread_pct = _as_float((quote or {}).get("spreadPct"), None)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001 - le spread statique de la paire reste un repli acceptable
            logger.info("Spread dynamique indisponible pour %s (%s) — spread statique de la paire utilisé.", symbol, exc)
        return pair_state_from_sdk(symbol, pair, price, spread_pct)

    async def funding_snapshot(self) -> list[dict]:
        client = await self._get_client()
        return funding_rows_from_snapshot(await client.markets.snapshot())

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.__aexit__(None, None, None)
            finally:
                self._client = None
