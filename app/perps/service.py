"""Boucle du service perps papier : un tick toutes les N minutes + dashboard."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from app.perps.engine import PaperEngine
from app.perps.market_data import MarketDataProvider, VerantaMarketData
from app.perps.models import PairState, PerpsConfig
from app.perps.storage import PerpsStore

logger = logging.getLogger("app.perps.service")


async def fetch_states(provider: MarketDataProvider, symbols: tuple[str, ...]) -> dict[str, PairState]:
    """Lit chaque marché indépendamment : une paire en erreur n'empêche pas
    les autres d'être traitées (elle est simplement absente du tick)."""
    states: dict[str, PairState] = {}
    for symbol in symbols:
        try:
            states[symbol] = await provider.pair_state(symbol)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Lecture du marché %s impossible : %s: %s", symbol, type(exc).__name__, str(exc)[:160])
    return states


def _summarize(states: dict[str, PairState]) -> str:
    return " | ".join(
        f"{s.symbol} {s.price:.4g} short {s.funding_income_apr_pct('short'):+.1f} %/an"
        for s in states.values()
    )


async def run_tick(engine: PaperEngine, provider: MarketDataProvider, config: PerpsConfig,
                   now: float | None = None) -> dict[str, Any]:
    states = await fetch_states(provider, config.markets)
    if not states:
        logger.warning("Aucun marché lisible : tick ignoré.")
        return {"actions": [], "markets": 0, "open_positions": None}
    summary = engine.process_tick(states, now if now is not None else time.time())
    summary["markets"] = len(states)
    logger.info(
        "Tick perps : %d marché(s) [%s] — %d position(s) ouverte(s), %d action(s).",
        len(states), _summarize(states), summary.get("open_positions") or 0, len(summary["actions"]),
    )
    return summary


async def run_periodic_jobs(store: PerpsStore, config: PerpsConfig, provider: MarketDataProvider,
                            treasury_provider, now: float | None = None) -> dict[str, Any]:
    """Tâches lentes exécutées à leur propre cadence depuis la boucle principale :
    balayage du funding de toutes les paires et observation de trésorerie."""
    now = now if now is not None else time.time()
    done: dict[str, Any] = {}
    if config.funding_scan.enabled:
        last = store.get_state("last_funding_scan_at")
        if not last or now - float(last) >= config.funding_scan.interval_minutes * 60:
            from app.perps.funding_scan import run_funding_scan

            try:
                rows = await provider.funding_snapshot()
                done["funding_alerts"] = run_funding_scan(store, config.funding_scan, rows, now)
                done["funding_pairs"] = len(rows)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Balayage du funding impossible : %s: %s", type(exc).__name__, str(exc)[:160])
    if config.treasury.enabled and treasury_provider is not None:
        last = store.get_state("last_treasury_at")
        if not last or now - float(last) >= config.treasury.interval_minutes * 60:
            from app.perps.treasury import run_treasury_observation

            try:
                done["treasury"] = await run_treasury_observation(store, config.treasury, treasury_provider, now)
                store.set_state("last_treasury_at", str(now))
            except Exception as exc:  # noqa: BLE001
                logger.warning("Observation de trésorerie impossible : %s: %s", type(exc).__name__, str(exc)[:160])
    return done


async def run_forever(config: PerpsConfig, provider: MarketDataProvider | None = None,
                      stop_event: asyncio.Event | None = None, treasury_provider=None) -> None:
    store = PerpsStore(config.database_path)
    engine = PaperEngine(store, config)
    if provider is None:
        provider = VerantaMarketData(
            reference_notional_usd=config.trend.notional_usd, reference_leverage=config.trend.leverage,
        )
        if treasury_provider is None and config.treasury.enabled:
            from app.perps.treasury import VerantaTreasuryData

            treasury_provider = VerantaTreasuryData(config.treasury, market_data=provider)
    stop_event = stop_event or asyncio.Event()
    tasks: list[asyncio.Task] = []

    if config.dashboard_enabled:
        import uvicorn

        from app.perps.dashboard import create_perps_app

        server = uvicorn.Server(uvicorn.Config(
            create_perps_app(store, engine, config), host=config.dashboard_host, port=config.dashboard_port,
            log_level="warning",
        ))
        tasks.append(asyncio.create_task(server.serve(), name="perps-dashboard"))
        logger.info("Dashboard perps démarré sur le port %s.", config.dashboard_port)

    async def _loop() -> None:
        interval = config.interval_minutes * 60
        while not stop_event.is_set():
            started = time.time()
            try:
                await run_tick(engine, provider, config)
            except Exception:  # noqa: BLE001 - le service ne doit jamais mourir sur un tick
                logger.exception("Tick perps en échec.")
            try:
                await run_periodic_jobs(store, config, provider, treasury_provider)
            except Exception:  # noqa: BLE001
                logger.exception("Tâches périodiques perps en échec.")
            delay = max(5.0, interval - (time.time() - started))
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    tasks.append(asyncio.create_task(_loop(), name="perps-loop"))
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await provider.close()
