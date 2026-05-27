"""
scheduler.py — APScheduler que reemplaza GitHub Actions + Railway cron.

Corre dentro del mismo proceso FastAPI.
Horarios en UTC (Colombia = UTC-5):
  - Nightly:  04:00 UTC = 11:00pm Colombia
  - Insight:  07:00 UTC = 02:00am Colombia
"""

import logging
import asyncio
import os

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.events import EVENT_JOB_ERROR

# Fase 0.6: Gmail OAuth por cuenta — el token viene de GET /api/v1/me/email_connection/token.
# Las credenciales IMAP globales (GMAIL_ADDRESS / GMAIL_APP_PASSWORD) ya no se usan.

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


def _make_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="UTC")

    # ── Revisión nocturna — 11pm Colombia (04:00 UTC) ────────────────────────
    scheduler.add_job(
        func=_run_nightly_all_accounts,
        trigger=CronTrigger(hour=4, minute=0),
        id="nightly_review",
        name="Revisión nocturna Daniel 15K",
        replace_existing=True,
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


async def _run_nightly_all_accounts() -> None:
    """
    Itera sobre todas las accounts activas y ejecuta la revisión nocturna para cada una.
    Cada cuenta corre de forma aislada: si una falla, las demás continúan.
    """
    from adapters.rails_http import RailsHttpAdapter
    from adapters.app_messenger import AppMessenger
    from agents.nightly import run_nightly

    # Usamos el adaptador por defecto (sin account_id) para obtener la lista de cuentas.
    # El endpoint /agent/accounts/active solo requiere el service token.
    admin_api = RailsHttpAdapter()

    try:
        accounts = admin_api.get_active_accounts()
    except Exception as e:
        logger.error("[nightly] No se pudo obtener la lista de cuentas: %s", e)
        return

    logger.info("[nightly] Iniciando revisión nocturna para %d cuenta(s).", len(accounts))

    loop = asyncio.get_event_loop()

    for account in accounts:
        account_id   = str(account["id"])
        account_name = account.get("name", f"account_{account_id}")

        logger.info("[nightly] account_id=%s (%s) — iniciando.", account_id, account_name)
        try:
            api       = RailsHttpAdapter(account_id=account_id)
            messenger = AppMessenger(api, session_id="nightly")

            # Fase 0.6: obtener token Gmail OAuth desde la API.
            # Si la cuenta no tiene Gmail conectado → None → el agente omite el análisis de correos.
            gmail_token = None
            try:
                token_data = api.get_gmail_token()
                if token_data:
                    gmail_token = token_data.get("access_token")
                    logger.info("[nightly] account_id=%s — Gmail token OK.", account_id)
                else:
                    logger.info("[nightly] account_id=%s — sin Gmail conectado, omitiendo correos.", account_id)
            except Exception as gmail_err:
                logger.warning("[nightly] account_id=%s — no se pudo obtener Gmail token: %s", account_id, gmail_err)

            from functools import partial
            task = partial(run_nightly, api, messenger, gmail_token=gmail_token)
            await loop.run_in_executor(None, task)
            logger.info("[nightly] account_id=%s — completado OK.", account_id)

        except Exception as e:
            logger.error(
                "[nightly] account_id=%s (%s) — error: %s",
                account_id, account_name, e,
                exc_info=True,
            )
            try:
                from services.python_error_notifier import capture
                capture(e, context=f"nightly:account_{account_id}")
            except Exception as notify_err:
                logger.error("[nightly] error notifier failed: %s", notify_err)


async def _run_insight_refresh_sync() -> None:
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _run_insight_all_accounts)


def _run_insight_all_accounts() -> None:
    """Genera/refresca el insight diario para cada cuenta activa."""
    from adapters.rails_http import RailsHttpAdapter
    from agents.insight import run_insight_refresh

    admin_api = RailsHttpAdapter()
    try:
        accounts = admin_api.get_active_accounts()
    except Exception as e:
        logger.error("[insight] No se pudo obtener la lista de cuentas: %s", e)
        return

    for account in accounts:
        account_id   = str(account["id"])
        account_name = account.get("name", f"account_{account_id}")
        try:
            api = RailsHttpAdapter(account_id=account_id)
            run_insight_refresh(api=api)
            logger.info("[insight] account_id=%s — OK.", account_id)
        except Exception as e:
            logger.error("[insight] account_id=%s (%s) — error: %s", account_id, account_name, e)
            try:
                from services.python_error_notifier import capture
                capture(e, context=f"insight:account_{account_id}")
            except Exception as notify_err:
                logger.error("[insight] error notifier failed: %s", notify_err)


async def _ping_rails() -> None:
    """Pings Rails /health every 5 min to prevent cold starts on Railway."""
    import httpx
    from adapters.rails_http import BASE_URL, build_service_headers
    try:
        r = httpx.get(f"{BASE_URL}/health", headers=build_service_headers(), timeout=10)
        if r.status_code >= 500:
            logger.warning("[scheduler] keep-alive Rails responded %s", r.status_code)
    except Exception as e:
        logger.warning("[scheduler] keep-alive Rails failed: %s", e)


def _on_job_error(event) -> None:
    if not event.exception:
        return
    try:
        from services.python_error_notifier import capture
        capture(event.exception, context=f"scheduler:{event.job_id}")
    except Exception as e:
        logger.error("[scheduler] error notifier failed: %s", e)


def start() -> AsyncIOScheduler:
    global _scheduler
    _scheduler = _make_scheduler()
    _scheduler.add_listener(_on_job_error, EVENT_JOB_ERROR)
    _scheduler.start()
    logger.info("[scheduler] APScheduler iniciado.")
    return _scheduler


def stop() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("[scheduler] APScheduler detenido.")
