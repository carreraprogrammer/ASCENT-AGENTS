"""
routers/webhook.py — POST /webhook/telegram

Punto de entrada de todos los updates de Telegram.
Lógica:
  1. Verificar que el update viene del chat autorizado
  2. Si hay PendingAction activo → delegar al wizard
  3. Si es callback_query conocido (cat/confirm/skip) → procesar inline
  4. Si es mensaje de texto → almacenar para el agente nocturno
"""

import logging
import os

from fastapi import APIRouter, Request, Response, HTTPException

from adapters.rails_http import RailsHttpAdapter
from adapters.telegram_messenger import TelegramMessenger
from flows import budget_wizard
from services import callback_handler

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
        # Responder 200 para que Telegram no reintente
        return Response(status_code=200)

    api = RailsHttpAdapter()
    messenger = TelegramMessenger()

    try:
        await _dispatch(api, messenger, update)
    except Exception as e:
        logger.error("[webhook] dispatch error: %s", e, exc_info=True)

    # Siempre 200 — Telegram no debe reintentar
    return Response(status_code=200)


async def _dispatch(api: RailsHttpAdapter, messenger: TelegramMessenger, update: dict) -> None:
    # ── ¿Hay un flujo conversacional abierto? ────────────────────────────────
    pending = api.get_active_pending_action()

    if update.get("callback_query"):
        cq = update["callback_query"]
        cq_id = cq["id"]
        data = cq.get("data", "")

        if pending and data.startswith("wz"):
            # Callback del wizard → delegamos
            budget_wizard.handle_update(
                api=api,
                messenger=messenger,
                pending_action=pending,
                update_type="callback_query",
                payload=cq,
                callback_query_id=cq_id,
            )
        elif data.startswith("wizard:") and not pending:
            # Trigger inicial del wizard (botones del planning scheduler)
            if data == "wizard:start":
                # Crear PendingAction y arrancar step 1
                action = api.create_pending_action(
                    action_type="budget_planning",
                    total_steps=8,
                    context={},
                )
                messenger.answer_callback(cq_id, "✅")
                budget_wizard._go_to_step(api, messenger, action["id"], {}, 1)
            elif data == "wizard:tomorrow":
                from datetime import datetime, timezone, timedelta
                expires_at = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
                action = api.create_pending_action(
                    action_type="budget_planning",
                    total_steps=8,
                    context={},
                    expires_at=expires_at,
                )
                messenger.answer_callback(cq_id, "👍")
                messenger.send_message("Entendido 👍 Te mando el wizard mañana.")
            elif data == "wizard:skip":
                messenger.answer_callback(cq_id, "Ok")
                messenger.send_message("Ok, sin presupuesto este período.")
        else:
            # Callback normal de categorización / confirmación
            messenger.answer_callback(cq_id, "✅")
            callback_handler.handle(api, messenger, data)

    elif update.get("message"):
        msg = update["message"]

        if pending:
            # Mensaje durante un flujo activo → delegamos al wizard
            budget_wizard.handle_update(
                api=api,
                messenger=messenger,
                pending_action=pending,
                update_type="message",
                payload=msg,
            )
        else:
            # Mensaje normal → se almacena en DB para el agente nocturno
            # (Rails TelegramController ya lo guarda via su propio webhook
            #  mientras la migración está en curso; en la versión final
            #  el Brain tiene su propia tabla de mensajes)
            logger.info("[webhook] mensaje recibido, sin flujo activo — guardado para agente nocturno")
