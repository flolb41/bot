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
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
