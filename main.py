#!/usr/bin/env python3
"""Point d'entrée CLI du Crypto Reward Hunter.

Usage:
    python main.py seed                 # initialise/MAJ les projets suivis en base
    python main.py scan                 # exécute un cycle de scan unique
    python main.py run                  # démarre scheduler + Telegram + dashboard (service RPi3)
    python main.py telegram             # démarre uniquement le polling Telegram
    python main.py dashboard            # démarre uniquement le serveur dashboard (uvicorn)
    python main.py status               # affiche un résumé en texte dans le terminal
    python main.py create-autosigner-wallet  # génère le wallet dédié à l'auto-signature (RPi3 uniquement)
    python main.py scan-opportunities   # scanne manuellement les micro-récompenses auto-réclamables
    python main.py discover-projects    # cherche manuellement de nouveaux projets (sources publiques)
    python main.py scan-arbitrage       # scanne manuellement les écarts de prix inter-DEX (SIMULATION)
"""
from __future__ import annotations

import argparse
import logging
import sys
import threading

# Force stdout/stderr en UTF-8 : le dashboard texte utilise des caractères Unicode
# (box-drawing, émojis) qui plantent sur une console Windows en cp1252 par défaut.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from app.config import load_config
from app.database import Database
from app.logger import setup_logger
from app.notifications.telegram import TelegramBot
from app.scheduler import build_scheduler, run_scan_once, seed_initial_data, seed_wallets


def _build_components(config_path: str):
    config = load_config(config_path)
    setup_logger(level=config.get("logging", {}).get("level", "INFO"),
                 log_file=config.get("logging", {}).get("file", "logs/app.log"))
    db = Database(config.get("database", {}).get("path", "data/reward_hunter.db"))
    telegram_cfg = config.get("telegram", {})
    telegram = TelegramBot(
        token=telegram_cfg.get("bot_token", ""),
        chat_id=telegram_cfg.get("chat_id", ""),
        db=db,
        scan_callback=lambda: run_scan_once(db, None),
    )
    return config, db, telegram


def cmd_seed(args: argparse.Namespace) -> None:
    config, db, _ = _build_components(args.config)
    seed_initial_data(db)
    seed_wallets(db, config)
    print(f"{len(db.list_projects())} projet(s) initialisé(s) en base.")
    print(f"{len(db.list_wallets())} wallet(s) enregistré(s) (adresses publiques).")


def cmd_scan(args: argparse.Namespace) -> None:
    config, db, telegram = _build_components(args.config)
    seed_initial_data(db)  # garantit que les projets existent avant le premier scan
    seed_wallets(db, config)
    changes = run_scan_once(db, telegram)
    print(f"Scan terminé : {len(changes)} changement(s) détecté(s).")


def cmd_run(args: argparse.Namespace) -> None:
    config, db, telegram = _build_components(args.config)
    logger = logging.getLogger("app.main")
    seed_initial_data(db)
    seed_wallets(db, config)

    scheduler_cfg = config.get("scheduler", {})
    scheduler = build_scheduler(
        db, telegram,
        scan_interval_minutes=scheduler_cfg.get("scan_interval_minutes", 30),
        deadline_check_hour=scheduler_cfg.get("deadline_check_hour", 8),
        digest_hour=scheduler_cfg.get("digest_hour", 9),
        opportunity_scan_interval_minutes=scheduler_cfg.get("opportunity_scan_interval_minutes", 120),
        discovery_scan_interval_minutes=scheduler_cfg.get("discovery_scan_interval_minutes", 1440),
        arbitrage_scan_interval_minutes=scheduler_cfg.get("arbitrage_scan_interval_minutes", 5),
        config=config,
    )
    scheduler.start()
    logger.info("Scheduler démarré.")

    dashboard_cfg = config.get("dashboard", {})
    if dashboard_cfg.get("enabled", True):
        import uvicorn

        from app.dashboard import create_app

        dash_app = create_app(db)
        thread = threading.Thread(
            target=lambda: uvicorn.run(
                dash_app,
                host=dashboard_cfg.get("host", "0.0.0.0"),
                port=dashboard_cfg.get("port", 8000),
                log_level="warning",
            ),
            daemon=True,
        )
        thread.start()
        logger.info("Dashboard démarré sur le port %s.", dashboard_cfg.get("port", 8000))

    try:
        if telegram.enabled:
            telegram.run_forever()
        else:
            # Pas de token Telegram configuré : on garde le process vivant quand
            # même (scheduler + dashboard tournent dans des threads en arrière-
            # plan) plutôt que de sortir immédiatement.
            logger.info("Telegram désactivé (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID "
                        "manquants) - scheduler et dashboard restent actifs.")
            threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        scheduler.shutdown(wait=False)


