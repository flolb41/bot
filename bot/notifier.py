"""Notifications Telegram via l'API HTTP directe (évite la dépendance lourde python-telegram-bot)."""
from __future__ import annotations

import logging

import requests


class TelegramNotifier:
    def __init__(self, enabled: bool, bot_token: str = "", chat_id: str = ""):
        self.enabled = enabled and bool(bot_token) and bool(chat_id)
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.logger = logging.getLogger("bot.notifier")

    def send(self, message: str) -> None:
        if not self.enabled:
            return
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        try:
            requests.post(url, data={"chat_id": self.chat_id, "text": message}, timeout=10)
        except requests.RequestException as exc:
            self.logger.warning("Échec de l'envoi Telegram: %s", exc)
