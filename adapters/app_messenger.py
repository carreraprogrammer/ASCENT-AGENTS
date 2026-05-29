"""MessengerPort para la app: transporta respuestas del chat a agent_ui_events."""

from __future__ import annotations

import html
import re

from ports.messenger import MessengerPort, ParsedUpdate
from ports.rails_api import RailsApiPort


TAG_RE = re.compile(r"<[^>]+>")


def _to_ui_text(text: str) -> str:
    text = text or ""
    text = re.sub(r"<\s*b\s*>(.*?)<\s*/\s*b\s*>", r"**\1**", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<\s*i\s*>(.*?)<\s*/\s*i\s*>", r"*\1*", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<\s*br\s*/?\s*>", "\n", text, flags=re.IGNORECASE)
    text = TAG_RE.sub("", text)
    return html.unescape(text).strip()


class AppMessenger(MessengerPort):
    def __init__(self, api: RailsApiPort, session_id: str) -> None:
        self.api = api
        self.session_id = session_id

    def parse_update(self, update: dict) -> ParsedUpdate:
        return ParsedUpdate(intent="", text=str(update.get("text") or ""), raw=update)

    def send_message(self, text: str, parse_mode: str = "HTML") -> None:
        body = _to_ui_text(text)
        self.api.create_agent_ui_event(
            "show_card",
            {
                "title": "Daniel 15K",
                "body": body or "Listo.",
                "tone": "info",
            },
            session_id=self.session_id,
        )

    def send_with_buttons(
        self,
        text: str,
        buttons: list[list[dict]],
        parse_mode: str = "HTML",
    ) -> None:
        quick_replies = [
            {
                "text": str(button.get("text") or "").strip(),
                "callback_data": str(button.get("callback_data") or "").strip(),
            }
            for row in buttons
            for button in row
            if str(button.get("text") or "").strip() and str(button.get("callback_data") or "").strip()
        ]
        body = _to_ui_text(text)

        self.api.create_agent_ui_event(
            "show_quick_replies",
            {
                "title": "Daniel 15K",
                "body": body or "Necesito una aclaración.",
                "buttons": quick_replies,
            },
            session_id=self.session_id,
        )

    def answer_callback(
        self,
        callback_query_id: str,
        text: str,
        show_alert: bool = False,
    ) -> None:
        return None

    def notify_data_changed(self) -> None:
        self.api.create_agent_ui_event("data_changed", {}, session_id=self.session_id)