def cmd_telegram(args: argparse.Namespace) -> None:
    config, db, telegram = _build_components(args.config)
    seed_initial_data(db)
    seed_wallets(db, config)
    telegram.run_forever()


def cmd_dashboard(args: argparse.Namespace) -> None:
    import uvicorn

    config, db, _ = _build_components(args.config)
    from app.dashboard import create_app

    dashboard_cfg = config.get("dashboard", {})
    uvicorn.run(
        create_app(db),
        host=dashboard_cfg.get("host", "0.0.0.0"),
        port=dashboard_cfg.get("port", 8000),
    )


def cmd_status(args: argparse.Namespace) -> None:
    from app.dashboard import build_dashboard_text

    _, db, _ = _build_components(args.config)
    print(build_dashboard_text(db))


def cmd_discover_projects(args: argparse.Namespace) -> None:
    """Lance manuellement une découverte de nouveaux projets (debug/test)."""
    from app.discovery.scanner import run_discovery

    config, db, _ = _build_components(args.config)
    found = run_discovery(db)
    if not found:
        print("Aucun nouveau candidat (ou toutes les sources ont déjà été vues). "
              "Consulte 'python main.py' puis le dashboard, section Projets découverts, "
              "pour la liste complète en attente de validation.")
        return
    for e in found:
        hint = f" {e['risk_hint']}" if e.get("risk_hint") else ""
        print(f"- [{e['source']}] {e['title']}\n  {e['url']}{hint}")


def cmd_scan_opportunities(args: argparse.Namespace) -> None:
    """Lance manuellement un scan des micro-récompenses auto-réclamables (debug/test)."""
    from app.trackers.opportunities import scan_opportunities

    config, db, _ = _build_components(args.config)
    seed_initial_data(db)
    seed_wallets(db, config)
    results = scan_opportunities(db)
    if not results:
        print("Aucun candidat de claim déclaré pour l'instant (aucun projet n'expose de contrat "
              "de claim public/permissionless confirmé) — rien à scanner.")
        return
    for r in results:
        status = "✅ réclamé" if r["claimed"] else ("⚪ non rentable" if r["profitable"] is False else "ℹ️ info")
        print(f"- {r['project_name']} [{r['contract_address']}] : {status}"
              f" (valeur≈{r['value_eur']}, frais≈{r['fee_eur']})"
              + (f" — {r['error']}" if r["error"] else ""))


def cmd_scan_arbitrage(args: argparse.Namespace) -> None:
    """Lance manuellement un scan d'arbitrage inter-DEX (debug/test, mode SIMULATION
    uniquement — aucune transaction n'est jamais envoyée par cette commande)."""
    from app.trading.arbitrage import run_arbitrage_scan

    config, db, _ = _build_components(args.config)
    signals = run_arbitrage_scan(db, config)
    if not signals:
        print("Aucun écart de prix significatif détecté sur les paires/DEX configurés "
              "(ou liquidité/API indisponible).")
        return
    for s in signals:
        tag = "✅ rentable (simulation)" if s["would_execute"] else "⚪ non rentable après frais/gas"
        net = f"{s['net_profit_usd']:.3f}$" if s["net_profit_usd"] is not None else "N/A (gas indisponible)"
        print(f"- {s['token_symbol']}/{s['quote_symbol']} : achat {s['buy_dex']} ({s['buy_price_usd']:.6f}$) "
              f"→ vente {s['sell_dex']} ({s['sell_price_usd']:.6f}$) — spread {s['spread_pct']:.2f}% — "
              f"taille≈{s['trade_size_usd']:.2f}$ — net≈{net} — {tag}")


