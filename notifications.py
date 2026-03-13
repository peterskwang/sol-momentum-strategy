"""
notifications.py — Telegram Alert Notifier

Sends strategy alerts via Telegram Bot API.
Reads TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from environment.
"""

import os
import logging
import requests

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """Simple Telegram notifier using direct Bot API (no library dependency)."""

    def __init__(self, bot_token: str = "", chat_id: str = ""):
        self.bot_token = bot_token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID", "")
        self._enabled = bool(self.bot_token and self.chat_id)

        if not self._enabled:
            logger.info("Telegram notifier disabled (TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set)")

    def send(self, message: str) -> bool:
        """
        Send a message via Telegram Bot API.

        Args:
            message: text to send (Markdown supported)

        Returns:
            True if sent successfully, False otherwise
        """
        if not self._enabled:
            logger.debug("Telegram disabled — suppressing message: %s", message[:80])
            return False

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": message,
            "parse_mode": "HTML",
        }

        try:
            resp = requests.post(url, json=payload, timeout=10)
            resp.raise_for_status()
            return True
        except requests.RequestException as exc:
            logger.warning("Failed to send Telegram alert: %s", exc)
            return False

    def __call__(self, message: str) -> bool:
        """Allow using notifier as a callable."""
        return self.send(message)


def create_notifier(config: dict = None) -> TelegramNotifier:
    """
    Create a TelegramNotifier from config dict or environment variables.

    Args:
        config: optional config dict with 'telegram_bot_token' and 'telegram_chat_id'

    Returns:
        TelegramNotifier instance
    """
    if config:
        return TelegramNotifier(
            bot_token=config.get("telegram_bot_token", ""),
            chat_id=config.get("telegram_chat_id", ""),
        )
    return TelegramNotifier()
