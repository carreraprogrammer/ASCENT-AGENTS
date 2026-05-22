"""Canal app -> mismo agente conversacional usado por Telegram."""

from __future__ import annotations

import logging

from adapters.app_messenger import AppMessenger
from ports.messenger import ParsedUpdate, UserIntent
from ports.rails_api import RailsApiPort
from agents import chat as chat_agent
from services import callback_handler

logger = logging.getLogger(__name__)


def _event_response_text(event_response: dict | None) -> str:
    if not event_response:
        return ""

    event_type = event_response.get("type", "unknown")
    data = event_response.get("data") or {}

    if event_type == "confirmed":
        return "Confirmo la acción propuesta."
    if event_type == "dismissed":
        return "Cancelo la acción propuesta."
    if event_type == "form_submitted":
        return f"Envié el formulario con estos datos: {data}"

    return f"Respondí al evento con tipo={event_type} y datos={data}"


def handle_app_chat(
    api: RailsApiPort,
    session_id: str,
    message: str | None = None,
    event_response: dict | None = None,
    prior_messages: list[dict] | None = None,
) -> None:
    messenger = AppMessenger(api, session_id)

    if event_response and event_response.get("type") == "callback":
        data = ((event_response.get("data") or {}).get("callback_data") or "").strip()
        if not data:
            logger.warning("[app_chat] session %s: callback without callback_data", session_id)
            return

        if data.startswith("chat:"):
            parsed = ParsedUpdate(
                intent=UserIntent.CHAT_CALLBACK,
                text=data.removeprefix("chat:"),
                callback_data=data,
                raw={"source": "app", "session_id": session_id},
            )
            chat_agent.handle_app_message(api, messenger, parsed, prior_messages=prior_messages)
            return

        callback_handler.handle(api, messenger, data)
        return

    text = (message or "").strip() or _event_response_text(event_response)
    if not text.strip():
        logger.warning("[app_chat] session %s: no message nor event_response - skipping", session_id)
        return

    parsed = ParsedUpdate(
        intent=UserIntent.EXPENSE_REPORT,
        text=text,
        raw={"source": "app", "session_id": session_id},
    )
    chat_agent.handle_app_message(api, messenger, parsed, prior_messages=prior_messages)
