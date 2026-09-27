#!/usr/bin/env python3
"""Point d'entrée CLI du bot de trading.

Usage:
    python main.py run                       # démarre la boucle de trading (paper ou live selon config)
    python main.py backtest --days 30         # lance un backtest sur l'historique récent
    python main.py status                     # affiche positions ouvertes et derniers trades
"""
from __future__ import annotations

import argparse
import logging

from tabulate import tabulate

from bot.backtester import fetch_historical, print_report, run_backtest
from bot.config import load_config
from bot.db import Database
from bot.engine import TradingEngine
from bot.exchange import ExchangeClient
from bot.logger import setup_logger
from bot.risk import RiskManager, RiskParams
from bot.strategies import get_strategy


def cmd_run(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    setup_logger(level=config["logging"]["level"], log_file=config["logging"]["file"])
    engine = TradingEngine(config)
    engine.run()


def cmd_backtest(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    setup_logger(level=config["logging"]["level"], log_file=config["logging"]["file"])
    logger = logging.getLogger("bot.backtest")

    exch_cfg = config["exchange"]
    exchange = ExchangeClient(
        name=exch_cfg["name"],
        api_key="",
        api_secret="",
        sandbox=False,  # les données historiques publiques ne nécessitent pas le testnet
    )

    trading_cfg = config["trading"]
    strat_cfg = config["strategy"]
    risk_cfg = config["risk"]
    strategy = get_strategy(strat_cfg["name"], strat_cfg.get("params", {}))
    risk = RiskManager(
        RiskParams(
            max_open_positions=int(risk_cfg["max_open_positions"]),
            stop_loss_pct=float(risk_cfg["stop_loss_pct"]),
            take_profit_pct=float(risk_cfg["take_profit_pct"]),
            trailing_stop_pct=float(risk_cfg.get("trailing_stop_pct", 0)),
            max_daily_loss_pct=float(risk_cfg["max_daily_loss_pct"]),
        ),
        Database(config["database"]["path"]),
    )

    symbols = args.symbols.split(",") if args.symbols else trading_cfg["symbols"]
    for symbol in symbols:
        logger.info("Téléchargement de l'historique pour %s (%d jours)...", symbol, args.days)
        df = fetch_historical(exchange, symbol, trading_cfg["timeframe"], args.days)
        result = run_backtest(
            df,
            symbol,
            strategy,
            risk,
            starting_balance=float(trading_cfg["starting_balance"]),
            allocation_pct=float(trading_cfg["allocation_pct"]),
            fee_pct=float(trading_cfg.get("fee_pct", 0.1)),
        )
        print_report(result)


def cmd_status(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    db = Database(config["database"]["path"])

    positions = db.get_all_positions()
    print("\n=== Positions ouvertes ===")
    if positions:
        rows = [[p["symbol"], p["side"], p["entry_price"], p["amount"], p["stop_loss"], p["take_profit"]] for p in positions]
        print(tabulate(rows, headers=["Symbole", "Côté", "Prix entrée", "Quantité", "Stop-loss", "Take-profit"]))
    else:
        print("Aucune position ouverte.")

    trades = db.get_recent_trades(15)
    print("\n=== Derniers trades ===")
    if trades:
        rows = [[t["symbol"], t["side"], t["price"], t["amount"], round(t["pnl"], 4), t["reason"]] for t in trades]
        print(tabulate(rows, headers=["Symbole", "Côté", "Prix", "Quantité", "PnL", "Raison"]))
    else:
        print("Aucun trade enregistré.")

    equity = db.get_latest_equity()
    if equity:
        print(f"\nSolde: {equity['balance']:.2f} | Equity: {equity['equity']:.2f}")

    quote = config["trading"]["quote_currency"]
    vault_amount, vault_rewards, _ = db.get_vault(quote)
    if vault_amount > 0 or vault_rewards > 0:
        print(f"Coffre: {vault_amount:.2f} {quote} | Récompenses cumulées: {vault_rewards:.4f} {quote}")


def _get_wallet(config):
    from bot.wallet import Wallet

    wallet_cfg = config.get("wallet", {}) or {}
    return Wallet(
        keystore_path=wallet_cfg.get("keystore_path", "data/keystore.json"),
        rpc_url=wallet_cfg.get("rpc_url", "https://bsc-dataseed.binance.org"),
        chain_id=int(wallet_cfg.get("chain_id", 56)),
    ), wallet_cfg


def _wallet_password() -> str:
    import getpass
    import os

    password = os.environ.get("WALLET_PASSWORD", "")
    if not password:
        password = getpass.getpass("Mot de passe du wallet: ")
    return password


def cmd_wallet(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    setup_logger(level=config["logging"]["level"], log_file=config["logging"]["file"])
    wallet, wallet_cfg = _get_wallet(config)

    if args.wallet_command == "create":
        password = _wallet_password()
        if not password:
            raise SystemExit("Mot de passe vide refusé.")
        address = wallet.create(wallet.keystore_path, password)
        print(f"Wallet créé. Adresse: {address}")
        print(f"Keystore chiffré: {wallet.keystore_path}")
        print("⚠️  Sauvegarde ce fichier keystore ET le mot de passe hors du Pi (sans les deux, fonds perdus).")

    elif args.wallet_command == "address":
        print(wallet.address)

    elif args.wallet_command == "balance":
        print(f"Adresse : {wallet.address}")
        print(f"BNB     : {wallet.native_balance():.6f}")
        token = wallet_cfg.get("usdt_contract", "0x55d398326f99059fF775485246999027B3197955")
        decimals = int(wallet_cfg.get("usdt_decimals", 18))
        print(f"USDT    : {wallet.token_balance(token, decimals):.4f}")

    elif args.wallet_command == "sweep":
        # Retire du USDT de l'exchange vers le wallet local (nécessite le droit 'withdraw' sur la clé API)
        exch_cfg = config["exchange"]
        exchange = ExchangeClient(
            name=exch_cfg["name"],
            api_key=exch_cfg.get("api_key", ""),
            api_secret=exch_cfg.get("api_secret", ""),
            sandbox=bool(exch_cfg.get("sandbox", True)),
        )
        network = wallet_cfg.get("network", "BSC")
        print(f"Retrait de {args.amount} USDT vers {wallet.address} (réseau {network})...")
        result = exchange.withdraw("USDT", args.amount, wallet.address, network)
        print(f"Retrait soumis: {result.get('id', result)}")

    elif args.wallet_command == "send":
        # Renvoie du USDT du wallet vers une adresse (ex: adresse de dépôt de l'exchange)
        password = _wallet_password()
        token = wallet_cfg.get("usdt_contract", "0x55d398326f99059fF775485246999027B3197955")
        decimals = int(wallet_cfg.get("usdt_decimals", 18))
        tx_hash = wallet.send_token(password, token, args.to, args.amount, decimals)
        print(f"Transaction envoyée: {tx_hash}")


def cmd_rewards(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    setup_logger(level=config["logging"]["level"], log_file=config["logging"]["file"])
    from bot.rewards import RewardsCollector

    db = Database(config["database"]["path"])
    trading_cfg = config["trading"]
    rewards_cfg = config.get("rewards", {}) or {}
    mode = trading_cfg["mode"]

    exchange = None
    if mode == "live":
        exch_cfg = config["exchange"]
        exchange = ExchangeClient(
            name=exch_cfg["name"],
            api_key=exch_cfg.get("api_key", ""),
            api_secret=exch_cfg.get("api_secret", ""),
            sandbox=bool(exch_cfg.get("sandbox", True)),
        )

    collector = RewardsCollector(
        db=db,
        mode=mode,
        exchange=exchange,
        asset=trading_cfg["quote_currency"],
        paper_apr_pct=float(rewards_cfg.get("paper_apr_pct", 5.0)),
        profit_skim_pct=float(rewards_cfg.get("profit_skim_pct", 30.0)),
        min_idle_to_subscribe=float(rewards_cfg.get("min_idle_to_subscribe", 50.0)),
        keep_free=float(rewards_cfg.get("keep_free", 100.0)),
        min_trading_balance=float(rewards_cfg.get("min_trading_balance", 50.0)),
        dust_enabled=bool(rewards_cfg.get("dust_enabled", True)),
    )

    if args.rewards_command == "collect":
        collector.collect()
        print("Collecte effectuée.")

    print(f"Coffre ({trading_cfg['quote_currency']}) : {collector.vault_balance():.4f}")
    print(f"Récompenses cumulées : {collector.total_rewards():.4f}")


def cmd_web(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    setup_logger(level=config["logging"]["level"], log_file=config["logging"]["file"])
    from bot.web.app import create_app

    web_cfg = config.get("web", {}) or {}
    host = args.host or web_cfg.get("host", "0.0.0.0")
    port = args.port or int(web_cfg.get("port", 8080))
    app = create_app(config)
    logging.getLogger("bot.web").info("Dashboard disponible sur http://%s:%d", host, port)
    # Serveur WSGI intégré : suffisant pour un usage LAN mono-utilisateur sur le Pi
    app.run(host=host, port=port, debug=False, threaded=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Bot de trading crypto")
    parser.add_argument("--config", default="config/config.yaml", help="Chemin du fichier de configuration")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="Démarre la boucle de trading")
    p_run.set_defaults(func=cmd_run)

    p_backtest = sub.add_parser("backtest", help="Lance un backtest sur données historiques")
    p_backtest.add_argument("--days", type=int, default=30, help="Nombre de jours d'historique")
    p_backtest.add_argument("--symbols", default="", help="Liste de symboles séparés par des virgules")
    p_backtest.set_defaults(func=cmd_backtest)

    p_status = sub.add_parser("status", help="Affiche l'état actuel du bot")
    p_status.set_defaults(func=cmd_status)

    p_wallet = sub.add_parser("wallet", help="Gère le wallet on-chain local sécurisé")
    wallet_sub = p_wallet.add_subparsers(dest="wallet_command", required=True)
    wallet_sub.add_parser("create", help="Crée un nouveau wallet (keystore chiffré)")
    wallet_sub.add_parser("address", help="Affiche l'adresse publique")
    wallet_sub.add_parser("balance", help="Affiche les soldes BNB et USDT on-chain")
    p_sweep = wallet_sub.add_parser("sweep", help="Retire du USDT de l'exchange vers le wallet")
    p_sweep.add_argument("--amount", type=float, required=True, help="Montant USDT à retirer")
    p_send = wallet_sub.add_parser("send", help="Envoie du USDT du wallet vers une adresse")
    p_send.add_argument("--to", required=True, help="Adresse destinataire (ex: dépôt exchange)")
    p_send.add_argument("--amount", type=float, required=True, help="Montant USDT à envoyer")
    p_wallet.set_defaults(func=cmd_wallet)

    p_rewards = sub.add_parser("rewards", help="Coffre et collecte de récompenses")
    rewards_sub = p_rewards.add_subparsers(dest="rewards_command", required=True)
    rewards_sub.add_parser("status", help="Affiche le solde du coffre et les récompenses cumulées")
    rewards_sub.add_parser("collect", help="Lance un cycle de collecte immédiat")
    p_rewards.set_defaults(func=cmd_rewards)

    p_web = sub.add_parser("web", help="Démarre le dashboard web")
    p_web.add_argument("--host", default=None, help="Adresse d'écoute (défaut: config ou 0.0.0.0)")
    p_web.add_argument("--port", type=int, default=None, help="Port (défaut: config ou 8080)")
    p_web.set_defaults(func=cmd_web)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
