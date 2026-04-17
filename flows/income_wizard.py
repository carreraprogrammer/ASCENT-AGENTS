"""
flows/income_wizard.py — Wizard de registro de ingresos.

Se activa cuando:
  a) El agente de presupuesto detecta que no hay fuentes de ingreso
  b) El usuario pide explícitamente registrar sus ingresos
  c) El preflight detecta intent=income_setup

Flujo (4–5 steps):
  1. Pedir nombre del ingreso base (texto libre)
  2. Pedir monto del ingreso base (texto libre)
  3. Pedir rango de días (botones)
  4. ¿Hay ingresos variables? (botones)
  [5. Si sí: nombre + monto del variable]
  [6. Si sí: confiabilidad del variable (botones)]
  → Guarda via POST /api/v1/income_sources y confirma
"""

from __future__ import annotations

import logging
import re

from ports.rails_api import RailsApiPort
from ports.messenger import MessengerPort

logger = logging.getLogger(__name__)

DAY_RANGES = [
    ("1 al 5",   1,  5),
    ("6 al 10",  6, 10),
    ("11 al 15", 11, 15),
    ("16 al 20", 16, 20),
    ("21 al 25", 21, 25),
    ("26 al 31", 26, 31),
]

RELIABILITY_OPTIONS = [
    ("25% — muy incierto",         25),
    ("50% — la mitad de los meses", 50),
    ("75% — casi siempre llega",   75),
]


def _fmt_cop(n: int) -> str:
    return f"${n:,.0f}".replace(",", ".")


def _parse_monto(text: str) -> int | None:
    text = text.strip().lower().replace("$", "").replace(".", "").replace(",", "")
    try:
        if text.endswith("m"):
            return int(float(text[:-1]) * 1_000_000)
        if text.endswith("k"):
            return int(float(text[:-1]) * 1_000)
        val = int(float(re.sub(r"[^\d.]", "", text) or "0"))
        return val if val > 0 else None
    except (ValueError, AttributeError):
        return None


# ── Trigger ───────────────────────────────────────────────────────────────────

def trigger(api: RailsApiPort, messenger: MessengerPort, reason: str | None = None) -> dict | None:
    """Lanza el wizard creando un PendingAction."""
    existing = api.get_active_pending_action()
    if existing and existing.get("action_type") == "income_setup":
        if reason:
            messenger.send_message(reason)
        _go_to_step(api, messenger, existing["id"], existing.get("context", {}), existing.get("current_step") or 1)
        return existing

    action = api.create_pending_action(
        action_type="income_setup",
        total_steps=5,
        context={},
    )
    if reason:
        messenger.send_message(reason)
    _go_to_step(api, messenger, action["id"], {}, 1)
    return action


# ── Router de callbacks y mensajes ────────────────────────────────────────────

def handle_update(
    api: RailsApiPort,
    messenger: MessengerPort,
    pending_action: dict,
    update_type: str,
    payload: dict,
    callback_query_id: str | None = None,
) -> None:
    step  = pending_action.get("current_step", 1)
    ctx   = pending_action.get("context") or {}
    pa_id = pending_action["id"]

    if callback_query_id:
        messenger.answer_callback(callback_query_id, "✅")

    if update_type == "callback_query":
        data = payload.get("data", "")
        _handle_callback(api, messenger, pa_id, step, ctx, data)
    elif update_type == "message":
        text = payload.get("text", "").strip()
        _handle_message(api, messenger, pa_id, step, ctx, text)


def _handle_callback(
    api: RailsApiPort,
    messenger: MessengerPort,
    pa_id: int | str,
    step: int,
    ctx: dict,
    data: str,
) -> None:
    if not data.startswith("wi:"):
        return

    _, key = data.split(":", 1)

    # Step 3: day range selected
    if key.startswith("day:"):
        _, day_from, day_to = key.split(":")
        ctx["base_day_from"] = int(day_from)
        ctx["base_day_to"]   = int(day_to)
        _go_to_step(api, messenger, pa_id, ctx, 4)
        return

    # Step 4: variable income?
    if key == "var:yes":
        _go_to_step(api, messenger, pa_id, ctx, 5)
        return

    if key == "var:no":
        _save_base_income(api, messenger, pa_id, ctx)
        return

    # Step 6: reliability
    if key.startswith("rel:"):
        reliability = int(key.split(":")[1])
        ctx["var_reliability"] = reliability
        _save_all(api, messenger, pa_id, ctx)
        return

    # Step 6b: skip variable
    if key == "var:skip":
        _save_base_income(api, messenger, pa_id, ctx)
        return

    logger.warning("[income_wizard] callback no reconocido: %s", data)


def _handle_message(
    api: RailsApiPort,
    messenger: MessengerPort,
    pa_id: int | str,
    step: int,
    ctx: dict,
    text: str,
) -> None:
    if step == 1:
        ctx["base_name"] = text
        _go_to_step(api, messenger, pa_id, ctx, 2)

    elif step == 2:
        monto = _parse_monto(text)
        if monto:
            ctx["base_amount"] = monto
            _go_to_step(api, messenger, pa_id, ctx, 3)
        else:
            messenger.send_message("No entendí el monto. Escribilo como <code>6400000</code> o <code>6.4M</code>.")

    elif step == 5:
        # Variable name
        if not ctx.get("var_name"):
            ctx["var_name"] = text
            api.update_pending_action(pa_id, context=ctx)
            messenger.send_message(f"Y cuando llega, ¿cuánto es aproximadamente? Escribí el monto.")
        else:
            # Variable amount
            monto = _parse_monto(text)
            if monto:
                ctx["var_amount"] = monto
                _go_to_step(api, messenger, pa_id, ctx, 6)
            else:
                messenger.send_message("No entendí el monto. Escribilo como <code>1500000</code> o <code>1.5M</code>.")

    else:
        messenger.send_message("Usá los botones para avanzar.")


