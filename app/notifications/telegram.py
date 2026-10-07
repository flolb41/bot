"""Notifications et commandes Telegram (section 9 du TODO).

Utilise l'API HTTP Telegram directement (pas de dépendance lourde), adapté
aux ressources limitées du Raspberry Pi 3. Toutes les commandes sont en
LECTURE SEULE sauf /stop et /start qui pilotent le killswitch global.
"""
from __future__ import annotations

import logging
import time
from typing import Callable

import httpx

from app.database import Database
from app.killswitch import is_stopped, resume, stop
from app.scoring import score_from_project_row
from app.trackers.deadlines import upcoming_deadlines
from app.trackers.rewards import rewards_summary
from app.wallet.balances import get_all_balances

logger = logging.getLogger("app.telegram")

API_BASE = "https://api.telegram.org/bot{token}"


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

    def send(self, text: str, chat_id: str | None = None) -> None:
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
            "projects": self.cmd_projects,
            "top": self.cmd_top,
            "today": self.cmd_today,
            "deadlines": self.cmd_deadlines,
            "rewards": self.cmd_rewards,
            "balances": self.cmd_balances,
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
            self.send("Commande inconnue. Commandes disponibles : /start /projects /top /today "
                       "/deadlines /rewards /balances /status /scan /stop")

    def cmd_start(self) -> None:
        self.send(
            "🤖 <b>Crypto Reward Hunter</b>\n"
            "Capital à risque : 0 €. Surveillance d'airdrops/testnets/quêtes.\n\n"
            "Commandes : /projects /top /today /deadlines /rewards /balances /status /scan /stop"
        )
        resume(self.db)

    def cmd_projects(self) -> None:
        projects = self.db.list_projects()
        if not projects:
            self.send("Aucun projet suivi pour le moment.")
            return
        lines = [f"{p['score']:>3}/100  {p['name']} ({p['status']})" for p in projects]
        self.send("📋 <b>Projets suivis</b>\n" + "\n".join(lines))

    def cmd_top(self, limit: int = 5) -> None:
        projects = self.db.list_projects(order_by="score DESC")[:limit]
        if not projects:
            self.send("Aucun projet suivi pour le moment.")
            return
        emojis = {"PRIORITÉ CRITIQUE": "🔴", "PRIORITÉ HAUTE": "🟠", "À SURVEILLER": "🟡"}
        lines = []
        for p in projects:
            breakdown = score_from_project_row(p)
            label = breakdown.classify().value
            emoji = emojis.get(label, "⚪")
            lines.append(f"{emoji} {p['name']:<20} {p['score']:>3}/100  — {label}")
        self.send("🏆 <b>Top opportunités</b>\n" + "\n".join(lines))

    def cmd_today(self) -> None:
        projects = sorted(self.db.list_projects(), key=lambda p: p["score"], reverse=True)
        if not projects:
            self.send("Aucun projet suivi pour le moment.")
            return
        top = projects[0]
        tasks = self.db.list_tasks(project_id=top["id"], status="pending")[:5]
        lines = [
            "🔥 <b>OPPORTUNITÉ DU JOUR</b>",
            "",
            top["name"],
            f"Score : {top['score']}/100",
            "",
            f"Coût : {top.get('estimated_cost', 0)} €",
            f"Deadline : {top.get('deadline') or 'non définie'}",
            "",
            "Actions :",
        ]
        lines += [f"{i}. {t['title']}" for i, t in enumerate(tasks, start=1)] or ["(aucune tâche enregistrée)"]
        self.send("\n".join(lines))

    def cmd_deadlines(self) -> None:
        items = upcoming_deadlines(self.db)
        if not items:
            self.send("Aucune échéance proche (fenêtre 48h).")
            return
        lines = []
        for item in items:
            flag = "⛔ EXPIRÉ" if item["expired"] else "⏰"
            lines.append(f"{flag} [{item['scope']}] {item['name']} — {item['deadline']}")
        self.send("📅 <b>Échéances proches</b>\n" + "\n".join(lines))

    def cmd_rewards(self) -> None:
        summary = rewards_summary(self.db)
        lines = [
            "💰 <b>Rewards</b>",
            f"Reçus : {summary['nb_received']}  |  En attente : {summary['nb_pending']}  "
            f"|  Convertis : {summary['nb_converted']}",
            f"Gas dépensé (cumulé) : {summary['total_gas_spent']:.4f}",
            "",
        ]
        for token, amount in summary["by_token"].items():
            lines.append(f"  {token}: {amount}")
        self.send("\n".join(lines))

    def cmd_balances(self) -> None:
        wallets = self.db.list_wallets()
        if not wallets:
            self.send("Aucun wallet configuré. Ajoute une adresse (publique) via la config.")
            return
        balances = get_all_balances(wallets)
        lines = ["👛 <b>Balances (lecture seule)</b>"]
        for w in balances:
            status = w["balance"] if w.get("error") is None else f"erreur: {w['error']}"
            lines.append(f"{w['name']} ({w['network']}): {status}")
        self.send("\n".join(lines))

    def cmd_status(self) -> None:
        stopped = is_stopped(self.db)
        nb_projects = len(self.db.list_projects())
        nb_tasks_pending = len(self.db.list_tasks(status="pending"))
        self.send(
            "ℹ️ <b>Statut</b>\n"
            f"Système : {'🛑 ARRÊTÉ (killswitch actif)' if stopped else '✅ actif'}\n"
            f"Projets suivis : {nb_projects}\n"
            f"Tâches en attente : {nb_tasks_pending}"
        )

    def cmd_scan(self) -> None:
        if is_stopped(self.db):
            self.send("🛑 Killswitch actif : /start pour réactiver avant de scanner.")
            return
        self.send("🔍 Scan manuel lancé...")
        if self.scan_callback:
            self.scan_callback()
            self.send("✅ Scan terminé. Utilise /projects ou /top pour voir les résultats.")
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
