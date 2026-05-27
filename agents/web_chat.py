"""Handler del agente conversacional para el canal web (PWA)."""

from __future__ import annotations

import logging
from datetime import datetime

import httpx

from adapters.rails_http import BASE_URL, RailsHttpAdapter
from ports.messenger import NullMessenger
from services.chat_context import COLOMBIA_TZ
from services.chat_prompts import WEB_SYSTEM_PROMPT
from services.chat_tools import build_tool_map, build_tools
from services.llm_factory import build_llm_provider, resolve_llm_model

logger = logging.getLogger(__name__)

# Herramientas que no tienen sentido en el canal web
_WEB_EXCLUDED_TOOLS = {"send_telegram"}

LEVEL_NAMES = {
    0: "Huevo",
    1: "Pulso",
    2: "Conciencia",
    3: "Estructura",
    4: "Estrategia",
    5: "Sistema Nervioso",
}


def _web_tools() -> list[dict]:
    return [t for t in build_tools() if t["name"] not in _WEB_EXCLUDED_TOOLS]


def _fetch_preflight(api: RailsHttpAdapter, month: int, year: int) -> dict | None:
    """Llama a /agents/preflight y devuelve el resultado, o None si falla."""
    try:
        r = httpx.post(
            f"{BASE_URL}/api/v1/agents/preflight",
            headers=api.headers(),
            json={"month": month, "year": year, "intent": "general"},
            timeout=10,
        )
        r.raise_for_status()
        return r.json().get("data")
    except Exception as exc:
        logger.warning("[web_chat] preflight failed (non-blocking): %s", exc)
        return None


def _build_level_block(progress: dict) -> str:
    """Construye el bloque de nivel para inyectar en el mensaje inicial."""
    level         = progress.get("level", 0)
    readiness     = progress.get("readiness_score", 0)
    streak        = progress.get("streak_days", 0)
    bypass        = progress.get("bypass_readiness", False)
    level_name    = LEVEL_NAMES.get(level, f"Nivel {level}")

    lines = [
        "=== PERFIL DEL USUARIO ===",
        f"Nivel: {level} — {level_name}",
        f"Readiness score: {readiness}/100",
        f"Racha activa: {streak} días",
    ]
    if bypass:
        lines.append("(bypass_readiness: true — usuario de prueba, sin restricciones por nivel)")
    lines.append(
        "Ajustá el tono y profundidad de tus respuestas según el nivel: "
        "0-1 → simple y directo, sin jerga; 2-3 → más profundidad; 4-5 → peer-to-peer."
    )
    lines.append("===")
    return "\n".join(lines)


def _build_initial_message(
    message: str | None,
    event_response: dict | None,
    budget_context: dict | None = None,
    level_block: str | None = None,
) -> str:
    parts = [
        "El usuario está interactuando desde la aplicación web.",
        "Respondé usando herramientas visuales (emit_ui_event, navigate_to). No uses send_telegram.",
    ]

    if level_block:
        parts.append(f"\n{level_block}")

    if budget_context:
        parts.append(f"\n=== CONTEXTO FINANCIERO PRE-CARGADO ===\n{budget_context}\n===")

    if event_response:
        event_type = event_response.get("type", "unknown")
        event_id = event_response.get("event_id")
        data = event_response.get("data") or {}

        if event_type == "form_submitted":
            parts.append(f"El usuario envió un formulario (event_id={event_id}) con los datos: {data}")
        elif event_type == "confirmed":
            parts.append(f"El usuario confirmó la acción (event_id={event_id}). Guardá y cerrá el flujo.")
        elif event_type == "dismissed":
            parts.append(f"El usuario canceló la acción (event_id={event_id}).")
        elif event_type == "categories_selected":
            selected = data.get("selected_categories", [])
            parts.append(f"El usuario seleccionó estas categorías de presupuesto (event_id={event_id}): {selected}. Continuá el wizard de presupuesto al siguiente paso.")
        elif event_type == "amounts_confirmed":
            amounts = data.get("amounts", {})
            parts.append(f"El usuario confirmó los montos por categoría (event_id={event_id}): {amounts}. Calculá el plan y mostrá show_plan_proposal.")
        else:
            parts.append(f"Respuesta del usuario al evento {event_id}: tipo={event_type}, datos={data}")

    if message:
        parts.append(f"Mensaje del usuario: {message}")

    return "\n".join(parts)


def handle_web_chat(
    account_id: int,
    session_id: str,
    message: str | None = None,
    event_response: dict | None = None,
    budget_context: dict | None = None,
) -> None:
    if not message and not event_response:
        logger.warning("[web_chat] session %s: no message nor event_response — skipping", session_id)
        return

    # Adaptador con el account_id correcto — aislamiento por cuenta
    api = RailsHttpAdapter(account_id=str(account_id))
    messenger = NullMessenger()
    now_col = datetime.now(COLOMBIA_TZ)
    state = {"responded": False, "mutated": False, "source_event_id": None, "session_id": session_id}

    tool_map = build_tool_map(api, messenger, now_col, state)

    # Preflight: obtener nivel + readiness para inyectar en el contexto del agente
    level_block: str | None = None
    preflight = _fetch_preflight(api, now_col.month, now_col.year)
    if preflight and (progress := preflight.get("user_progress")):
        level_block = _build_level_block(progress)
        logger.info(
            "[web_chat] session %s account %s — level=%s readiness=%s",
            session_id, account_id,
            progress.get("level"), progress.get("readiness_score"),
        )

    initial_message = _build_initial_message(message, event_response, budget_context, level_block)

    try:
        provider = build_llm_provider()
        provider.run_agent(
            system_prompt=WEB_SYSTEM_PROMPT,
            tools=_web_tools(),
            tool_map=tool_map,
            initial_message=initial_message,
            max_iterations=12,
            model=resolve_llm_model(),
        )
        logger.info("[web_chat] session %s completed mutated=%s", session_id, state["mutated"])

        if state["mutated"]:
            try:
                tool_map["emit_ui_event"]({
                    "event_type": "data_changed",
                    "payload": {},
                    "session_id": session_id,
                })
            except Exception as emit_exc:
                logger.warning("[web_chat] data_changed emit failed: %s", emit_exc)
    except Exception as exc:
        logger.error("[web_chat] session %s failed: %s", session_id, exc, exc_info=True)
        try:
            tool_map["emit_ui_event"]({
                "event_type": "show_card",
                "payload": {
                    "title": "Algo salió mal",
                    "body": "No pude procesar tu solicitud. Intentá de nuevo.",
                    "tone": "warning",
                },
            })
        except Exception:
            pass
