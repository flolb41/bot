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


def cmd_import_trading_wallet(args: argparse.Namespace) -> None:
    """Importe le wallet MetaMask PRINCIPAL de l'utilisateur comme wallet de
    TRADING (distinct du wallet auto-signature). ⚠️ COMMANDE À RISQUE :
    - Exécute cette commande UNIQUEMENT en te connectant toi-même en SSH
      directement sur le Pi (jamais via un script tiers, jamais via l'IA).
    - La clé privée est saisie via `getpass` : elle ne s'affiche jamais à
      l'écran et n'est jamais écrite dans l'historique shell.
    - La passphrase générée est écrite AUTOMATIQUEMENT dans le `.env` par ce
      script (évite tout risque de faute de frappe en la recopiant à la main).
    - Après import, le trading réel reste DÉSACTIVÉ par défaut (plusieurs
      garde-fous séparés à activer volontairement, voir message final)."""
    import getpass
    import os
    from pathlib import Path

    from app.trading.guardrails import DEFAULT_TRADING_KEYSTORE_PATH, LIVE_CONFIRMATION_PHRASE
    from app.wallet.signer import import_wallet_from_private_key

    print("=" * 70)
    print("IMPORT DU WALLET DE TRADING (wallet MetaMask PRINCIPAL)")
    print("=" * 70)
    print("⚠️  Ne fais JAMAIS cela dans un terminal partagé, un script, ou via un")
    print("   assistant distant. Cette commande doit être tapée par TOI, en direct,")
    print("   dans une session SSH que tu contrôles sur le Pi.")
    print()
    if args.overwrite:
        print("⚠️  --overwrite : le keystore existant (s'il y en a un) sera REMPLACÉ et sa")
        print("   passphrase actuelle deviendra définitivement inutilisable.")
        print()
    private_key = getpass.getpass("Clé privée du wallet (saisie masquée, jamais affichée) : ").strip()
    if not private_key:
        print("Aucune clé saisie — abandon.")
        return
    keystore_path = args.keystore_path or DEFAULT_TRADING_KEYSTORE_PATH
    try:
        result = import_wallet_from_private_key(private_key, keystore_path=keystore_path, overwrite=args.overwrite)
    finally:
        private_key = None  # noqa: F841 - abandon de la référence au plus vite
    print()
    print("Wallet de trading importé avec succès.")
    print(f"  Adresse publique : {result['address']}")
    print(f"  Keystore chiffré : {result['keystore_path']}")

    env_path = Path(".env")
    lines_to_write = [f"WALLET_TRADING_KEYSTORE_PATH={result['keystore_path']}"]
    if result["passphrase_was_generated"]:
        print()
        print("  ⚠️  Passphrase générée automatiquement (affichée UNE SEULE FOIS) :")
        print(f"      {result['passphrase']}")
        lines_to_write.append(f"WALLET_TRADING_PASSPHRASE={result['passphrase']}")

    # Écriture directe dans .env (plutôt que de faire recopier la passphrase à la
    # main) : élimine le risque de faute de frappe/transcription qui rendrait le
    # keystore illisible. On ne touche que les clés WALLET_TRADING_*/TRADING_ENABLED
    # et on retire d'abord les éventuelles anciennes valeurs pour éviter les doublons.
    existing = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    keys_managed = {"WALLET_TRADING_KEYSTORE_PATH", "WALLET_TRADING_PASSPHRASE"}
    filtered = [ln for ln in existing if not any(ln.startswith(f"{k}=") for k in keys_managed)]
    if not any(ln.startswith("TRADING_ENABLED=") for ln in filtered):
        filtered.append("TRADING_ENABLED=false")
    filtered.extend(lines_to_write)
    env_path.write_text("\n".join(filtered) + "\n", encoding="utf-8")
    try:
        os.chmod(env_path, 0o600)
    except OSError:
        pass

    print()
    print(f"  ✅ .env mis à jour automatiquement ({env_path.resolve()}).")
    print("Le trading réel reste DÉSACTIVÉ tant que TOUTES ces conditions ne sont pas")
    print("réunies volontairement (voir app/trading/guardrails.py) :")
    print("  1. trading.enabled: true dans config/config.yaml")
    print("  2. TRADING_ENABLED=true dans le .env")
    print(f"  3. TRADING_LIVE_CONFIRMED=\"{LIVE_CONFIRMATION_PHRASE}\" dans le .env")
    print("  4. Le killswitch n'est pas actif")
    print()
    print("Redémarre le service pour que ces changements soient pris en compte :")
    print("  sudo systemctl restart crypto-reward-hunter.service")
    print()
    print("Ne transfère sur cette adresse QUE la petite somme que tu es prêt à risquer")
    print("(le capital de test, ex. 5$) — ce n'est pas fait pour stocker ton épargne.")


def cmd_execute_arbitrage(args: argparse.Namespace) -> None:
    """Scanne puis tente d'EXÉCUTER RÉELLEMENT les signaux rentables (debug/test
    manuel du chemin live). Ne fait rien de plus dangereux que le scheduler : tous
    les mêmes garde-fous s'appliquent (is_trading_live, whitelist, simulation
    préalable, caps quotidiens, etc.) — reste sans effet tant qu'ils ne sont pas
    tous explicitement activés."""
    from app.trading.arbitrage import run_arbitrage_scan
    from app.trading.executor import execute_signal
    from app.trading.guardrails import is_trading_live

    config, db, _ = _build_components(args.config)
    if not is_trading_live(config):
        print("Trading live désactivé (voir app/trading/guardrails.py::is_trading_live) — "
              "cette commande ne fera qu'un scan en simulation, aucune transaction.")
    signals = run_arbitrage_scan(db, config)
    profitable = [s for s in signals if s["would_execute"]]
    if not profitable:
        print("Aucune opportunité rentable détectée pour le moment.")
        return
    for signal in profitable:
        result = execute_signal(db, config, signal)
        status = "✅ EXÉCUTÉ" if result["executed"] else "⚪ non exécuté"
        print(f"- {signal['token_symbol']}/{signal['quote_symbol']} ({signal['buy_dex']}→{signal['sell_dex']}) : "
              f"{status}")
        if result["tx_hash_buy"]:
            print(f"    tx achat : {result['tx_hash_buy']}")
        if result["tx_hash_sell"]:
            print(f"    tx vente : {result['tx_hash_sell']}")
        if result["error"]:
            print(f"    détail  : {result['error']}")


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

    import_trading_parser = sub.add_parser(
        "import-trading-wallet",
        help="⚠️ Importe ton wallet MetaMask PRINCIPAL comme wallet de trading. "
             "À exécuter TOI-MÊME en SSH direct sur le Pi, jamais à distance.",
    )
    import_trading_parser.add_argument("--keystore-path", default=None, help="Chemin du keystore chiffré à créer")
    import_trading_parser.add_argument(
        "--overwrite", action="store_true",
        help="Remplace un keystore existant (invalide l'accès via l'ancienne passphrase).",
    )
    import_trading_parser.set_defaults(func=cmd_import_trading_wallet)

    sub.add_parser(
        "execute-arbitrage",
        help="Scanne et tente d'exécuter réellement les opportunités rentables "
             "(sans effet tant que le trading live n'est pas explicitement activé).",
    ).set_defaults(func=cmd_execute_arbitrage)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
