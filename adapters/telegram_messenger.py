"""
adapters/telegram_messenger.py — Implementación del MessengerPort para Telegram.
"""

import os
import logging
import httpx

from ports.messenger import MessengerPort

logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = int(os.environ.get("TELEGRAM_CHAT_ID", "0"))
TG_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"
TIMEOUT = 8


class TelegramMessenger(MessengerPort):

    def _post(self, method: str, payload: dict) -> dict:
        url = f"{TG_BASE}/{method}"
        try:
            resp = httpx.post(url, json=payload, timeout=TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error("[TelegramMessenger] %s failed: %s", method, e)
            return {}

    def send_message(self, text: str, parse_mode: str = "HTML") -> None:
        self._post("sendMessage", {
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": parse_mode,
        })

    def send_with_buttons(
        self,
        text: str,
        buttons: list[list[dict]],
        parse_mode: str = "HTML",
    ) -> None:
        """
        buttons: [[{"text": "Sí", "callback_data": "resp:yes"}], ...]
        """
        keyboard = {
            "inline_keyboard": [
                [{"text": btn["text"], "callback_data": btn["callback_data"]} for btn in row]
                for row in buttons
            ]
        }
        self._post("sendMessage", {
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": parse_mode,
            "reply_markup": keyboard,
        })

    def answer_callback(self, callback_query_id: str, text: str, show_alert: bool = False) -> None:
        self._post("answerCallbackQuery", {
            "callback_query_id": callback_query_id,
            "text": text,
            "show_alert": show_alert,
        })
