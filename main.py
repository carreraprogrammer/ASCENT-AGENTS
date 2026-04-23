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

from routers import webhook, agents
import scheduler as sched

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Brain iniciando — arrancando scheduler...")
    sched.start()
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
    return JSONResponse(status_code=500, content={"ok": False, "error": "Internal server error"})
