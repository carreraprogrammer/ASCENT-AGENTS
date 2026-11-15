"""
main.py — FastAPI Brain: daniel15k-agents

Arquitectura:
  Telegram ↔ Brain (FastAPI) ↔ Rails API

El Brain orquesta — Rails persiste.
"""

import logging
import os

from contextlib import asynccontextmanager
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from routers import webhook, agents, errors
import scheduler as sched
from adapters.rails_http import RailsHttpAdapter
from cromo.manifest import MANIFEST
from cromo.server import make_server

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ── Ascent como cromo de The Cyborg Way (TCW-1): MCP en /mcp, solo con tokens del cerebro para Ascent ──
cromo_mcp = make_server(RailsHttpAdapter)
cromo_app = cromo_mcp.streamable_http_app(stateless_http=True, json_response=True, host="0.0.0.0")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Brain iniciando — arrancando scheduler...")
    sched.start()
    async with cromo_app.router.lifespan_context(cromo_app):   # el gestor de sesiones del MCP del cromo
        yield
    logger.info("Brain cerrando — deteniendo scheduler...")
    sched.stop()


app = FastAPI(
    title="Daniel 15K — Brain",
    description="Capa de inteligencia entre Telegram y la API de Rails.",
    version="2.0.0",
    lifespan=lifespan,
)

# ── Routers ──────────────────────────────────────────────────────────────────
app.include_router(webhook.router)
app.include_router(agents.router)
app.include_router(errors.router)


app.router.routes.extend(cromo_app.routes)   # /mcp y /.well-known/oauth-protected-resource/mcp


@app.get("/.well-known/cromo.json")
async def cromo_manifest():
    return JSONResponse(MANIFEST, headers={"Cache-Control": "public, max-age=300"})


# ── Health ───────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"ok": True, "service": "daniel15k-agents"}


@app.get("/")
async def root():
    return {"service": "daniel15k-agents", "version": "2.0.0"}


# ── Error handler global ─────────────────────────────────────────────────────
@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    logger.error("Unhandled exception: %s", exc, exc_info=True)
    try:
        from services.python_error_notifier import capture
        capture(
            exc,
            context=f"{request.method} {request.url.path}",
            params=dict(request.query_params),
        )
    except Exception as notify_err:
        logger.error("[main] error notifier failed: %s", notify_err)
    return JSONResponse(status_code=500, content={"ok": False, "error": "Internal server error"})
