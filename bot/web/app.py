"""Serveur web léger (Flask) : dashboard + API JSON en lecture seule.

Conçu pour un Raspberry Pi 3 : pas de framework front lourd, une page HTML statique
qui interroge /api/* et trace les courbes avec Chart.js (CDN). Le serveur ne fait
que LIRE la base SQLite : il ne peut ni passer d'ordre ni toucher au wallet.

Sécurité : si WEB_PASSWORD est défini dans l'environnement, toutes les routes exigent
une authentification HTTP Basic (utilisateur libre, mot de passe = WEB_PASSWORD).
"""
from __future__ import annotations

import hmac
import os
import time
from collections import deque
from functools import wraps
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request

from bot.config import Config
from bot.db import Database


def _require_auth(password: str):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not password:
                return view(*args, **kwargs)
            auth = request.authorization
            if auth is None or not hmac.compare_digest(auth.password or "", password):
                return Response("Authentification requise", 401, {"WWW-Authenticate": 'Basic realm="trading-bot"'})
            return view(*args, **kwargs)

        return wrapped

    return decorator


def _tail(path: Path, lines: int = 100) -> list[str]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        return list(deque(f, maxlen=lines))


def create_app(config: Config) -> Flask:
    base_dir = Path(__file__).parent
    app = Flask(__name__, template_folder=str(base_dir / "templates"), static_folder=str(base_dir / "static"))

    db = Database(config["database"]["path"])
    quote = config["trading"]["quote_currency"]
    log_file = Path(config["logging"]["file"])
    wallet_cfg = config.get("wallet", {}) or {}
    keystore_path = Path(wallet_cfg.get("keystore_path", "data/keystore.json"))
    password = os.environ.get("WEB_PASSWORD", "")
    protected = _require_auth(password)

    @app.route("/")
    @protected
    def index():
        return render_template("index.html")

    @app.route("/api/overview")
    @protected
    def api_overview():
        trading = config["trading"]
        latest = db.get_latest_equity()
        stats = db.get_trade_stats()
        vault_amount, vault_rewards, _ = db.get_vault(quote)

        wallet_address = None
        if keystore_path.exists():
            try:
                import json

                wallet_address = "0x" + json.loads(keystore_path.read_text(encoding="utf-8"))["address"]
            except Exception:
                wallet_address = None

        # Le bot est considéré actif si un snapshot equity date de moins de 3 cycles
        bot_alive = False
        if latest is not None:
            bot_alive = (time.time() - latest["timestamp"]) < 3 * int(trading["poll_interval_seconds"]) + 30

        return jsonify(
            {
                "mode": trading["mode"],
                "exchange": config["exchange"]["name"],
                "symbols": trading["symbols"],
                "timeframe": trading["timeframe"],
                "strategy": config["strategy"]["name"],
                "quote_currency": quote,
                "bot_alive": bot_alive,
                "balance": float(latest["balance"]) if latest else None,
                "equity": float(latest["equity"]) if latest else None,
                "starting_balance": float(trading.get("starting_balance", 0)),
                "daily_pnl": db.get_daily_pnl(),
                "stats": stats,
                "vault": {"amount": vault_amount, "total_rewards": vault_rewards},
                "wallet_address": wallet_address,
                "open_positions": len(db.get_all_positions()),
                "max_open_positions": int(config["risk"]["max_open_positions"]),
                "server_time": time.time(),
            }
        )

    @app.route("/api/positions")
    @protected
    def api_positions():
        return jsonify([dict(p) for p in db.get_all_positions()])

    @app.route("/api/trades")
    @protected
    def api_trades():
        limit = min(int(request.args.get("limit", 50)), 500)
        return jsonify([dict(t) for t in db.get_recent_trades(limit)])

    @app.route("/api/equity")
    @protected
    def api_equity():
        limit = min(int(request.args.get("limit", 500)), 5000)
        return jsonify([dict(e) for e in db.get_equity_history(limit)])

    @app.route("/api/pnl_by_symbol")
    @protected
    def api_pnl_by_symbol():
        return jsonify([dict(r) for r in db.get_pnl_by_symbol()])

    @app.route("/api/logs")
    @protected
    def api_logs():
        lines = min(int(request.args.get("lines", 100)), 1000)
        return jsonify(_tail(log_file, lines))

    return app
