"""
routers/webhook.py — POST /webhook/telegram

Punto de entrada de todos los updates de Telegram.
Este router no conoce ningún detalle del formato de Telegram — trabaja
exclusivamente con ParsedUpdate e UserIntent. El parsing pertenece al adapter.

Flujo de despacho (por prioridad):
  1. WIZARD_CALLBACK       → budget_wizard (si hay PendingAction activo)
  2. WIZARD_TRIGGER        → arrancar / posponer / ignorar wizard
  3. CATEGORIZATION_CALLBACK → callback_handler (cat/confirm/skip)
  4. COMMAND               → chat agent en background
  5. EXPENSE_REPORT        → agente conversacional en tiempo real
"""

import asyncio
import logging
import os
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Request, Response, HTTPException

from adapters.rails_http import RailsHttpAdapter
from adapters.telegram_messenger import TelegramMessenger
from flows import budget_wizard, financial_context_wizard, income_wizard
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

    # 1a-pre. Callback del income wizard ─────────────────────────────────────
    if parsed.intent == UserIntent.INCOME_WIZARD_CALLBACK:
        pending = api.get_active_pending_action()
        if pending and pending.get("action_type") == "income_setup":
            income_wizard.handle_update(
                api=api,
                messenger=messenger,
                pending_action=pending,
                update_type="callback_query",
                payload=parsed.raw.get("callback_query", {}),
                callback_query_id=parsed.callback_query_id,
            )
        else:
            messenger.answer_callback(parsed.callback_query_id, "⚠️ Este flujo ya cerró.")
        return

    # 1a. Callback del financial context wizard ───────────────────────────────
    if parsed.intent == UserIntent.FC_WIZARD_CALLBACK:
        pending = api.get_active_pending_action()
        if pending and pending.get("action_type") == "financial_context_setup":
            messenger.answer_callback(parsed.callback_query_id, "")
            financial_context_wizard.handle_update(
                api=api,
                messenger=messenger,
                pending_action=pending,
                update_type="callback_query",
                payload=parsed.raw.get("callback_query", {}),
                callback_query_id=parsed.callback_query_id,
            )
        else:
            messenger.answer_callback(parsed.callback_query_id, "⚠️ Este flujo ya cerró.")
        return

    # 1b. Callback de step del budget wizard ──────────────────────────────────
    if parsed.intent == UserIntent.WIZARD_CALLBACK:
        pending = api.get_active_pending_action()
        if pending:
            budget_wizard.handle_update(
                api=api,
                messenger=messenger,
                pending_action=pending,
                update_type="callback_query",
                payload=parsed.raw["callback_query"],
                callback_query_id=parsed.callback_query_id,
            )
        else:
            # El wizard ya terminó pero el botón quedó en el chat
            messenger.answer_callback(parsed.callback_query_id, "⚠️ Este flujo ya cerró.")
        return

    # 2. Botones de trigger del wizard (start / tomorrow / skip) ─────────────
    if parsed.intent == UserIntent.WIZARD_TRIGGER:
        messenger.answer_callback(parsed.callback_query_id, "")
        _handle_wizard_trigger(api, messenger, parsed.callback_data or "")
        return

    # 3. Categorización de transacciones (cat / confirm / skip) ───────────────
    if parsed.intent == UserIntent.CATEGORIZATION_CALLBACK:
        messenger.answer_callback(parsed.callback_query_id, "✅")
        callback_handler.handle(api, messenger, parsed.callback_data or "")
        return

    # 3b. Respuesta rápida del chat con botones inline ───────────────────────
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

    # 4. Texto mientras hay wizard activo ─────────────────────────────────────
    if parsed.intent == UserIntent.EXPENSE_REPORT:
        pending = api.get_active_pending_action()
        if pending and pending.get("action_type") == "financial_context_setup":
            financial_context_wizard.handle_update(
                api=api,
                messenger=messenger,
                pending_action=pending,
                update_type="message",
                payload=parsed.raw.get("message", {}),
            )
            return

        if pending and pending.get("action_type") == "income_setup":
            income_wizard.handle_update(
                api=api,
                messenger=messenger,
                pending_action=pending,
                update_type="message",
                payload=parsed.raw.get("message", {}),
            )
            return

        if pending and pending.get("action_type") == "budget_planning":
            budget_wizard.handle_update(
                api=api,
                messenger=messenger,
                pending_action=pending,
                update_type="message",
                payload=parsed.raw.get("message", {}),
            )
            return

    # 4. Slash command → chat agent en background ──────────────────────────────
    if parsed.intent == UserIntent.COMMAND:
        # create_task: el webhook devuelve 200 inmediatamente
        asyncio.get_event_loop().run_in_executor(
            None,
            chat_agent.handle_command,
            api,
            messenger,
            parsed,
        )
        return

    # 5. Texto plano → agente conversacional en tiempo real ───────────────────
    logger.info("[webhook] realtime message recibido: %.60r", parsed.text)
    asyncio.get_event_loop().run_in_executor(
        None,
        chat_agent.handle_message,
        api,
        messenger,
        parsed,
    )


def _handle_wizard_trigger(api: RailsHttpAdapter, messenger: TelegramMessenger, data: str) -> None:
    """
    Maneja los botones del mensaje de trigger quincenal:
      wizard:start              → crear PendingAction e ir al step 1
      wizard:tomorrow           → crear PendingAction con expires_at=+24h
      wizard:skip               → no hacer nada
      wizard:open:{YYYY-MM}     → redirigir al planificador web
      wizard:snooze:{YYYY-MM}   → confirmar snooze; el agente nocturno vuelve a alertar mañana
    """
    if data == "wizard:start":
        action = api.create_pending_action(
            action_type="budget_planning",
            total_steps=8,
            context={},
        )
        budget_wizard._go_to_step(api, messenger, action["id"], {}, 1)

    elif data == "wizard:tomorrow":
        expires_at = (
            datetime.now(timezone.utc) + timedelta(hours=24)
        ).isoformat()
        api.create_pending_action(
            action_type="budget_planning",
            total_steps=8,
            context={},
            expires_at=expires_at,
        )
        messenger.send_message("Entendido 👍 Te mando el wizard mañana.")

    elif data == "wizard:skip":
        messenger.send_message("Ok, sin presupuesto este período. Cualquier cosa me avisás.")

    elif data.startswith("wizard:open:"):
        # wizard:open:{YYYY-MM} — el planning real ocurre en la app web
        yyyy_mm = data.removeprefix("wizard:open:")
        try:
            year, month = int(yyyy_mm[:4]), int(yyyy_mm[5:7])
            from agents.nightly import MESES_FULL
            mes_nombre = MESES_FULL[month - 1]
        except (ValueError, IndexError):
            mes_nombre = yyyy_mm
        messenger.send_message(
            f"Abrí el planificador en la app web para <b>{mes_nombre}</b>. "
            f"Ve a la sección Presupuesto para armarlo. 📱"
        )

    elif data.startswith("wizard:snooze:"):
        # wizard:snooze:{YYYY-MM} — snooze silencioso; el agente nightly alerta de nuevo mañana
        messenger.send_message("Perfecto, te recuerdo mañana. 👍")

    else:
        logger.warning("[webhook] wizard trigger desconocido: %s", data)
