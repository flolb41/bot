#!/usr/bin/env python3
"""Point d'entrée du service perps PAPIER (process et venv séparés du bot).

Usage:
    python perps_main.py run       # boucle de simulation + dashboard (port 8001)
    python perps_main.py status    # résumé texte des stratégies papier
    python perps_main.py backtest  # backtest tendance sur bougies (voir --help)
    python perps_main.py live-status  # verrous du mode live (lecture seule, aucun ordre)

Aucune clé, aucun ordre : voir app/perps/__init__.py.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
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
        logger.error(
            "Seul le mode « paper » est câblé dans le moteur (perps.mode=%s) — arrêt par prudence. "
            "Les prérequis live se vérifient avec `perps_main.py live-status` ; aucun ordre n'est envoyé.", config.mode,
        )
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
    if report["funding_leaderboard"]:
        reference = report.get("carry_reference_apr_pct")
        print(
            f"  Seuil carry : {report['carry_hurdle_apr_pct']:.1f} %/an net à {report['carry_leverage']:g}x "
            f"(capital {report['carry_capital_usd']:.0f} $ / 100 $ de notionnel, "
            f"{'Aave ' + format(reference, '.2f') + ' %' if reference is not None else 'Aave inconnu'})"
        )
        print(f"  Funding net — top 8 sur {report['funding_scan_pairs']} paires balayées :")
        for entry in report["funding_leaderboard"][:8]:
            hedge = "spot Base" if entry["spot_hedgeable"] else "directionnel seul"
            if entry.get("beats_hurdle"):
                hedge += " — CARRY > AAVE"
            average = f"moy. {entry['avg_net_apr_pct']:+6.1f} %" if entry.get("avg_net_apr_pct") is not None else "moy.    n/a  "
            print(
                f"    {entry['symbol']:<11} {entry['best_side']:<5} {entry['best_net_apr_pct']:+7.1f} %/an net ({average}, "
                f"rollover {entry['rollover_apr_pct']:4.1f} %) OI {entry['oi_long_usd']:>12,.0f}/{entry['oi_short_usd']:<12,.0f} {hedge}"
            )
    treasury = report.get("treasury")
    if treasury:
        fmt = lambda v, suffix="": "n/a" if v is None else f"{float(v):.2f}{suffix}"  # noqa: E731
        print(
            f"  Trésorerie : USDC liquide {fmt(treasury['idle_usdc'])} | déposé Aave {fmt(treasury.get('deposited_usdc'))} | "
            f"Aave {fmt(treasury['aave_usdc_apr_pct'], ' %')} | "
            f"vault Veranta 7j/30j/12m {fmt(treasury['vault_7d_apr_pct'], ' %')}/{fmt(treasury['vault_30d_apr_pct'], ' %')}/"
            f"{fmt(treasury['vault_12m_apr_pct'], ' %')} | manque à gagner cumulé {float(treasury['missed_yield_usd']):.4f} $"
        )


def cmd_backtest(args: argparse.Namespace) -> None:
    """Backtest de la stratégie tendance sur bougies Veranta (cache data/perps_candles)."""
    import json
    import time
    from dataclasses import asdict
    from pathlib import Path

    from app.perps import backtest as bt

    config = _load(args.config)
    logging.basicConfig(level=logging.WARNING if args.json else logging.INFO, format="%(levelname)s | %(message)s")
    base = bt.TrendParams.from_trend_config(config.trend)
    fetcher = None if args.no_fetch else bt.sdk_fetcher
    now = time.time()
    reports: list[dict] = []
    for symbol in args.symbol or list(config.trend.markets):
        candles = asyncio.run(bt.load_candles(symbol, args.resolution, args.days, now=now, cache_dir=Path(args.cache_dir), fetcher=fetcher))
        if len(candles) < 50:
            print(f"{symbol} : {len(candles)} bougie(s) seulement — ignoré (cache vide ? relancer sans --no-fetch).", file=sys.stderr)
            continue
        costs = bt.CostModel()
        if args.live_costs:
            from app.perps.market_data import VerantaMarketData

            async def _live_costs() -> bt.CostModel:
                provider = VerantaMarketData(reference_notional_usd=config.trend.notional_usd, reference_leverage=config.trend.leverage)
                try:
                    return bt.CostModel.from_pair_state(await provider.pair_state(symbol))
                finally:
                    await provider.close()

            costs = asyncio.run(_live_costs())
        if args.grid:
            report = bt.walk_forward(candles, base, costs, symbol=symbol, resolution=args.resolution, intrabar=not args.close_only,
                                     train_ratio=args.train_ratio, min_trades=args.min_trades)
            report["costs"] = asdict(costs)
            reports.append(report)
            if not args.json:
                print(bt.verdict(report))
        else:
            result = bt.simulate_trend(candles, base, costs, symbol=symbol, resolution=args.resolution, intrabar=not args.close_only)
            metrics = result.metrics()
            reports.append({"symbol": symbol, "resolution": args.resolution, "params": asdict(base), "costs": asdict(costs),
                            "metrics": metrics, "period": [result.start, result.end]})
            if not args.json:
                pf = metrics["profit_factor"]
                print(
                    f"{symbol} {args.resolution} — {metrics['bars']} bougies ({metrics['months']:.1f} mois) : {metrics['trades']} trades, "
                    f"gain {metrics['win_rate_pct'] or 0:.0f} %, PF {'n/a' if pf is None else f'{pf:.2f}'}, net {metrics['net_pnl_usd']:+.2f} $ "
                    f"({metrics['net_per_month_usd']:+.2f} $/mois), DD max {metrics['max_drawdown_usd']:.2f} $, "
                    f"frais {metrics['fees_usd']:.2f} $, funding {metrics['funding_usd']:+.2f} $, sorties {metrics['exits']}"
                )
    if args.json:
        print(json.dumps(reports, ensure_ascii=False, indent=2, default=str))


def cmd_live_status(args: argparse.Namespace) -> None:
    """État de la porte live (lecture seule) : verrous, identité, délégation, solde, positions venue."""
    from app.perps import broker as br

    config = _load(args.config)
    checks = br.live_gate(config)
    print(f"Porte live perps : {'OUVERTE' if br.gate_open(checks) else 'FERMÉE'} (mode {config.mode})")
    for check in checks:
        print(f"  [{'ok' if check.ok else 'KO'}] {check.name:<22} {check.detail}")
    print(f"  Phrase attendue dans {br.LIVE_CONFIRMATION_ENV} : {br.LIVE_CONFIRMATION_PHRASE}")
    if not os.environ.get("VERANTA_PRIVATE_KEY"):
        print("  Pas de clé : vérification venue sautée. Aucun ordre n'est câblé dans le moteur de toute façon.")
        return
    venue = br.VerantaBroker(config.live)

    async def _probe() -> None:
        try:
            identity = await venue.verify()
            print(f"  Venue : signataire {identity['signer']} | trader {identity['trader']} | "
                  f"délégué {'oui' if identity['delegate_mode'] else 'NON'} | délégation {identity['delegation']} | "
                  f"{'ok' if identity['ok'] else 'ERREUR ' + str(identity['error'])}")
            balance = await venue.usdc_balance()
            print(f"  Solde USDC venue : {'n/a' if balance is None else f'{balance:.2f}'}")
            positions = await venue.positions()
            print(f"  Positions ouvertes sur le venue : {len(positions)}")
            for position in positions:
                print(f"    {position.symbol} {position.side} {position.notional_usd:.2f} $ ({position.leverage:.1f}x) @ {position.entry_price:.4f} "
                      f"TP {position.take_profit} SL {position.stop_loss} liq {position.liquidation_price} "
                      f"funding {position.funding_fee_usd:+.4f} rollover {position.rollover_fee_usd:.4f}")
        finally:
            await venue.aclose()

    asyncio.run(_probe())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Service perps papier (Base / Veranta)")
    parser.add_argument("--config", default="config/config.yaml", help="Chemin du fichier de config YAML")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run").set_defaults(func=cmd_run)
    sub.add_parser("status").set_defaults(func=cmd_status)
    backtest = sub.add_parser("backtest", help="Backtest tendance sur bougies Veranta")
    backtest.add_argument("--symbol", action="append", help="Marché (répétable) ; défaut : perps.trend.markets")
    backtest.add_argument("--resolution", default="60", choices=("60", "240", "D"), help="Bougies 1 h, 4 h ou journalières")
    backtest.add_argument("--days", type=float, default=365, help="Profondeur d'historique (1 an en 1 h, 2 ans en 4 h, 4 ans en D)")
    backtest.add_argument("--grid", action="store_true", help="Grille de paramètres + walk-forward 70/30")
    backtest.add_argument("--train-ratio", type=float, default=0.7)
    backtest.add_argument("--min-trades", type=int, default=10, help="Trades minimum à l'entraînement pour retenir une combinaison")
    backtest.add_argument("--close-only", action="store_true", help="Sorties évaluées à la clôture seulement (comportement exact du moteur)")
    backtest.add_argument("--live-costs", action="store_true", help="Lire spread/frais/funding actuels du marché (SDK) au lieu des défauts")
    backtest.add_argument("--no-fetch", action="store_true", help="Cache local uniquement, aucun appel réseau")
    backtest.add_argument("--cache-dir", default="data/perps_candles")
    backtest.add_argument("--json", action="store_true", help="Sortie JSON complète")
    backtest.set_defaults(func=cmd_backtest)
    sub.add_parser("live-status", help="Verrous du mode live, identité/délégation et positions venue (lecture seule)").set_defaults(func=cmd_live_status)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
