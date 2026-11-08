#!/usr/bin/env python3
"""Point d'entrée du service perps PAPIER (process et venv séparés du bot).

Usage:
    python perps_main.py run      # boucle de simulation + dashboard (port 8001)
    python perps_main.py status   # résumé texte des stratégies papier

Aucune clé, aucun ordre : voir app/perps/__init__.py.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from app.config import load_config
from app.logger import setup_logger
from app.perps.models import PerpsConfig
from app.runtime_lock import acquire_single_instance_lock


def _load(config_path: str) -> PerpsConfig:
    return PerpsConfig.from_config(load_config(config_path).raw)


def cmd_run(args: argparse.Namespace) -> None:
    config = _load(args.config)
    setup_logger(level=config.log_level, log_file=config.log_file)
    logger = logging.getLogger("app.perps.main")
    if not config.enabled:
        logger.warning("perps.enabled est false dans la configuration : rien à faire, arrêt.")
        return
    if config.mode != "paper":
        logger.error("Seul le mode « paper » est implémenté (perps.mode=%s) — arrêt par prudence.", config.mode)
        raise SystemExit(1)
    if not acquire_single_instance_lock(config.database_path):
        raise SystemExit(1)
    logger.info(
        "Service perps papier démarré : venue %s, %d marché(s) [%s], tick toutes les %g min, base %s.",
        config.venue, len(config.markets), ", ".join(config.markets), config.interval_minutes, config.database_path,
    )
    from app.perps.service import run_forever

    try:
        asyncio.run(run_forever(config))
    except KeyboardInterrupt:
        pass


def cmd_status(args: argparse.Namespace) -> None:
    from app.perps.dashboard import build_report
    from app.perps.engine import PaperEngine
    from app.perps.storage import PerpsStore

    config = _load(args.config)
    store = PerpsStore(config.database_path)
    report = build_report(store, PaperEngine(store, config), config)
    print(f"Perps papier ({config.venue}) — dernier tick : {report['last_tick_at']}")
    for market in report["markets"]:
        print(
            f"  {market['symbol']:<9} prix {market['price']:>12.4f}  funding short {market['funding_income_apr_short_pct']:+6.1f} %/an"
            f"  spread {market['spread_pct']:.3f} %  {'ouvert' if market['market_open'] else 'fermé'}"
        )
    for name, stats in report["strategies"].items():
        print(
            f"  [{name}] ouvertes={stats.get('open_positions', 0)} clôturées={stats.get('closed_trades', 0)} "
            f"net={float(stats.get('realized_pnl_usd') or 0):+.4f} $ funding net={-float(stats.get('funding_paid_usd') or 0):+.4f} $ "
            f"frais={float(stats.get('fees_usd') or 0):.4f} $ liquidations={stats.get('liquidations', 0)}"
        )
    for position in report["open_positions"]:
        print(
            f"    {position['strategy']} {position['symbol']} {position['side']} {position['notional_usd']:.0f} $ "
            f"@ {position['entry_price']:.4f} → non réalisé {position.get('unrealized_net_usd')}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Service perps papier (Base / Veranta)")
    parser.add_argument("--config", default="config/config.yaml", help="Chemin du fichier de config YAML")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run").set_defaults(func=cmd_run)
    sub.add_parser("status").set_defaults(func=cmd_status)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
