"""
adapters/telegram_messenger.py — Implementación del MessengerPort para Telegram.

Este adapter es el único lugar del Brain que sabe sobre el formato de Telegram:
  - callback_query vs message
  - prefijos "wz:", "wizard:", "cat:", "confirm:", "skip:"
  - slash commands (/)
  - entities de tipo bot_command

El resto del sistema trabaja con ParsedUpdate y UserIntent.
"""

import os
import logging
import httpx

from ports.messenger import MessengerPort, ParsedUpdate, UserIntent

logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = int(os.environ.get("TELEGRAM_CHAT_ID", "0"))
TG_BASE   = f"https://api.telegram.org/bot{BOT_TOKEN}"
TIMEOUT   = 8


class TelegramMessenger(MessengerPort):

    # ── Parsing ────────────────────────────────────────────────────────────────

    def parse_update(self, update: dict) -> ParsedUpdate:
        """
        Traduce un update de Telegram a un ParsedUpdate agnóstico.

        Prioridad de detección:
          1. callback_query con data "wz:*"     → WIZARD_CALLBACK
          2. callback_query con data "wizard:*" → WIZARD_TRIGGER
          3. callback_query (resto)             → CATEGORIZATION_CALLBACK
          4. message con text "/" al inicio     → COMMAND
          5. message (resto)                    → EXPENSE_REPORT
        """
        if cq := update.get("callback_query"):
            return self._parse_callback_query(cq, update)

        if msg := update.get("message"):
            return self._parse_message(msg, update)

        # Update de tipo desconocido (edited_message, inline, etc.)
        return ParsedUpdate(intent=UserIntent.EXPENSE_REPORT, raw=update)

    def _parse_callback_query(self, cq: dict, raw: dict) -> ParsedUpdate:
        data    = cq.get("data", "")
        cq_id   = cq.get("id", "")

        if data.startswith("wz_fc:"):
            return ParsedUpdate(
                intent=UserIntent.FC_WIZARD_CALLBACK,
                callback_query_id=cq_id,
                callback_data=data,
                raw=raw,
            )

        if data.startswith("wz:") or data.startswith("wz_"):
            return ParsedUpdate(
                intent=UserIntent.WIZARD_CALLBACK,
                callback_query_id=cq_id,
                callback_data=data,
                raw=raw,
            )

        if data.startswith("wizard:"):
            return ParsedUpdate(
                intent=UserIntent.WIZARD_TRIGGER,
                callback_query_id=cq_id,
                callback_data=data,          # "wizard:start" | "wizard:tomorrow" | "wizard:skip"
                raw=raw,
            )

        if data.startswith("chat:"):
            return ParsedUpdate(
                intent=UserIntent.CHAT_CALLBACK,
                callback_query_id=cq_id,
                callback_data=data,
                text=data.removeprefix("chat:"),
                raw=raw,
            )

        # cat:, confirm:, skip: — categorización de transacciones
        return ParsedUpdate(
            intent=UserIntent.CATEGORIZATION_CALLBACK,
            callback_query_id=cq_id,
            callback_data=data,
            raw=raw,
        )

    def _parse_message(self, msg: dict, raw: dict) -> ParsedUpdate:
        text = msg.get("text", "").strip()

        if text.startswith("/"):
            # Separar "/comando args opcionales"
            parts        = text[1:].split(None, 1)
            command      = parts[0].lower()
            command_args = parts[1] if len(parts) > 1 else None
            return ParsedUpdate(
                intent=UserIntent.COMMAND,
                command=command,
                command_args=command_args,
                text=text,
                raw=raw,
            )

        return ParsedUpdate(
            intent=UserIntent.EXPENSE_REPORT,
            text=text,
            raw=raw,
        )

    # ── Envío ──────────────────────────────────────────────────────────────────

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
            "chat_id":    CHAT_ID,
            "text":       text,
            "parse_mode": parse_mode,
        })

    def send_with_buttons(
        self,
        text: str,
        buttons: list[list[dict]],
        parse_mode: str = "HTML",
    ) -> None:
        keyboard = {
            "inline_keyboard": [
                [{"text": btn["text"], "callback_data": btn["callback_data"]} for btn in row]
                for row in buttons
            ]
        }
        self._post("sendMessage", {
            "chat_id":      CHAT_ID,
            "text":         text,
            "parse_mode":   parse_mode,
            "reply_markup": keyboard,
        })

    def answer_callback(
        self,
        callback_query_id: str,
        text: str,
        show_alert: bool = False,
    ) -> None:
        self._post("answerCallbackQuery", {
            "callback_query_id": callback_query_id,
            "text":              text,
            "show_alert":        show_alert,
        })
