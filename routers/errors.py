"""
routers/errors.py — POST /webhook/error

Recibe errores 500 desde daniel15k-api y dispara el agente debugger en background.
"""

import asyncio
import logging
import os

from fastapi import APIRouter, Request, Response, HTTPException

from agents.debugger import DebugPayload, handle as debugger_handle

logger = logging.getLogger(__name__)
router = APIRouter()

WEBHOOK_SECRET = os.environ.get("AGENTS_WEBHOOK_SECRET", "")


@router.post("/webhook/error")
async def error_webhook(request: Request) -> Response:
    if WEBHOOK_SECRET:
        secret = request.headers.get("X-Webhook-Secret", "")
        if secret != WEBHOOK_SECRET:
            raise HTTPException(status_code=401, detail="Invalid webhook secret")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    payload = DebugPayload(
        error_id=body.get("error_id", 0),
        error_hash=body.get("error_hash", ""),
        exception_class=body.get("exception_class", "UnknownError"),
        message=body.get("message", ""),
        stacktrace=body.get("stacktrace", []),
        endpoint=body.get("endpoint", ""),
        http_method=body.get("http_method", ""),
        params=body.get("params", {}),
        occurred_at=body.get("occurred_at", ""),
    )

    logger.info("[errors] received error_id=%s class=%s endpoint=%s",
                payload.error_id, payload.exception_class, payload.endpoint)

    asyncio.get_event_loop().run_in_executor(None, debugger_handle, payload)

    return Response(status_code=202)
