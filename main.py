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
import os

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


def _apply_overrides(config, overrides: list[str]) -> None:
    """Applique des surcharges `section.cle=valeur` à la config chargée (valeurs parsées en YAML)."""
    import yaml

    for item in overrides or []:
        path, _, raw = item.partition("=")
        keys = path.split(".")
        node = config.raw
        for key in keys[:-1]:
            node = node.setdefault(key, {})
        node[keys[-1]] = yaml.safe_load(raw)


def cmd_backtest(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    _apply_overrides(config, args.set)
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


def _get_wallet(config, role: str = "bot"):
    from bot.wallet import Wallet

    wallet_cfg = config.get("wallet", {}) or {}
    key = "vault_keystore_path" if role == "vault" else "keystore_path"
    default = "data/keystore_vault.json" if role == "vault" else "data/keystore.json"
    return Wallet(
        keystore_path=wallet_cfg.get(key, default),
        rpc_url=wallet_cfg.get("rpc_url", "https://bsc-dataseed.binance.org"),
        chain_id=int(wallet_cfg.get("chain_id", 56)),
    ), wallet_cfg


def _wallet_password(role: str = "bot", confirm: bool = False) -> str:
    import getpass
    import os

    env_var = "VAULT_PASSWORD" if role == "vault" else "WALLET_PASSWORD"
    password = os.environ.get(env_var, "")
    if not password:
        password = getpass.getpass(f"Mot de passe du wallet {role.upper()}: ")
        if confirm:
            confirmation = getpass.getpass(f"Confirme le mot de passe du wallet {role.upper()}: ")
            if password != confirmation:
                raise SystemExit("Les mots de passe ne correspondent pas; aucun wallet créé.")
    return password


def _build_treasury(config):
    from bot.treasury import Treasury

    bot_wallet, wallet_cfg = _get_wallet(config, "bot")
    vault_wallet, _ = _get_wallet(config, "vault")
    sweep_cfg = wallet_cfg.get("auto_sweep", {}) or {}
    return Treasury(
        bot_wallet=bot_wallet,
        vault_wallet=vault_wallet,
        usdt_contract=wallet_cfg.get("usdt_contract", "0x55d398326f99059fF775485246999027B3197955"),
        usdt_decimals=int(wallet_cfg.get("usdt_decimals", 18)),
        sweep_threshold=float(sweep_cfg.get("threshold", 200.0)),
        keep_on_bot=float(sweep_cfg.get("keep_on_bot", 50.0)),
    )


def cmd_wallet(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    setup_logger(level=config["logging"]["level"], log_file=config["logging"]["file"])
    role = getattr(args, "role", "bot")
    wallet, wallet_cfg = _get_wallet(config, role)

    if args.wallet_command == "create":
        password = _wallet_password(role, confirm=True)
        if not password:
            raise SystemExit("Mot de passe vide refusé.")
        address = wallet.create(wallet.keystore_path, password)
        print(f"Wallet {role.upper()} créé. Adresse: {address}")
        print(f"Keystore chiffré: {wallet.keystore_path}")
        print("⚠️  Sauvegarde ce fichier keystore ET le mot de passe hors du Pi (sans les deux, fonds perdus).")
        if role == "vault":
            print("⚠️  Ne mets PAS VAULT_PASSWORD dans le .env du Pi : le bot ne doit jamais pouvoir vider le vault.")

    elif args.wallet_command == "address":
        print(wallet.address)

    elif args.wallet_command == "balance":
        treasury = _build_treasury(config)
        for name, info in treasury.balances().items():
            print(f"=== {name.upper()} ===")
            if info is None:
                print("  non créé")
                continue
            print(f"  Adresse : {info['address']}")
            print(f"  BNB     : {info['bnb']:.6f}")
            print(f"  USDT    : {info['usdt']:.4f}")

    elif args.wallet_command == "sweep":
        # Retire du USDT de l'exchange vers le wallet BOT (nécessite le droit 'withdraw' sur la clé API)
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
        # Envoie du USDT depuis le wallet choisi vers une adresse (ex: dépôt exchange)
        password = _wallet_password(role)
        token = wallet_cfg.get("usdt_contract", "0x55d398326f99059fF775485246999027B3197955")
        decimals = int(wallet_cfg.get("usdt_decimals", 18))
        tx_hash = wallet.send_token(password, token, args.to, args.amount, decimals)
        print(f"Transaction envoyée depuis {role.upper()}: {tx_hash}")

    elif args.wallet_command == "to-vault":
        treasury = _build_treasury(config)
        amount = args.amount if args.amount else treasury.sweep_amount()
        if amount <= 0:
            print("Rien à transférer (sous le seuil ou wallet BOT vide).")
            return
        print(f"Transfert de {amount:.4f} USDT du wallet BOT vers le VAULT {treasury.vault.address}...")
        tx_hash = treasury.sweep_to_vault(_wallet_password("bot"), amount)
        print(f"Transaction: {tx_hash}")


def cmd_dex(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    from bot.dex import PancakeQuoter

    dex_cfg = config.get("dex", {}) or {}
    wallet_cfg = config.get("wallet", {}) or {}
    quoter = PancakeQuoter(
        rpc_url=wallet_cfg.get("rpc_url", "https://bsc-dataseed.binance.org"),
        router=dex_cfg.get("router", "0x10ED43C718714eb63d5aA57B78B54704E256024E"),
        tokens=dex_cfg.get("tokens") or {},
    )
    exch_cfg = config["exchange"]
    exchange = ExchangeClient(name=exch_cfg["name"], sandbox=False)
    notional = args.notional or float(dex_cfg.get("notional", 100.0))
    symbols = args.symbols.split(",") if args.symbols else config["trading"]["symbols"]

    rows = []
    for symbol in symbols:
        cex = exchange.fetch_ticker_price(symbol)
        try:
            dex = quoter.quote_price(symbol, notional)
            rows.append([symbol, f"{cex:.4f}", f"{dex:.4f}", f"{quoter.spread_pct(cex, dex):+.3f} %"])
        except Exception as exc:
            rows.append([symbol, f"{cex:.4f}", "n/a", str(exc)[:40]])
    print(f"\nCotation pour {notional:g} USDT (PancakeSwap V2 vs {exch_cfg['name']})")
    print(tabulate(rows, headers=["Symbole", "CEX", "DEX", "Écart DEX/CEX"]))


def cmd_rewards(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    setup_logger(level=config["logging"]["level"], log_file=config["logging"]["file"])
    from bot.rewards import RewardsCollector

    db = Database(config["database"]["path"])
    trading_cfg = config["trading"]
    rewards_cfg = config.get("rewards", {}) or {}
    live_override = bool(getattr(args, "live", False))
    mode = "live" if live_override else trading_cfg["mode"]

    exchange = None
    if mode == "live":
        exch_cfg = config["exchange"]
        if live_override:
            api_key = os.environ.get("REWARDS_API_KEY", "")
            api_secret = os.environ.get("REWARDS_API_SECRET", "")
            if not api_key or not api_secret:
                raise SystemExit("Renseigne REWARDS_API_KEY et REWARDS_API_SECRET pour le compte Binance réel.")
        else:
            if bool(exch_cfg.get("sandbox", True)):
                raise SystemExit("Collecte live refusée: le mode sandbox du compte trading est actif.")
            api_key = exch_cfg.get("api_key", "")
            api_secret = exch_cfg.get("api_secret", "")

        if not live_override and args.rewards_command == "collect" and not rewards_cfg.get("enabled", False):
            raise SystemExit("Collecte live refusée: active rewards.enabled dans la configuration.")
        exchange = ExchangeClient(
            name=exch_cfg["name"],
            api_key=api_key,
            api_secret=api_secret,
            sandbox=False,
        )

    asset = rewards_cfg.get("asset") or trading_cfg["quote_currency"]
    collector = RewardsCollector(
        db=db,
        mode=mode,
        exchange=exchange,
        asset=asset,
        paper_apr_pct=float(rewards_cfg.get("paper_apr_pct", 5.0)),
        profit_skim_pct=float(rewards_cfg.get("profit_skim_pct", 30.0)),
        min_idle_to_subscribe=float(rewards_cfg.get("min_idle_to_subscribe", 50.0)),
        keep_free=float(rewards_cfg.get("keep_free", 100.0)),
        min_trading_balance=float(rewards_cfg.get("min_trading_balance", 50.0)),
        dust_enabled=bool(rewards_cfg.get("dust_enabled", True)),
    )

    if args.rewards_command == "collect":
        if mode == "live":
            collector.validate_live_access()
        collector.collect()
        if mode == "live":
            free_balance = exchange.fetch_balance(asset)
            subscribed = collector.subscribe_idle(free_balance)
            print(f"Collecte live terminée. Souscription Simple Earn: {subscribed:.4f} {asset}.")
        else:
            print("Collecte effectuée.")

    print(f"Coffre ({asset}) : {collector.vault_balance():.4f}")
    print(f"Récompenses cumulées : {collector.total_rewards():.4f}")


def cmd_check(args: argparse.Namespace) -> None:
    """Vérifie la connexion à l'exchange avec les clés du .env, sans jamais les afficher."""
    config = load_config(args.config)
    exch_cfg = config["exchange"]
    trading_cfg = config["trading"]
    key, secret = exch_cfg.get("api_key", ""), exch_cfg.get("api_secret", "")

    print(f"Exchange : {exch_cfg['name']} | sandbox/testnet : {exch_cfg.get('sandbox', True)} | mode : {trading_cfg['mode']}")
    print(f"Clé API  : {'présente (' + str(len(key)) + ' car.)' if key else 'ABSENTE'} | secret : {'présent' if secret else 'ABSENT'}")
    if not key or not secret:
        raise SystemExit("Renseigne EXCHANGE_API_KEY et EXCHANGE_API_SECRET dans .env")

    exchange = ExchangeClient(name=exch_cfg["name"], api_key=key, api_secret=secret, sandbox=bool(exch_cfg.get("sandbox", True)))
    print(f"Prix {trading_cfg['symbols'][0]} : {exchange.fetch_ticker_price(trading_cfg['symbols'][0]):.4f}  (données publiques OK)")
    try:
        balance = exchange.exchange.fetch_balance()
    except Exception as exc:
        raise SystemExit(f"❌ Authentification refusée : {type(exc).__name__}: {str(exc)[:200]}")
    nonzero = {k: v for k, v in balance.get("total", {}).items() if v}
    print("✅ Authentification OK. Soldes non nuls :")
    for asset, amount in sorted(nonzero.items()):
        print(f"   {asset:<8} {amount}")
    if not nonzero:
        print("   (aucun)")


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
    p_backtest.add_argument("--set", action="append", default=[], metavar="SECTION.CLE=VALEUR",
                            help="Surcharge un paramètre (ex: --set trading.timeframe=1h --set risk.trailing_stop_pct=3)")
    p_backtest.set_defaults(func=cmd_backtest)

    p_status = sub.add_parser("status", help="Affiche l'état actuel du bot")
    p_status.set_defaults(func=cmd_status)

    p_check = sub.add_parser("check", help="Teste la connexion API à l'exchange (sans afficher les clés)")
    p_check.set_defaults(func=cmd_check)

    p_wallet = sub.add_parser("wallet", help="Gère les wallets on-chain (BOT opérationnel / VAULT coffre-fort)")
    p_wallet.add_argument("--role", choices=["bot", "vault"], default="bot", help="Wallet ciblé (défaut: bot)")
    wallet_sub = p_wallet.add_subparsers(dest="wallet_command", required=True)
    wallet_sub.add_parser("create", help="Crée un wallet (keystore chiffré)")
    wallet_sub.add_parser("address", help="Affiche l'adresse publique")
    wallet_sub.add_parser("balance", help="Affiche les soldes BNB/USDT des deux wallets")
    p_sweep = wallet_sub.add_parser("sweep", help="Retire du USDT de l'exchange vers le wallet BOT")
    p_sweep.add_argument("--amount", type=float, required=True, help="Montant USDT à retirer")
    p_send = wallet_sub.add_parser("send", help="Envoie du USDT du wallet vers une adresse")
    p_send.add_argument("--to", required=True, help="Adresse destinataire (ex: dépôt exchange)")
    p_send.add_argument("--amount", type=float, required=True, help="Montant USDT à envoyer")
    p_to_vault = wallet_sub.add_parser("to-vault", help="Déplace l'excédent USDT du BOT vers le VAULT")
    p_to_vault.add_argument("--amount", type=float, default=None, help="Montant (défaut: excédent selon config)")
    p_wallet.set_defaults(func=cmd_wallet)

    p_dex = sub.add_parser("dex", help="Compare les prix CEX et PancakeSwap (lecture seule)")
    p_dex.add_argument("--symbols", default="", help="Symboles séparés par des virgules")
    p_dex.add_argument("--notional", type=float, default=None, help="Montant USDT simulé")
    p_dex.set_defaults(func=cmd_dex)

    p_rewards = sub.add_parser("rewards", help="Coffre et collecte de récompenses")
    rewards_sub = p_rewards.add_subparsers(dest="rewards_command", required=True)
    p_rewards_status = rewards_sub.add_parser("status", help="Affiche le solde du coffre et les récompenses cumulées")
    p_rewards_status.add_argument("--live", action="store_true", help="Lit le coffre Simple Earn du compte réel")
    p_rewards_collect = rewards_sub.add_parser("collect", help="Lance un cycle de collecte immédiat")
    p_rewards_collect.add_argument("--live", action="store_true", help="Collecte sur le compte réel sans lancer le trading")
    p_rewards.set_defaults(func=cmd_rewards)

    p_web = sub.add_parser("web", help="Démarre le dashboard web")
    p_web.add_argument("--host", default=None, help="Adresse d'écoute (défaut: config ou 0.0.0.0)")
    p_web.add_argument("--port", type=int, default=None, help="Port (défaut: config ou 8080)")
    p_web.set_defaults(func=cmd_web)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
