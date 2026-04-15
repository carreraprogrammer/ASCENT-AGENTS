"""
flows/financial_context_wizard.py — Wizard de configuración del contexto financiero.

Se activa cuando:
  a) El agente nocturno detecta que financial_context es null
  b) El usuario escribe que quiere configurar su contexto financiero

Flujo (3 steps):
  1. Fase financiera — explica cada opción, recomienda basado en deudas/ingresos
  2. Estrategia de pago — snowball vs avalanche con coaching
  3. Reward % — qué % del sobrante va a gustos vs deuda/ahorro
  → Guarda via PATCH /api/v1/financial_context y confirma
"""

from __future__ import annotations

import logging

from ports.rails_api import RailsApiPort
from ports.messenger import MessengerPort

logger = logging.getLogger(__name__)

PHASES = {
    "debt_payoff":      "🔴 Modo pago de deudas",
    "emergency_fund":   "🟡 Construyendo fondo de emergencia",
    "investing":        "🟢 Etapa de inversión",
    "wealth_building":  "💎 Construcción de patrimonio",
}

STRATEGIES = {
    "snowball":  "⛄ Snowball — pagás la deuda más pequeña primero (motivación rápida)",
    "avalanche": "🏔 Avalanche — pagás la de mayor tasa primero (matemáticamente óptimo)",
}


def _fmt_cop(n: int) -> str:
    return f"${n:,.0f}".replace(",", ".")


# ── Trigger ───────────────────────────────────────────────────────────────────

def trigger(api: RailsApiPort, messenger: MessengerPort) -> None:
    """Lanza el wizard creando un PendingAction."""
    existing = api.get_active_pending_action()
    if existing and existing.get("action_type") == "financial_context_setup":
        return  # ya está en curso

    action = api.create_pending_action(
        action_type="financial_context_setup",
        total_steps=3,
        context={},
    )
    _go_to_step(api, messenger, action["id"], {}, 1)


# ── Router de callbacks ───────────────────────────────────────────────────────

def handle_update(
    api: RailsApiPort,
    messenger: MessengerPort,
    pending_action: dict,
    update_type: str,
    payload: dict,
    callback_query_id: str | None = None,
) -> None:
    step    = pending_action.get("current_step", 1)
    ctx     = pending_action.get("context") or {}
    pa_id   = pending_action["id"]

    if update_type != "callback_query":
        return

    data = payload.get("data", "")
    if not data.startswith("wz_fc:"):
        return

    _, key = data.split(":", 1)

    if step == 1:
        ctx["phase"] = key
        api.update_pending_action(pa_id, context=ctx, current_step=2)
        _go_to_step(api, messenger, pa_id, ctx, 2)

    elif step == 2:
        ctx["strategy"] = key
        api.update_pending_action(pa_id, context=ctx, current_step=3)
        _go_to_step(api, messenger, pa_id, ctx, 3)

    elif step == 3:
        ctx["reward_pct"] = int(key)
        api.update_financial_context(
            phase=ctx["phase"],
            strategy=ctx["strategy"],
            reward_pct=ctx["reward_pct"],
        )
        api.update_pending_action(pa_id, status="completed", current_step=3)

        phase_label    = PHASES.get(ctx["phase"], ctx["phase"])
        strategy_label = STRATEGIES.get(ctx["strategy"], ctx["strategy"])
        messenger.send_message(
            f"✅ <b>Contexto financiero configurado</b>\n\n"
            f"Fase: {phase_label}\n"
            f"Estrategia: {strategy_label}\n"
            f"Reward: {ctx['reward_pct']}% del sobrante para vos\n\n"
            f"El agente nocturno ya tiene esto en cuenta. Podés cambiarlo cuando quieras con "
            f"un mensaje como <code>configurar contexto financiero</code>."
        )


# ── Steps ─────────────────────────────────────────────────────────────────────

def _go_to_step(
    api: RailsApiPort,
    messenger: MessengerPort,
    pa_id: int | str,
    ctx: dict,
    step: int,
) -> None:

    if step == 1:
        _step_phase(api, messenger)
    elif step == 2:
        _step_strategy(api, messenger, ctx)
    elif step == 3:
        _step_reward(messenger, ctx)


