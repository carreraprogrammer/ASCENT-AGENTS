"""
scheduler.py — APScheduler que reemplaza GitHub Actions + Railway cron.

Corre dentro del mismo proceso FastAPI.
Horarios en UTC (Colombia = UTC-5):
  - Nightly:  04:00 UTC = 11:00pm Colombia
  - Insight:  07:00 UTC = 02:00am Colombia
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

    # ── Insight diario — 2am Colombia (07:00 UTC) ─────────────────────────────
    scheduler.add_job(
        func=_run_insight_refresh_sync,
        trigger=CronTrigger(hour=7, minute=0),
        id="daily_insight",
        name="Insight diario Daniel 15K",
        replace_existing=True,
    )

    # ── Keep-alive Rails API (cada 5 min) — evita cold start en Railway ──────
    scheduler.add_job(
        func=_ping_rails,
        trigger=CronTrigger(minute="*/5"),
        id="rails_keepalive",
        name="Keep-alive Rails API",
        replace_existing=True,
    )

    return scheduler


async def _run_insight_refresh_sync() -> None:
    import asyncio
    from agents.insight import run_insight_refresh
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, run_insight_refresh)


async def _ping_rails() -> None:
    """Pings Rails /health every 5 min to prevent cold starts on Railway."""
    import httpx
    from adapters.rails_http import BASE_URL, build_auth_headers
    try:
        r = httpx.get(f"{BASE_URL}/health", headers=build_auth_headers(), timeout=10)
        if r.status_code >= 500:
            logger.warning("[scheduler] keep-alive Rails responded %s", r.status_code)
    except Exception as e:
        logger.warning("[scheduler] keep-alive Rails failed: %s", e)


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
