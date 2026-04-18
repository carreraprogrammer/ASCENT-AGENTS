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

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel

from adapters.rails_http import RailsHttpAdapter
from adapters.telegram_messenger import TelegramMessenger
from agents.nightly import run_nightly
from agents.web_chat import handle_web_chat
from flows.budget_wizard import trigger_planning

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


@router.post("/nightly")
async def trigger_nightly(background_tasks: BackgroundTasks) -> dict:
    """Dispara la revisión nocturna en background."""
    api = RailsHttpAdapter()
    messenger = TelegramMessenger()
    background_tasks.add_task(run_nightly, api, messenger)
    return {"ok": True, "message": "Revisión nocturna iniciada en background."}


@router.post("/planning")
async def trigger_planning_endpoint(background_tasks: BackgroundTasks) -> dict:
    """Dispara el wizard de planificación quincenal."""
    api = RailsHttpAdapter()
    messenger = TelegramMessenger()
    background_tasks.add_task(trigger_planning, api, messenger)
    return {"ok": True, "message": "Wizard de planificación iniciado."}


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


@router.get("/health")
async def health() -> dict:
    """Health check de los agentes."""
    return {"ok": True, "agents": ["nightly", "planning", "web_chat"]}