def _step_phase(api: RailsApiPort, messenger: MessengerPort) -> None:
    debts = api.get_debts()
    total_deuda = sum(d.get("current_balance", 0) for d in debts)
    cuotas      = sum(d.get("monthly_payment", 0) for d in debts)

    recomendacion = ""
    if total_deuda > 20_000_000:
        recomendacion = (
            f"\n\n💡 <b>Mi recomendación:</b> Con {_fmt_cop(total_deuda)} en deudas y "
            f"{_fmt_cop(cuotas)}/mes en cuotas, estás claramente en modo <b>pago de deudas</b>. "
            f"No tiene sentido invertir antes de limpiar esto."
        )

    messenger.send_with_buttons(
        text=(
            "<b>Contexto financiero — Paso 1 de 3</b>\n\n"
            "¿En qué fase financiera estás ahora mismo?\n\n"
            "🔴 <b>Pago de deudas</b> — tu prioridad es liquidar lo que debés\n"
            "🟡 <b>Fondo de emergencia</b> — estás construyendo un colchón de 3-6 meses\n"
            "🟢 <b>Inversión</b> — deudas bajo control, empezás a hacer crecer la plata\n"
            "💎 <b>Patrimonio</b> — modo avanzado de construcción de riqueza"
            + recomendacion
        ),
        buttons=[
            [{"text": "🔴 Pago de deudas",         "callback_data": "wz_fc:debt_payoff"}],
            [{"text": "🟡 Fondo de emergencia",     "callback_data": "wz_fc:emergency_fund"}],
            [{"text": "🟢 Inversión",               "callback_data": "wz_fc:investing"}],
            [{"text": "💎 Construcción patrimonio", "callback_data": "wz_fc:wealth_building"}],
        ],
    )


def _step_strategy(api: RailsApiPort, messenger: MessengerPort, ctx: dict) -> None:
    debts = api.get_debts()
    debts_activas = [d for d in debts if d.get("status") == "active"]

    # Identificar cuál conviene según datos reales
    por_saldo = sorted(debts_activas, key=lambda d: d.get("current_balance", 0))
    por_tasa  = sorted(debts_activas, key=lambda d: d.get("interest_rate", 0), reverse=True)

    snowball_target  = por_saldo[0]["name"]  if por_saldo  else "—"
    avalanche_target = por_tasa[0]["name"]   if por_tasa   else "—"

    tiene_tasas = any(d.get("interest_rate", 0) > 0 for d in debts_activas)

    if tiene_tasas:
        recomendacion = (
            f"\n\n💡 <b>Avalanche</b> te ahorra más plata — empezarías por <b>{avalanche_target}</b>. "
            f"Snowball es si necesitás motivación rápida — empezarías por <b>{snowball_target}</b>."
        )
    else:
        recomendacion = (
            f"\n\n💡 Tus deudas no tienen tasa registrada. Snowball te da victorias rápidas. "
            f"Empezarías liquidando <b>{snowball_target}</b>."
        )

    messenger.send_with_buttons(
        text=(
            "<b>Contexto financiero — Paso 2 de 3</b>\n\n"
            "¿Cómo priorizás el pago de deudas?\n\n"
            "⛄ <b>Snowball</b> — pagás la más pequeña primero. Más motivación, algo más caro.\n"
            "🏔 <b>Avalanche</b> — pagás la de mayor tasa primero. Matemáticamente óptimo."
            + recomendacion
        ),
        buttons=[
            [{"text": "⛄ Snowball (menor saldo)",  "callback_data": "wz_fc:snowball"}],
            [{"text": "🏔 Avalanche (mayor tasa)",  "callback_data": "wz_fc:avalanche"}],
        ],
    )


def _step_reward(messenger: MessengerPort, ctx: dict) -> None:
    phase = ctx.get("phase", "")

    if phase == "debt_payoff":
        sugerencia = "En modo deuda, 10–15% es razonable. Más que eso frena el progreso."
    elif phase == "emergency_fund":
        sugerencia = "Podés ir con 15–20% mientras construís el colchón."
    else:
        sugerencia = "Con deudas bajo control, 20–25% está bien."

    messenger.send_with_buttons(
        text=(
            "<b>Contexto financiero — Paso 3 de 3</b>\n\n"
            "Del dinero que sobra al final del mes (después de gastos fijos y cuotas), "
            "¿qué % querés reservar para gastos libres / gustos?\n\n"
            f"💡 {sugerencia}"
        ),
        buttons=[
            [
                {"text": "5%",  "callback_data": "wz_fc:5"},
                {"text": "10%", "callback_data": "wz_fc:10"},
                {"text": "15%", "callback_data": "wz_fc:15"},
            ],
            [
                {"text": "20%", "callback_data": "wz_fc:20"},
                {"text": "25%", "callback_data": "wz_fc:25"},
                {"text": "30%", "callback_data": "wz_fc:30"},
            ],
        ],
    )
