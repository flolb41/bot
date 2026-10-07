"""Notifications et commandes Telegram pour le bot d'arbitrage.

Utilise l'API HTTP Telegram directement (pas de dépendance lourde), adapté
aux ressources limitées du Raspberry Pi 3. Toutes les commandes sont en
LECTURE SEULE sauf /stop et /start qui pilotent le killswitch global.

Chaque message envoyé via `send()` est aussi journalisé en base (table
`notifications`), consultable depuis le dashboard web — ainsi Telegram est
optionnel : le dashboard affiche les mêmes alertes même sans bot configuré.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Callable

import httpx

from app.database import Database
from app.killswitch import is_stopped, resume, stop

logger = logging.getLogger("app.telegram")

API_BASE = "https://api.telegram.org/bot{token}"
_TAG_RE = re.compile(r"<[^>]+>")


def _plain_title(text: str, max_len: int = 80) -> str:
    """Dérive un titre lisible (sans HTML) à partir de la première ligne du message."""
    first_line = text.strip().split("\n", 1)[0]
    title = _TAG_RE.sub("", first_line).strip() or "Notification"
    return title[:max_len]


class TelegramBot:
    def __init__(self, token: str, chat_id: str, db: Database, scan_callback: Callable[[], None] | None = None):
        self.token = token
        self.chat_id = str(chat_id)
        self.db = db
        self.scan_callback = scan_callback
        self.enabled = bool(token) and bool(chat_id)
        self._offset = 0
        self._client = httpx.Client(timeout=30.0)

    # --- bas niveau -------------------------------------------------------
    def _url(self, method: str) -> str:
        return f"{API_BASE.format(token=self.token)}/{method}"

    def send(self, text: str, chat_id: str | None = None, level: str = "info",
             project_id: str | None = None) -> None:
        # Toujours journalisé en base (visible dans le dashboard web), que
        # Telegram soit configuré ou non.
        try:
            self.db.add_notification(title=_plain_title(text), message=text,
                                      level=level, project_id=project_id)
        except Exception:
            logger.exception("Échec de l'enregistrement de la notification en base.")

        if not self.enabled:
            logger.debug("Telegram désactivé (token/chat_id manquant) - message non envoyé: %s", text[:80])
            return
        try:
            self._client.post(
                self._url("sendMessage"),
                data={"chat_id": chat_id or self.chat_id, "text": text, "parse_mode": "HTML"},
            )
        except httpx.HTTPError as exc:
            logger.warning("Échec de l'envoi Telegram: %s", exc)

    def get_updates(self, timeout: int = 20) -> list[dict]:
        if not self.enabled:
            return []
        try:
            resp = self._client.get(
                self._url("getUpdates"),
                params={"offset": self._offset, "timeout": timeout},
                timeout=timeout + 10,
            )
            data = resp.json()
            updates = data.get("result", [])
            if updates:
                self._offset = updates[-1]["update_id"] + 1
            return updates
        except httpx.HTTPError as exc:
            logger.warning("Échec de la récupération des updates Telegram: %s", exc)
            return []

    # --- commandes ----------------------------------------------------------
    def handle_update(self, update: dict) -> None:
        message = update.get("message") or {}
        text = (message.get("text") or "").strip()
        from_chat = str(message.get("chat", {}).get("id", ""))

        if not text.startswith("/"):
            return
        # Sécurité : n'exécute une commande que pour le chat_id configuré.
        if self.chat_id and from_chat != self.chat_id:
            logger.warning("Commande ignorée depuis un chat non autorisé: %s", from_chat)
            return

        command = text.split()[0].lower().lstrip("/")
        handler = {
            "start": self.cmd_start,
            "arbitrage": self.cmd_arbitrage,
            "status": self.cmd_status,
            "scan": self.cmd_scan,
            "stop": self.cmd_stop,
        }.get(command)

        if handler:
            try:
                handler()
            except Exception:
                logger.exception("Erreur lors du traitement de la commande /%s", command)
                self.send(f"⚠️ Erreur lors du traitement de /{command}. Voir les logs.")
        else:
            self.send("Commande inconnue. Commandes disponibles : /start /arbitrage /status /scan /stop")

    def cmd_start(self) -> None:
        self.send(
            "🤖 <b>Bot d'arbitrage inter-DEX</b>\n\n"
            "Commandes : /arbitrage /status /scan /stop"
        )
        resume(self.db)

    def cmd_arbitrage(self) -> None:
        stats = self.db.arbitrage_stats_summary()
        live = self.db.live_trading_stats()
        lines = [
            "📈 <b>Arbitrage</b>",
            f"Signaux détectés : {stats.get('total', 0)}",
            f"Dont rentables : {stats.get('would_execute_count', 0)}",
            f"P&L cumulé (simulation) : {stats.get('cumulative_net_profit_usd') or 0:.2f} $",
            "",
            f"Trades réels : {live.get('total_live', 0)} "
            f"(terminés : {live.get('completed', 0)}, ouverts : {live.get('open_count', 0)})",
        ]
        recent = self.db.list_arbitrage_signals(limit=5, only_would_execute=True)
        if recent:
            lines.append("")
            lines.append("Derniers signaux rentables :")
            for s in recent:
                lines.append(
                    f"• {s['token_symbol']}/{s['quote_symbol']} : {s['buy_dex']} → {s['sell_dex']} "
                    f"(net≈{s['net_profit_usd']:.2f}$)"
                )
        self.send("\n".join(lines))

    def cmd_status(self) -> None:
        stopped = is_stopped(self.db)
        stats = self.db.arbitrage_stats_summary()
        self.send(
            "ℹ️ <b>Statut</b>\n"
            f"Système : {'🛑 ARRÊTÉ (killswitch actif)' if stopped else '✅ actif'}\n"
            f"Signaux détectés : {stats.get('total', 0)}\n"
            f"Dont rentables : {stats.get('would_execute_count', 0)}"
        )

    def cmd_scan(self) -> None:
        if is_stopped(self.db):
            self.send("🛑 Killswitch actif : /start pour réactiver avant de scanner.")
            return
        self.send("🔍 Scan manuel lancé...")
        if self.scan_callback:
            self.scan_callback()
            self.send("✅ Scan terminé. Utilise /arbitrage pour voir les résultats.")
        else:
            self.send("⚠️ Aucun callback de scan configuré.")

    def cmd_stop(self) -> None:
        stop(self.db)
        self.send("🛑 Killswitch activé : plus aucune action automatique ne sera effectuée. "
                   "Envoie /start pour reprendre.")

    # --- boucle de polling ----------------------------------------------
    def run_forever(self, poll_timeout: int = 20) -> None:
        if not self.enabled:
            logger.info("Telegram désactivé (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID manquants).")
            return
        logger.info("Démarrage du polling Telegram.")
        while True:
            for update in self.get_updates(timeout=poll_timeout):
                self.handle_update(update)
            time.sleep(1)
