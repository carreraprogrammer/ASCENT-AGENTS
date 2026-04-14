"""
scheduler.py — APScheduler que reemplaza GitHub Actions + Railway cron.

Corre dentro del mismo proceso FastAPI.
Horarios en UTC (Colombia = UTC-5):
  - Nightly:   04:00 UTC = 11:00pm Colombia
  - Planning:  13:00 UTC = 08:00am Colombia, días 1 y 15 de cada mes
"""

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


def _make_scheduler() -> AsyncIOScheduler:
    from adapters.rails_http import RailsHttpAdapter
    from adapters.telegram_messenger import TelegramMessenger
    from agents.nightly import run_nightly
    from flows.budget_wizard import trigger_planning

    scheduler = AsyncIOScheduler(timezone="UTC")

    # ── Revisión nocturna — 11pm Colombia (04:00 UTC) ────────────────────────
    scheduler.add_job(
        func=run_nightly,
        trigger=CronTrigger(hour=4, minute=0),
        id="nightly_review",
        name="Revisión nocturna Daniel 15K",
        replace_existing=True,
        kwargs={
            "api":       RailsHttpAdapter(),
            "messenger": TelegramMessenger(),
        },
    )

    # ── Planificación quincenal — 8am Colombia (13:00 UTC) día 1 ────────────
    scheduler.add_job(
        func=trigger_planning,
        trigger=CronTrigger(day=1, hour=13, minute=0),
        id="planning_day1",
        name="Planificación quincenal — Día 1",
        replace_existing=True,
        kwargs={
            "api":       RailsHttpAdapter(),
            "messenger": TelegramMessenger(),
        },
    )

    # ── Planificación quincenal — 8am Colombia (13:00 UTC) día 15 ───────────
    scheduler.add_job(
        func=trigger_planning,
        trigger=CronTrigger(day=15, hour=13, minute=0),
        id="planning_day15",
        name="Planificación quincenal — Día 15",
        replace_existing=True,
        kwargs={
            "api":       RailsHttpAdapter(),
            "messenger": TelegramMessenger(),
        },
    )

    # ── Expirar PendingActions sin respuesta (cada hora) ─────────────────────
    scheduler.add_job(
        func=_expire_pending_actions,
        trigger=CronTrigger(minute=0),
        id="expire_pending_actions",
        name="Expirar PendingActions vencidos",
        replace_existing=True,
    )

    return scheduler


async def _expire_pending_actions() -> None:
    """Marca como 'expired' los PendingActions que pasaron su expires_at."""
    from datetime import datetime, timezone
    from adapters.rails_http import RailsHttpAdapter
    import httpx

    api = RailsHttpAdapter()
    try:
        pending = api.get_active_pending_action()
        if not pending:
            return
        expires_at = pending.get("expires_at")
        if not expires_at:
            return
        from datetime import datetime, timezone
        expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        if datetime.now(timezone.utc) > expiry:
            api.update_pending_action(pending["id"], status="expired")
            logger.info("[scheduler] PendingAction %s expirado.", pending["id"])
    except Exception as e:
        logger.error("[scheduler] error expirando PendingActions: %s", e)


def start() -> AsyncIOScheduler:
    global _scheduler
    _scheduler = _make_scheduler()
    _scheduler.start()
    logger.info("[scheduler] APScheduler iniciado.")
    return _scheduler


def stop() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("[scheduler] APScheduler detenido.")
