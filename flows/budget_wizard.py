"""
flows/budget_wizard.py — Máquina de estados del wizard de planificación quincenal.

Principios:
- Cada step es idempotente: si el Brain reinicia, puede retomar desde el step guardado
- El contexto acumulado vive en PendingAction.context (Rails persiste)
- El Brain nunca asume — siempre lee el estado desde Rails antes de actuar
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta, date
from statistics import mean

from ports.rails_api import RailsApiPort
from ports.messenger import MessengerPort

logger = logging.getLogger(__name__)

COLOMBIA_TZ = timezone(timedelta(hours=-5))

# Benchmarks 50/30/20 adaptados Colombia
BUDGET_BENCHMARKS = {
    "committed":     0.50,
    "necessary":     0.15,
    "discretionary": 0.10,
    "investment":    0.10,
    "social":        0.05,
}


def _fmt_cop(amount: int) -> str:
    """$1.234.567"""
    return f"${amount:,.0f}".replace(",", ".")


def _now_col() -> datetime:
    return datetime.now(COLOMBIA_TZ)


# ══════════════════════════════════════════════════════════════════════════════
# LÓGICA DE PROPUESTA AUTOMÁTICA
# ══════════════════════════════════════════════════════════════════════════════

def proponer_presupuesto(categoria_key: str, historial_3_meses: list[int], income_total: int) -> int:
    """
    Propone un presupuesto para una categoría basado en historial y benchmarks.
    Redondea a miles de pesos.
    """
    if historial_3_meses:
        promedio = mean(historial_3_meses)
        benchmark = income_total * BUDGET_BENCHMARKS.get(categoria_key, 0.10)
        recomendado = min(promedio * 1.05, benchmark)
    else:
        recomendado = income_total * BUDGET_BENCHMARKS.get(categoria_key, 0.10)
    return round(recomendado / 1000) * 1000


# ══════════════════════════════════════════════════════════════════════════════
# DISPARADOR — corre el día 1 y 15 si no hay PendingAction activo
# ══════════════════════════════════════════════════════════════════════════════

def trigger_planning(api: RailsApiPort, messenger: MessengerPort) -> None:
    """
    Lanzado por el scheduler el día 1 y 15.
    Si ya hay un PendingAction activo, no hace nada (el wizard está en curso).
    """
    existing = api.get_active_pending_action()
    if existing:
        logger.info("[budget_wizard] PendingAction activo encontrado (%s), skip trigger.", existing.get("id"))
        return

    ctx = api.get_financial_context()
    now = _now_col()
    mes_nombre = ["enero", "febrero", "marzo", "abril", "mayo", "junio",
                  "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"][now.month - 1]

    messenger.send_with_buttons(
        text=(
            f"Hola Daniel 👋 Es quincena — ¿planificamos el presupuesto de <b>{mes_nombre}</b>?\n\n"
            "Esto toma unos minutos y define con qué números comparar el agente nocturno este mes."
        ),
        buttons=[
            [{"text": "Sí, vamos 🚀", "callback_data": "wizard:start"}],
            [{"text": "Mañana", "callback_data": "wizard:tomorrow"}],
            [{"text": "No por ahora", "callback_data": "wizard:skip"}],
        ],
    )


# ══════════════════════════════════════════════════════════════════════════════
# ROUTER DE CALLBACKS Y MENSAJES — punto de entrada desde el webhook
# ══════════════════════════════════════════════════════════════════════════════

def handle_update(
    api: RailsApiPort,
    messenger: MessengerPort,
    pending_action: dict,
    update_type: str,
    payload: dict,
    callback_query_id: str | None = None,
) -> None:
    """
    Punto de entrada: el webhook llama esto cuando hay un PendingAction activo.

    Args:
        pending_action: dict con id, current_step, context, status
        update_type: "callback_query" | "message"
        payload: el cuerpo del update de Telegram
        callback_query_id: id del callback_query para responderlo inmediatamente
    """
    action_id = pending_action["id"]
    step = pending_action.get("current_step", 0)
    ctx = pending_action.get("context", {})

    # Responder el callback inmediatamente (quitar spinner)
    if callback_query_id:
        messenger.answer_callback(callback_query_id, "✅")

    if update_type == "callback_query":
        data = payload.get("data", "")
        _handle_callback(api, messenger, action_id, step, ctx, data)
    elif update_type == "message":
        text = payload.get("text", "").strip()
        _handle_message(api, messenger, action_id, step, ctx, text)


def _handle_callback(
    api: RailsApiPort,
    messenger: MessengerPort,
    action_id: int | str,
    step: int,
    ctx: dict,
    data: str,
) -> None:
    parts = data.split(":")

    # ── Trigger inicial ──────────────────────────────────────────────────────
    if data == "wizard:start":
        _go_to_step(api, messenger, action_id, ctx, 1)
        return

    if data == "wizard:tomorrow":
        expires_at = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
        api.update_pending_action(action_id, status="waiting_response", current_step=0, expires_at=expires_at)
        messenger.send_message("Entendido 👍 Te mando el wizard mañana.")
        return

    if data == "wizard:skip":
        api.update_pending_action(action_id, status="cancelled")
        messenger.send_message("Ok, sin presupuesto este período. Puedes pedirme uno cuando quieras.")
        return

    # ── Step 1: confirmar ingresos ────────────────────────────────────────────
    if data == "wz1:ok":
        ctx["ingresos_confirmados"] = True
        _go_to_step(api, messenger, action_id, ctx, 2)
        return
    if data == "wz1:cambio":
        api.update_pending_action(action_id, current_step=1, context=ctx)
        messenger.send_message("¿Qué cambió en tus ingresos este mes? Escríbeme los nuevos montos.")
        return

    # ── Step 2: comprometido ──────────────────────────────────────────────────
    if data == "wz2:ok":
        _go_to_step(api, messenger, action_id, ctx, 3)
        return
    if data == "wz2:cambio":
        api.update_pending_action(action_id, current_step=2, context=ctx)
        messenger.send_message("¿Qué cambió? Dime la deuda o gasto fijo y el nuevo valor.")
        return

    # ── Step 3: abono snowball ────────────────────────────────────────────────
    if data == "wz3:yes":
        ctx["abono_extra"] = ctx.get("abono_sugerido", 0)
        _go_to_step(api, messenger, action_id, ctx, 4)
        return
    if data == "wz3:adjust":
        api.update_pending_action(action_id, current_step=3, context=ctx)
        messenger.send_message("¿Cuánto quieres abonar extra? Escribe el monto en pesos.")
        return
    if data == "wz3:no":
        ctx["abono_extra"] = 0
        _go_to_step(api, messenger, action_id, ctx, 4)
        return

    # ── Step 4: necesario ─────────────────────────────────────────────────────
    if data == "wz4:ok":
        ctx["budget_necessary"] = ctx.get("budget_necessary_sugerido", 0)
        _go_to_step(api, messenger, action_id, ctx, 5)
        return
    if data == "wz4:adjust":
        api.update_pending_action(action_id, current_step=4, context=ctx)
        messenger.send_message("¿Cuánto quieres presupuestar para lo necesario (mercado, transporte, salud, celular)?")
        return

    # ── Step 5: discrecional ──────────────────────────────────────────────────
    if data == "wz5:ok":
        ctx["budget_discretionary"] = ctx.get("budget_discretionary_sugerido", 0)
        _go_to_step(api, messenger, action_id, ctx, 6)
        return
    if data == "wz5:more":
        api.update_pending_action(action_id, current_step=5, context=ctx)
        messenger.send_message("¿Cuánto necesitas para discrecional este mes?")
        return
    if data == "wz5:less":
        api.update_pending_action(action_id, current_step=5, context=ctx)
        messenger.send_message("¿Cuánto puedes comprometerte a gastar en discrecional?")
        return

    # ── Step 6: recompensa ────────────────────────────────────────────────────
    if data == "wz6:ok":
        _go_to_step(api, messenger, action_id, ctx, 7)
        return
    if data == "wz6:change":
        api.update_pending_action(action_id, current_step=6, context=ctx)
        messenger.send_message("¿Qué porcentaje del excedente quieres como recompensa? (ej: 5, 10, 15)")
        return

    # ── Step 7: aprobar plan ──────────────────────────────────────────────────
    if data == "wz7:approve":
        _step_8_save(api, messenger, action_id, ctx)
        return
    if data == "wz7:adjust":
        api.update_pending_action(action_id, current_step=7, context=ctx)
        messenger.send_message(
            "¿Qué quieres ajustar?\n\n"
            "1 — Ingresos\n2 — Comprometido\n3 — Abono extra\n4 — Necesario\n5 — Discrecional\n6 — Recompensa\n\n"
            "Responde con el número del paso."
        )
        return

    logger.warning("[budget_wizard] callback no reconocido: %s", data)


def _handle_message(
    api: RailsApiPort,
    messenger: MessengerPort,
    action_id: int | str,
    step: int,
    ctx: dict,
    text: str,
) -> None:
    """Procesa texto libre según el step actual."""
    if step == 1:
        # Daniel corrigió ingresos
        ctx["nota_ingresos"] = text
        messenger.send_message(f"Anotado: {text}. Actualizaré el contexto financiero cuando aprobemos el plan.")
        _go_to_step(api, messenger, action_id, ctx, 2)

    elif step == 2:
        # Daniel corrigió comprometido
        ctx["nota_comprometido"] = text
        messenger.send_message(f"Anotado: {text}. Lo tengo en cuenta para el plan.")
        _go_to_step(api, messenger, action_id, ctx, 3)

    elif step == 3:
        # Monto de abono personalizado
        monto = _parse_monto(text)
        if monto:
            ctx["abono_extra"] = monto
            messenger.send_message(f"Perfecto, {_fmt_cop(monto)} de abono extra incluidos.")
            _go_to_step(api, messenger, action_id, ctx, 4)
        else:
            messenger.send_message("No entendí el monto. Escribe algo como '200000' o '200k'.")

    elif step == 4:
        monto = _parse_monto(text)
        if monto:
            ctx["budget_necessary"] = monto
            _go_to_step(api, messenger, action_id, ctx, 5)
        else:
            messenger.send_message("No entendí el monto. Escribe algo como '700000' o '700k'.")

    elif step == 5:
        monto = _parse_monto(text)
        if monto:
            ctx["budget_discretionary"] = monto
            _go_to_step(api, messenger, action_id, ctx, 6)
        else:
            messenger.send_message("No entendí. Escribe el monto para discrecional.")

    elif step == 6:
        pct = _parse_pct(text)
        if pct is not None:
            ctx["reward_pct"] = pct
            _go_to_step(api, messenger, action_id, ctx, 7)
        else:
            messenger.send_message("Escribe un porcentaje, por ejemplo: 5")

    elif step == 7:
        # Daniel quiere volver a un paso específico
        try:
            target = int(text.strip())
            if 1 <= target <= 6:
                _go_to_step(api, messenger, action_id, ctx, target)
            else:
                messenger.send_message("Escribe un número del 1 al 6.")
        except ValueError:
            messenger.send_message("Escribe el número del paso al que quieres volver (1-6).")

    else:
        messenger.send_message("No entendí. Usa los botones para avanzar en el plan.")


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS DE DOMINIO
# ══════════════════════════════════════════════════════════════════════════════

def _current_period() -> tuple[int, int]:
    now = _now_col()
    return now.month, now.year


def _month_name(month: int) -> str:
    return ["enero", "febrero", "marzo", "abril", "mayo", "junio",
            "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"][month - 1]


def _active_income_sources(api: RailsApiPort) -> list[dict]:
    return [source for source in api.get_income_sources() if source.get("active", True)]


def _source_classification(source: dict) -> str:
    classification = source.get("classification")
    if classification:
        return classification
    return "variable" if source.get("is_variable") else "base"


def _income_breakdown(api: RailsApiPort) -> tuple[list[dict], list[dict]]:
    sources = _active_income_sources(api)
    base = [source for source in sources if _source_classification(source) == "base"]
    variable = [source for source in sources if _source_classification(source) != "base"]
    return base, variable


def _committed_breakdown(api: RailsApiPort) -> tuple[list[str], int]:
    lines: list[str] = []
    recurring_total = 0

    for obligation in api.get_recurring_obligations():
        if not obligation.get("active", True):
            continue
        if obligation.get("allocatable_type") == "Debt":
            continue
        amount = int(obligation.get("amount", 0) or 0)
        recurring_total += amount
        lines.append(f"• {obligation.get('name', 'Obligación')}: {_fmt_cop(amount)}")

    debt_total = 0
    for debt in api.get_debts():
        if debt.get("status") != "active":
            continue
        payment = int(debt.get("monthly_payment", 0) or 0)
        debt_total += payment
        lines.append(f"• {debt.get('name', 'Deuda')}: {_fmt_cop(payment)}")

    return lines, recurring_total + debt_total


def _get_or_generate_plan(api: RailsApiPort, *, mode: str = "conservative") -> dict:
    month, year = _current_period()
    plan = api.get_current_monthly_plan(month, year)
    if plan:
        return plan
    return api.generate_monthly_plan(month, year, mode=mode)


def _plan_spendable_income(plan: dict) -> int:
    assumptions = plan.get("assumptions") or {}
    return int(assumptions.get("planning_income_used") or plan.get("base_budget_income") or 0)


def _resolve_budget_categories(api: RailsApiPort) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for category in api.get_categories():
        code = category.get("code")
        category_id = category.get("id")
        if code and category_id:
            mapping[code] = category_id
    return mapping


# ══════════════════════════════════════════════════════════════════════════════
# STEPS
# ══════════════════════════════════════════════════════════════════════════════

def _go_to_step(
    api: RailsApiPort,
    messenger: MessengerPort,
    action_id: int | str,
    ctx: dict,
    step: int,
) -> None:
    api.update_pending_action(action_id, current_step=step, context=ctx)

    if step == 1:
        _send_step_1(api, messenger, ctx)
    elif step == 2:
        _send_step_2(api, messenger, ctx)
    elif step == 3:
        _send_step_3(api, messenger, ctx)
    elif step == 4:
        _send_step_4(api, messenger, ctx)
    elif step == 5:
        _send_step_5(api, messenger, ctx)
    elif step == 6:
        _send_step_6(api, messenger, ctx)
    elif step == 7:
        _send_step_7(api, messenger, ctx)
    else:
        logger.error("[budget_wizard] step desconocido: %d", step)


def _send_step_1(api: RailsApiPort, messenger: MessengerPort, ctx: dict) -> None:
    base_sources, variable_sources = _income_breakdown(api)
    if not base_sources and not variable_sources:
        messenger.send_message(
            "Antes de planificar necesito al menos una fuente de ingreso registrada. "
            "Primero define tu ingreso base confiable y luego volvemos al plan."
        )
        return

    lines = []
    if base_sources:
        lines.append("Ingreso base confiable:")
        lines.extend(
            f"• {source['name']} ({source['expected_day_from']}-{source['expected_day_to']}): "
            f"<b>{_fmt_cop(int(source.get('expected_amount', 0) or 0))}</b>"
            for source in base_sources
        )
    if variable_sources:
        lines.append("")
        lines.append("Ingresos variables / extra:")
        lines.extend(
            f"• {source['name']} ({source['expected_day_from']}-{source['expected_day_to']}): "
            f"~<b>{_fmt_cop(int(source.get('expected_amount', 0) or 0))}</b>"
            for source in variable_sources
        )

    messenger.send_with_buttons(
        text=(
            "Este mes tengo registradas estas fuentes de ingreso:\n\n"
            f"{chr(10).join(lines)}\n\n"
            f"¿Es correcto o cambió algo?"
        ),
        buttons=[
            [{"text": "Correcto ✓", "callback_data": "wz1:ok"}],
            [{"text": "Cambió algo", "callback_data": "wz1:cambio"}],
        ],
    )


def _send_step_2(api: RailsApiPort, messenger: MessengerPort, ctx: dict) -> None:
    lines, total = _committed_breakdown(api)
    lines_text = "\n".join(lines) if lines else "• No encontré obligaciones estructurales registradas"

    messenger.send_with_buttons(
        text=(
            f"Tus gastos fijos este mes:\n{lines_text}\n\n"
            f"<b>Total comprometido: {_fmt_cop(total)}</b>\n"
            "¿Esto sigue correcto?"
        ),
        buttons=[
            [{"text": "Ok ✓", "callback_data": "wz2:ok"}],
            [{"text": "Hay un cambio", "callback_data": "wz2:cambio"}],
        ],
    )
    ctx["total_committed"] = total


def _send_step_3(api: RailsApiPort, messenger: MessengerPort, ctx: dict) -> None:
    debts = api.get_debts()
    active = [d for d in debts if d.get("status") == "active"]
    fc = api.get_financial_context()
    strategy = fc.get("strategy", "snowball") if fc else "snowball"

    if not active:
        ctx["abono_extra"] = 0
        messenger.send_with_buttons(
            text="No veo deudas activas para acelerar este mes. Continuemos con el presupuesto base.",
            buttons=[[{"text": "Continuar ✓", "callback_data": "wz3:no"}]],
        )
        return

    if strategy == "snowball":
        target = min(active, key=lambda d: d.get("current_balance", 0))
    else:
        target = max(active, key=lambda d: d.get("interest_rate", 0))

    balance = target.get("current_balance", 0)
    monthly = target.get("monthly_payment", 0)
    sugerido = min(balance, round(balance * 0.25 / 1000) * 1000)
    sugerido = max(sugerido, 50_000)

    meses_restantes = round(balance / (monthly + sugerido)) if (monthly + sugerido) > 0 else "?"
    ctx["abono_sugerido"] = sugerido
    ctx["abono_target_id"] = target.get("id")
    ctx["abono_target_name"] = target.get("name", "")

    messenger.send_with_buttons(
        text=(
            f"Estrategia <b>{strategy}</b> — te recomiendo abonar "
            f"<b>{_fmt_cop(sugerido)} extra</b> a <i>{target['name']}</i> "
            f"(saldo {_fmt_cop(balance)}).\n"
            f"{'Lo liquidas en ' + str(meses_restantes) + ' meses.' if isinstance(meses_restantes, int) else ''}"
        ),
        buttons=[
            [{"text": "Sí, incluirlo ✓", "callback_data": "wz3:yes"}],
            [{"text": "Ajustar monto", "callback_data": "wz3:adjust"}],
            [{"text": "No este mes", "callback_data": "wz3:no"}],
        ],
    )


def _send_step_4(api: RailsApiPort, messenger: MessengerPort, ctx: dict) -> None:
    sugerido = ctx.get("budget_necessary_sugerido")
    if not sugerido:
        plan = _get_or_generate_plan(api)
        income = max(_plan_spendable_income(plan), 1)
        sugerido = proponer_presupuesto("necessary", [], income)
        ctx["budget_necessary_sugerido"] = sugerido

    messenger.send_with_buttons(
        text=(
            f"Para lo <b>necesario</b> (mercado, transporte, celular, salud) "
            f"te propongo presupuestar <b>{_fmt_cop(sugerido)}</b>.\n"
            "¿Te parece bien?"
        ),
        buttons=[
            [{"text": "Bien ✓", "callback_data": "wz4:ok"}],
            [{"text": "Ajustar", "callback_data": "wz4:adjust"}],
        ],
    )


def _send_step_5(api: RailsApiPort, messenger: MessengerPort, ctx: dict) -> None:
    sugerido = ctx.get("budget_discretionary_sugerido")
    if not sugerido:
        plan = _get_or_generate_plan(api)
        sugerido = int(plan.get("discretionary_limit", 0) or 0)
        if sugerido <= 0:
            sugerido = proponer_presupuesto("discretionary", [], max(_plan_spendable_income(plan), 1))
        ctx["budget_discretionary_sugerido"] = sugerido

    messenger.send_with_buttons(
        text=(
            f"<b>Discrecional</b> (restaurantes, ocio, suscripciones, ropa):\n"
            f"Te propongo <b>{_fmt_cop(sugerido)}</b>.\n"
            "¿Qué te parece?"
        ),
        buttons=[
            [{"text": "Perfecto ✓", "callback_data": "wz5:ok"}],
            [{"text": "Necesito más", "callback_data": "wz5:more"}],
            [{"text": "Puedo menos", "callback_data": "wz5:less"}],
        ],
    )


def _send_step_6(api: RailsApiPort, messenger: MessengerPort, ctx: dict) -> None:
    fc = api.get_financial_context()
    reward_pct = ctx.get("reward_pct", fc.get("reward_pct", 5) if fc else 5)
    ctx["reward_pct"] = reward_pct

    plan = _get_or_generate_plan(api)
    income = int(plan.get("base_budget_income", 0) or 0)
    committed = int(plan.get("recurring_obligations_total", 0) or 0) + int(plan.get("debt_minimums_total", 0) or 0)
    necessary = ctx.get("budget_necessary", ctx.get("budget_necessary_sugerido", 0))
    discretionary = ctx.get("budget_discretionary", ctx.get("budget_discretionary_sugerido", 0))
    abono = ctx.get("abono_extra", 0)
    buffer = int(plan.get("protected_buffer_amount", 0) or 0)
    excedente = income - committed - necessary - discretionary - abono - buffer
    recompensa = round((max(excedente, 0) * reward_pct / 100) / 1000) * 1000

    ctx["excedente_estimado"] = excedente

    messenger.send_with_buttons(
        text=(
            f"Si llegas al final del mes dentro del presupuesto, el <b>{reward_pct}%</b> "
            f"del excedente es tuyo para gastar sin culpa.\n"
            f"Con este plan serían ~<b>{_fmt_cop(recompensa)}</b>.\n\n"
            "¿Ajustamos el porcentaje?"
        ),
        buttons=[
            [{"text": "Está bien ✓", "callback_data": "wz6:ok"}],
            [{"text": "Cambiar %", "callback_data": "wz6:change"}],
        ],
    )


def _send_step_7(api: RailsApiPort, messenger: MessengerPort, ctx: dict) -> None:
    month, year = _current_period()
    plan = _get_or_generate_plan(api)
    base_sources, variable_sources = _income_breakdown(api)
    base_income = int(plan.get("base_budget_income", 0) or 0)
    variable_income = int(plan.get("expected_variable_income", 0) or 0)

    committed = int(plan.get("recurring_obligations_total", 0) or 0) + int(plan.get("debt_minimums_total", 0) or 0)
    abono = ctx.get("abono_extra", 0)
    necessary = ctx.get("budget_necessary", ctx.get("budget_necessary_sugerido", 0))
    discretionary = ctx.get("budget_discretionary", ctx.get("budget_discretionary_sugerido", 0))
    reward_pct = ctx.get("reward_pct", 5)
    buffer = int(plan.get("protected_buffer_amount", 0) or 0)

    excedente = base_income - committed - abono - necessary - discretionary - buffer
    recompensa = round((max(excedente, 0) * reward_pct / 100) / 1000) * 1000
    resto = excedente - recompensa

    abono_name = ctx.get("abono_target_name", "")
    mes_nombre = _month_name(month).capitalize()

    base_lines = "\n".join(
        f"• {source['name']}: {_fmt_cop(int(source.get('expected_amount', 0) or 0))}"
        for source in base_sources
    ) or "• Sin ingresos base registrados"

    variable_lines = "\n".join(
        f"• {source['name']}: {_fmt_cop(int(source.get('expected_amount', 0) or 0))}"
        for source in variable_sources
    ) or "• Sin ingresos extra registrados"

    alerta = ""
    if excedente < 0:
        alerta = f"\n\n⚠️ <b>Déficit proyectado: {_fmt_cop(abs(excedente))}</b> — revisa discrecional o abono extra."

    plan_text = (
        f"📋 <b>Plan del mes — {mes_nombre} {year}</b>\n\n"
        f"<b>Ingreso base presupuestable</b>\n{base_lines}\n"
        f"Total base: <b>{_fmt_cop(base_income)}</b>\n\n"
        f"<b>Ingresos variables / overflow</b>\n{variable_lines}\n"
        f"Total variable esperado: <b>{_fmt_cop(variable_income)}</b>\n\n"
        f"<b>Comprometido</b>: -{_fmt_cop(committed)}\n"
        f"<b>Buffer protegido</b>: -{_fmt_cop(buffer)}\n"
        f"<b>Necesario</b>: -{_fmt_cop(necessary)}\n"
        f"<b>Discrecional</b>: -{_fmt_cop(discretionary)}\n"
        + (f"<b>Abono extra {abono_name}</b>: -{_fmt_cop(abono)}\n" if abono else "")
        + f"💰 Excedente proyectado: <b>{_fmt_cop(excedente)}</b>\n"
        + f"🎁 Tu recompensa si cumples: <b>{_fmt_cop(recompensa)}</b> ({reward_pct}%)\n"
        + f"↪️ Overflow rule: <b>{plan.get('overflow_rule', 'debt')}</b>\n"
        + f"📈 Resto para metas/ahorro: <b>{_fmt_cop(resto)}</b>"
        + alerta
        + "\n\n¿Aprobamos este plan?"
    )

    messenger.send_with_buttons(
        text=plan_text,
        buttons=[
            [{"text": "Aprobar ✓", "callback_data": "wz7:approve"}],
            [{"text": "Ajustar algo", "callback_data": "wz7:adjust"}],
        ],
    )


def _step_8_save(api: RailsApiPort, messenger: MessengerPort, action_id: int | str, ctx: dict) -> None:
    plan = _get_or_generate_plan(api)
    category_ids = _resolve_budget_categories(api)

    committed = int(plan.get("recurring_obligations_total", 0) or 0) + int(plan.get("debt_minimums_total", 0) or 0)
    necessary = ctx.get("budget_necessary", 0)
    discretionary = ctx.get("budget_discretionary", 0)
    reward_pct = ctx.get("reward_pct", 5)
    abono_extra = ctx.get("abono_extra", 0)

    budgets = []
    if category_ids.get("committed"):
        budgets.append({"category_id": category_ids["committed"], "amount_limit": committed})
    if category_ids.get("necessary") and necessary:
        budgets.append({"category_id": category_ids["necessary"], "amount_limit": necessary})
    if category_ids.get("discretionary") and discretionary:
        budgets.append({"category_id": category_ids["discretionary"], "amount_limit": discretionary})

    assumptions = dict(plan.get("assumptions") or {})
    assumptions.update({
        "necessary_budget": necessary,
        "abono_extra": abono_extra,
        "wizard_confirmed_at": _now_col().isoformat(),
    })

    try:
        api.confirm_monthly_plan(
            plan["id"],
            budgets=budgets,
            discretionary_limit=discretionary,
            reward_pct=reward_pct,
            assumptions=assumptions,
        )

        if reward_pct:
            api.update_financial_context(reward_pct=reward_pct)

        api.update_pending_action(action_id, status="completed")

        messenger.send_message(
            "✅ <b>Plan guardado.</b> Esta noche el agente ya sabe con qué comparar.\n\n"
            "Si ves que algo no cuadra, escríbeme y lo ajustamos."
        )
    except Exception as e:
        logger.error("[budget_wizard] error guardando plan: %s", e)
        messenger.send_message(f"❌ Error al guardar el plan: {e}. Intenta de nuevo.")


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS DE PARSING
# ══════════════════════════════════════════════════════════════════════════════

def _parse_monto(text: str) -> int | None:
    """Parsea montos como '200k', '200000', '1.5M'."""
    text = text.strip().lower().replace("$", "").replace(".", "").replace(",", "")
    try:
        if text.endswith("m"):
            return int(float(text[:-1]) * 1_000_000)
        if text.endswith("k"):
            return int(float(text[:-1]) * 1_000)
        return int(float(text))
    except (ValueError, AttributeError):
        return None


def _parse_pct(text: str) -> int | None:
    text = text.strip().replace("%", "")
    try:
        val = int(float(text))
        return val if 1 <= val <= 100 else None
    except (ValueError, AttributeError):
        return None
