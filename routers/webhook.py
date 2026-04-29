"""
routers/webhook.py — POST /webhook/telegram

Punto de entrada de todos los updates de Telegram.
Este router no conoce ningún detalle del formato de Telegram — trabaja
exclusivamente con ParsedUpdate e UserIntent. El parsing pertenece al adapter.

Flujo de despacho (por prioridad):
  1. CATEGORIZATION_CALLBACK → callback_handler (cat/confirm/skip)
  2. CHAT_CALLBACK            → chat agent (respuestas rápidas con botones)
  3. COMMAND                  → chat agent en background
  4. EXPENSE_REPORT           → agente conversacional en tiempo real
"""

import asyncio
import logging
import os

from fastapi import APIRouter, Request, Response, HTTPException

from adapters.rails_http import RailsHttpAdapter
from adapters.telegram_messenger import TelegramMessenger
from services import callback_handler
from agents import chat as chat_agent
from ports.messenger import UserIntent

logger = logging.getLogger(__name__)

router = APIRouter()

AUTHORIZED_CHAT_ID = int(os.environ.get("TELEGRAM_CHAT_ID", "0"))


def _get_chat_id(update: dict) -> int | None:
    if cq := update.get("callback_query"):
        return cq.get("from", {}).get("id")
    if msg := update.get("message"):
        return msg.get("chat", {}).get("id")
    return None


@router.post("/webhook/telegram")
async def telegram_webhook(request: Request) -> Response:
    try:
        update = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    chat_id = _get_chat_id(update)
    if not chat_id or chat_id != AUTHORIZED_CHAT_ID:
        return Response(status_code=200)

    messenger = TelegramMessenger()
    api       = RailsHttpAdapter()

    try:
        parsed = messenger.parse_update(update)
        await _dispatch(api, messenger, parsed)
    except Exception as e:
        logger.error("[webhook] dispatch error: %s", e, exc_info=True)

    # Siempre 200 — Telegram no debe reintentar
    return Response(status_code=200)


async def _dispatch(api: RailsHttpAdapter, messenger: TelegramMessenger, parsed) -> None:

    # 1. Categorización de transacciones (cat / confirm / skip) ───────────────
    if parsed.intent == UserIntent.CATEGORIZATION_CALLBACK:
        messenger.answer_callback(parsed.callback_query_id, "✅")
        callback_handler.handle(api, messenger, parsed.callback_data or "")
        return

    # 2. Respuesta rápida del chat con botones inline ─────────────────────────
    if parsed.intent == UserIntent.CHAT_CALLBACK:
        messenger.answer_callback(parsed.callback_query_id, "")
        asyncio.get_event_loop().run_in_executor(
            None,
            chat_agent.handle_message,
            api,
            messenger,
            parsed,
        )
        return

    # 3. Slash command → chat agent en background ─────────────────────────────
    if parsed.intent == UserIntent.COMMAND:
        asyncio.get_event_loop().run_in_executor(
            None,
            chat_agent.handle_command,
            api,
            messenger,
            parsed,
        )
        return

    # 4. Texto plano → agente conversacional en tiempo real ───────────────────
    logger.info("[webhook] realtime message recibido: %.60r", parsed.text)
    asyncio.get_event_loop().run_in_executor(
        None,
        chat_agent.handle_message,
        api,
        messenger,
        parsed,
    )
