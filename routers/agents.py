"""
routers/agents.py — Endpoints HTTP para disparar agentes manualmente.

Útil para:
  - Testing sin esperar el scheduler
  - GitHub Actions durante la transición
  - Debugging
"""

import logging

from fastapi import APIRouter, BackgroundTasks, Response

from adapters.rails_http import RailsHttpAdapter
from adapters.telegram_messenger import TelegramMessenger
from agents.nightly import run_nightly
from flows.budget_wizard import trigger_planning

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents")


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


@router.get("/health")
async def health() -> dict:
    """Health check de los agentes."""
    return {"ok": True, "agents": ["nightly", "planning"]}
