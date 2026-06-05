"""
routers/agents.py — Endpoints HTTP para disparar agentes manualmente.

Útil para:
  - Testing sin esperar el scheduler
  - GitHub Actions durante la transición
  - Web chat (canal web → agente)
  - Debugging
"""

import logging
import os
from datetime import datetime, timezone, timedelta
from agents.nightly import COLOMBIA_TZ

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel

from adapters.rails_http import RailsHttpAdapter
from adapters.telegram_messenger import TelegramMessenger
from agents.app_chat import handle_app_chat
from agents.gmail_push import run_gmail_push
from agents.nightly import run_nightly
from agents.web_chat import handle_web_chat
from agents.insight import run_insight_refresh

# Rate limit: max once every 6 hours per account (simple in-memory guard)
_insight_last_run: dict[str, datetime] = {}
INSIGHT_COOLDOWN_HOURS = 6

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents")

SERVICE_TOKEN = os.environ.get("DANIEL15K_SERVICE_TOKEN", "")


def _verify_service_token(request: Request) -> None:
    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if not SERVICE_TOKEN or token != SERVICE_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")


class WebChatRequest(BaseModel):
    account_id: int
    session_id: str
    message: str | None = None
    event_response: dict | None = None
    budget_context: dict | None = None
    prior_messages: list[dict] | None = None
    skip_history: bool = False


@router.post("/nightly")
async def trigger_nightly(background_tasks: BackgroundTasks) -> dict:
    """Dispara la revisión nocturna en background."""
    from adapters.app_messenger import AppMessenger
    api = RailsHttpAdapter()
    background_tasks.add_task(run_nightly, api, AppMessenger(api, session_id="nightly"))
    return {"ok": True, "message": "Revisión nocturna iniciada en background."}


@router.post("/nightly/recovery")
async def trigger_nightly_recovery(background_tasks: BackgroundTasks, date: str) -> dict:
    """Dispara la revisión nocturna para una fecha pasada (formato: YYYY-MM-DD)."""
    from adapters.app_messenger import AppMessenger
    from functools import partial
    try:
        target = datetime.strptime(date, "%Y-%m-%d").replace(
            tzinfo=COLOMBIA_TZ, hour=23, minute=0
        )
    except ValueError:
        return {"ok": False, "error": "Formato de fecha inválido. Usar YYYY-MM-DD."}

    admin_api = RailsHttpAdapter()
    try:
        accounts = admin_api.get_active_accounts()
    except Exception as e:
        logger.error("[recovery] No se pudo obtener cuentas: %s", e)
        return {"ok": False, "error": "No se pudo obtener la lista de cuentas."}

    for account in accounts:
        account_id = str(account["id"])
        api = RailsHttpAdapter(account_id=account_id)
        messenger = AppMessenger(api, session_id="nightly")

        gmail_token = None
        bank_senders = None
        try:
            token_data = api.get_gmail_token()
            if token_data:
                gmail_token = token_data.get("access_token")
                bank_senders = token_data.get("bank_senders") or None
        except Exception as gmail_err:
            logger.warning("[recovery] account_id=%s — Gmail token error: %s", account_id, gmail_err)

        task = partial(run_nightly, api, messenger, target, gmail_token=gmail_token, bank_senders=bank_senders)
        background_tasks.add_task(task)

    return {"ok": True, "message": f"Revisión nocturna de {date} iniciada para {len(accounts)} cuenta(s)."}


@router.post("/planning")
async def trigger_planning_endpoint() -> dict:
    """Emite evento open_wizard para iniciar planificación desde la app."""
    api = RailsHttpAdapter()
    messenger = TelegramMessenger()
    api.create_agent_ui_event("open_wizard", {"wizard": "budget_planning"})
    messenger.send_message("Es momento de armar el presupuesto mensual. Abrí la app para comenzar 📱")
    return {"ok": True, "message": "Evento open_wizard emitido."}


@router.post("/web_chat", dependencies=[Depends(_verify_service_token)])
async def web_chat(body: WebChatRequest, background_tasks: BackgroundTasks) -> dict:
    """Canal web → agente. Llamado por Rails WebChatJob."""
    background_tasks.add_task(
        handle_web_chat,
        account_id=body.account_id,
        session_id=body.session_id,
        message=body.message,
        event_response=body.event_response,
        budget_context=body.budget_context,
    )
    return {"ok": True, "session_id": body.session_id}


@router.post("/app_chat", dependencies=[Depends(_verify_service_token)])
async def app_chat(body: WebChatRequest, background_tasks: BackgroundTasks) -> dict:
    """Canal app → mismo agente conversacional de Telegram, con messenger visual."""
    api = RailsHttpAdapter(account_id=str(body.account_id))
    background_tasks.add_task(
        handle_app_chat,
        api=api,
        session_id=body.session_id,
        message=body.message,
        event_response=body.event_response,
        prior_messages=body.prior_messages,
        skip_history=body.skip_history,
    )
    return {"ok": True, "session_id": body.session_id}


class InsightRequest(BaseModel):
    account_id: str | int | None = None


@router.post("/insight", dependencies=[Depends(_verify_service_token)])
async def trigger_insight(body: InsightRequest, background_tasks: BackgroundTasks) -> dict:
    """Dispara la generación de insight on-demand (max 1 vez cada 6h por cuenta)."""
    account_key = str(body.account_id or "default")
    now = datetime.now(timezone.utc)
    last = _insight_last_run.get(account_key)

    if last and (now - last) < timedelta(hours=INSIGHT_COOLDOWN_HOURS):
        remaining = INSIGHT_COOLDOWN_HOURS - int((now - last).total_seconds() / 3600)
        return {
            "ok": False,
            "rate_limited": True,
            "message": f"Análisis ejecutado recientemente. Podés volver a intentarlo en ~{remaining}h.",
        }

    _insight_last_run[account_key] = now
    background_tasks.add_task(run_insight_refresh, trigger="manual")
    return {"ok": True, "message": "Generando nuevo análisis en background."}


class GmailPushRequest(BaseModel):
    account_id: int
    history_id: str


@router.post("/gmail-push", dependencies=[Depends(_verify_service_token)])
async def gmail_push(body: GmailPushRequest, background_tasks: BackgroundTasks) -> dict:
    """
    Recibe notificación push de Gmail vía Rails (que recibió el webhook de Pub/Sub).
    Procesa los mensajes nuevos desde history_id en background.
    """
    background_tasks.add_task(run_gmail_push, account_id=body.account_id, history_id=body.history_id)
    return {"ok": True, "account_id": body.account_id}


@router.get("/health")
async def health() -> dict:
    """Health check de los agentes."""
    return {"ok": True, "agents": ["nightly", "planning", "web_chat", "insight", "gmail_push"]}
