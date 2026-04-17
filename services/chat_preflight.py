"""Heurísticas y adaptación del preflight conversacional."""

from __future__ import annotations

from datetime import datetime

from ports.rails_api import RailsApiPort


def detect_preflight_intent(*, command: str | None = None, text: str | None = None) -> str | None:
    normalized_command = (command or "").strip().lower()
    normalized_text = (text or "").strip().lower()

    if normalized_command == "presupuesto":
        return "budgeting"

    if normalized_command == "resumen":
        return "monthly_status"

    budgeting_markers = (
        "presupuesto",
        "plan del mes",
        "planifi",
        "plan mensual",
        "armemos el mes",
    )
    overflow_markers = (
        "ingreso extra",
        "plata extra",
        "extra que entró",
        "overflow",
        "qué hago con este ingreso",
        "que hago con este ingreso",
    )
    monthly_status_markers = (
        "cómo voy este mes",
        "como voy este mes",
        "cómo voy en abril",
        "como voy en abril",
        "cómo voy en el mes",
        "como voy en el mes",
    )

    if any(marker in normalized_text for marker in budgeting_markers):
        return "budgeting"

    if any(marker in normalized_text for marker in overflow_markers):
        return "overflow"

    if any(marker in normalized_text for marker in monthly_status_markers):
        return "monthly_status"

    return None


def run_preflight(api: RailsApiPort, *, intent: str, now: datetime) -> dict:
    return api.preflight_agent(intent=intent, month=now.month, year=now.year)


def inject_soft_nudge(initial_message: str, preflight: dict) -> str:
    message = (preflight.get("message") or "").strip()
    dimensions = preflight.get("nudge_dimensions") or []
    if not message or not dimensions:
        return initial_message

    return (
        "Preflight del sistema:\n"
        f"- Hay gaps no bloqueantes: {', '.join(dimensions)}.\n"
        f"- Mensaje sugerido al usuario: {message}\n"
        "- Responde normal, pero al final agrega un recordatorio breve y no moralista.\n\n"
        f"{initial_message}"
    )