# ── Steps ─────────────────────────────────────────────────────────────────────

def _go_to_step(
    api: RailsApiPort,
    messenger: MessengerPort,
    pa_id: int | str,
    ctx: dict,
    step: int,
) -> None:
    api.update_pending_action(pa_id, current_step=step, context=ctx)

    if step == 1:
        messenger.send_message(
            "Vamos a registrar tus ingresos para que el sistema pueda calcular tu presupuesto real.\n\n"
            "Primero, <b>¿cómo se llama tu ingreso más seguro?</b>\n"
            "El que siempre llega — por ejemplo: <i>Salario Acme</i>"
        )

    elif step == 2:
        name = ctx.get("base_name", "tu ingreso")
        messenger.send_message(
            f"Perfecto. ¿Cuánto recibís de <b>{name}</b> cada mes?\n"
            "Escribí el monto, por ejemplo: <code>6400000</code> o <code>6.4M</code>"
        )

    elif step == 3:
        name = ctx.get("base_name", "este ingreso")
        amount = ctx.get("base_amount", 0)
        messenger.send_with_buttons(
            text=(
                f"<b>{name}</b> — {_fmt_cop(amount)}\n\n"
                "¿En qué rango de días del mes suele llegar?"
            ),
            buttons=[
                [{"text": label, "callback_data": f"wi:day:{df}:{dt}"}]
                for label, df, dt in DAY_RANGES
            ],
        )

    elif step == 4:
        messenger.send_with_buttons(
            text="¿Tenés algún <b>ingreso variable</b> además? (freelance, comisiones, arriendo recibido, etc.)",
            buttons=[
                [{"text": "Sí, agregar uno", "callback_data": "wi:var:yes"}],
                [{"text": "No, solo ese",    "callback_data": "wi:var:no"}],
            ],
        )

    elif step == 5:
        messenger.send_message(
            "¿Cómo se llama ese ingreso variable?\n"
            "Por ejemplo: <i>Freelance Globex</i>, <i>Comisión ventas</i>"
        )

    elif step == 6:
        var_name   = ctx.get("var_name", "tu ingreso variable")
        var_amount = ctx.get("var_amount", 0)
        messenger.send_with_buttons(
            text=(
                f"<b>{var_name}</b> — {_fmt_cop(var_amount)}\n\n"
                "¿Qué tan seguido llega este ingreso?"
            ),
            buttons=[
                [{"text": label, "callback_data": f"wi:rel:{pct}"}]
                for label, pct in RELIABILITY_OPTIONS
            ] + [[{"text": "Mejor lo omito", "callback_data": "wi:var:skip"}]],
        )

    else:
        logger.error("[income_wizard] step desconocido: %d", step)


# ── Persistencia ──────────────────────────────────────────────────────────────

def _save_base_income(
    api: RailsApiPort,
    messenger: MessengerPort,
    pa_id: int | str,
    ctx: dict,
) -> None:
    try:
        api.create_income_source(
            name=ctx["base_name"],
            expected_amount=ctx["base_amount"],
            expected_day_from=ctx["base_day_from"],
            expected_day_to=ctx["base_day_to"],
            classification="base",
            reliability_score=100,
            is_variable=False,
        )
        api.update_pending_action(pa_id, status="completed")
        messenger.send_message(
            f"✅ <b>Ingreso base guardado</b>: {ctx['base_name']} — {_fmt_cop(ctx['base_amount'])}\n\n"
            "El sistema ya puede calcular tu presupuesto real. "
            "Cuando quieras agregar más ingresos o ajustar este, escribí <code>mis ingresos</code>."
        )
    except Exception as e:
        logger.error("[income_wizard] error guardando ingreso base: %s", e)
        messenger.send_message(f"❌ Error al guardar: {e}. Intentá de nuevo.")


def _save_all(
    api: RailsApiPort,
    messenger: MessengerPort,
    pa_id: int | str,
    ctx: dict,
) -> None:
    try:
        api.create_income_source(
            name=ctx["base_name"],
            expected_amount=ctx["base_amount"],
            expected_day_from=ctx["base_day_from"],
            expected_day_to=ctx["base_day_to"],
            classification="base",
            reliability_score=100,
            is_variable=False,
        )
        api.create_income_source(
            name=ctx["var_name"],
            expected_amount=ctx["var_amount"],
            expected_day_from=ctx.get("base_day_from", 1),
            expected_day_to=ctx.get("base_day_to", 31),
            classification="variable",
            reliability_score=ctx.get("var_reliability", 50),
            is_variable=True,
        )
        api.update_pending_action(pa_id, status="completed")
        messenger.send_message(
            f"✅ <b>Ingresos guardados</b>\n\n"
            f"• Base: {ctx['base_name']} — {_fmt_cop(ctx['base_amount'])}\n"
            f"• Variable: {ctx['var_name']} — {_fmt_cop(ctx['var_amount'])} "
            f"({ctx.get('var_reliability', 50)}% de confiabilidad)\n\n"
            "El sistema ya puede calcular tu presupuesto real. "
            "Para el plan del mes escribí <code>presupuesto</code>."
        )
    except Exception as e:
        logger.error("[income_wizard] error guardando ingresos: %s", e)
        messenger.send_message(f"❌ Error al guardar: {e}. Intentá de nuevo.")