def cmd_create_autosigner_wallet(args: argparse.Namespace) -> None:
    """Génère le wallet dédié à l'auto-signature (Phase 4). À exécuter UNIQUEMENT
    sur la machine qui fera tourner le bot (le RPi3) : la clé privée est générée
    et chiffrée localement, jamais transmise ailleurs."""
    from app.wallet.signer import DEFAULT_KEYSTORE_PATH, create_autosigner_wallet

    result = create_autosigner_wallet(keystore_path=args.keystore_path or DEFAULT_KEYSTORE_PATH)
    print("Wallet auto-signature créé.")
    print(f"  Adresse publique : {result['address']}")
    print(f"  Keystore chiffré : {result['keystore_path']}")
    if result["passphrase_was_generated"]:
        print()
        print("  ⚠️  Passphrase générée automatiquement (affichée UNE SEULE FOIS, non récupérable) :")
        print(f"      {result['passphrase']}")
        print()
        print("  Ajoute ces lignes dans ton .env :")
        print(f"      WALLET_AUTOSIGNER_ADDRESS={result['address']}")
        print(f"      WALLET_AUTOSIGNER_KEYSTORE_PATH={result['keystore_path']}")
        print(f"      WALLET_AUTOSIGNER_PASSPHRASE={result['passphrase']}")
        print("      AUTOSIGN_ENABLED=false")
    else:
        print("  (Passphrase reprise depuis WALLET_AUTOSIGNER_PASSPHRASE déjà présent dans l'environnement.)")
        print(f"  Ajoute : WALLET_AUTOSIGNER_ADDRESS={result['address']}")
        print(f"           WALLET_AUTOSIGNER_KEYSTORE_PATH={result['keystore_path']}")
    print()
    print("Ne transfère des fonds sur cette adresse qu'en petite quantité : c'est un wallet")
    print("opérationnel \"chaud\" dédié à l'automatisation, pas un coffre. Vire régulièrement")
    print("les rewards accumulés vers ton wallet personnel (MetaMask, seed sauvegardée hors-ligne).")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Crypto Reward Hunter / Airdrop Farmer")
    parser.add_argument("--config", default="config/config.yaml", help="Chemin du fichier de config YAML")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("seed").set_defaults(func=cmd_seed)
    sub.add_parser("scan").set_defaults(func=cmd_scan)
    sub.add_parser("run").set_defaults(func=cmd_run)
    sub.add_parser("telegram").set_defaults(func=cmd_telegram)
    sub.add_parser("dashboard").set_defaults(func=cmd_dashboard)
    sub.add_parser("status").set_defaults(func=cmd_status)

    autosigner_parser = sub.add_parser(
        "create-autosigner-wallet",
        help="Génère (une seule fois) le wallet dédié à l'auto-signature — à exécuter sur le RPi3.",
    )
    autosigner_parser.add_argument("--keystore-path", default=None, help="Chemin du keystore chiffré à créer")
    autosigner_parser.set_defaults(func=cmd_create_autosigner_wallet)

    sub.add_parser(
        "scan-opportunities",
        help="Scanne manuellement les micro-récompenses auto-réclamables (debug/test, sans attendre le scheduler).",
    ).set_defaults(func=cmd_scan_opportunities)

    sub.add_parser(
        "discover-projects",
        help="Lance manuellement une découverte de nouveaux projets via les sources publiques (debug/test).",
    ).set_defaults(func=cmd_discover_projects)

    sub.add_parser(
        "scan-arbitrage",
        help="Lance manuellement un scan d'arbitrage inter-DEX (debug/test, SIMULATION uniquement).",
    ).set_defaults(func=cmd_scan_arbitrage)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
