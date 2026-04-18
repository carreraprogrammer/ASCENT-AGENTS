"""Handler del agente conversacional para el canal web (PWA)."""

from __future__ import annotations

import logging
from datetime import datetime

from adapters.rails_http import RailsHttpAdapter
from ports.messenger import NullMessenger
from services.chat_context import COLOMBIA_TZ
from services.chat_prompts import CHAT_MODEL, WEB_SYSTEM_PROMPT
from services.chat_tools import build_tool_map, build_tools
from services.claude_client import run_agent

logger = logging.getLogger(__name__)

# Herramientas que no tienen sentido en el canal web
_WEB_EXCLUDED_TOOLS = {"send_telegram", "trigger_financial_context_wizard"}


def _web_tools() -> list[dict]:
    return [t for t in build_tools() if t["name"] not in _WEB_EXCLUDED_TOOLS]


def _build_initial_message(message: str | None, event_response: dict | None) -> str:
    parts = [
        "El usuario está interactuando desde la aplicación web.",
        "Respondé usando herramientas visuales (emit_ui_event, navigate_to). No uses send_telegram.",
    ]

    if event_response:
        event_type = event_response.get("type", "unknown")
        event_id = event_response.get("event_id")
        data = event_response.get("data") or {}

        if event_type == "form_submitted":
            parts.append(f"El usuario envió un formulario (event_id={event_id}) con los datos: {data}")
        elif event_type == "confirmed":
            parts.append(f"El usuario confirmó la acción (event_id={event_id}). Guardá y cerrá el flujo.")
        elif event_type == "dismissed":
            parts.append(f"El usuario canceló la acción (event_id={event_id}).")
        else:
            parts.append(f"Respuesta del usuario al evento {event_id}: tipo={event_type}, datos={data}")

    if message:
        parts.append(f"Mensaje del usuario: {message}")

    return "\n".join(parts)


def handle_web_chat(
    account_id: int,
    session_id: str,
    message: str | None = None,
    event_response: dict | None = None,
) -> None:
    if not message and not event_response:
        logger.warning("[web_chat] session %s: no message nor event_response — skipping", session_id)
        return

    api = RailsHttpAdapter()
    messenger = NullMessenger()
    now_col = datetime.now(COLOMBIA_TZ)
    state = {"responded": False, "mutated": False, "source_event_id": None, "session_id": session_id}

    tool_map = build_tool_map(api, messenger, now_col, state)

    initial_message = _build_initial_message(message, event_response)

    try:
        run_agent(
            system_prompt=WEB_SYSTEM_PROMPT,
            tools=_web_tools(),
            tool_map=tool_map,
            initial_message=initial_message,
            max_iterations=12,
            model=CHAT_MODEL,
        )
        logger.info("[web_chat] session %s completed", session_id)
    except Exception as exc:
        logger.error("[web_chat] session %s failed: %s", session_id, exc, exc_info=True)
        try:
            tool_map["emit_ui_event"]({
                "event_type": "show_card",
                "payload": {
                    "title": "Algo salió mal",
                    "body": "No pude procesar tu solicitud. Intentá de nuevo.",
                    "tone": "warning",
                },
            })
        except Exception:
            pass
