import logging
from datetime import datetime

from ports.messenger import MessengerPort, ParsedUpdate
from ports.rails_api import RailsApiPort
from services.chat_context import COLOMBIA_TZ, event_source_id, normalize_telegram_html, telegram_context
from services.chat_preflight import detect_preflight_intent, inject_soft_nudge, run_preflight
from services.chat_prompts import COMMAND_PROMPTS, HELP_TEXT, SYSTEM_PROMPT
from services.chat_tools import build_tool_map, build_tools
from services.conversation_store import append_history, get_history
from services.llm_factory import build_llm_provider, resolve_llm_model

logger = logging.getLogger(__name__)

MESES = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"
]


def _fmt_cop(amount: int | float | str | None) -> str:
    value = int(float(amount or 0))
    return f"${value:,}".replace(",", ".")


def _resource_attrs(resource: dict) -> dict:
    return resource.get("attributes") or resource


def _day_label(day_from: int | str | None, day_to: int | str | None) -> str:
    start = int(day_from or 0)
    end = int(day_to or start)
    if start <= 0:
        return "sin fecha"
    if end == start:
        return f"día {start}"
    return f"días {start}-{end}"


def _classification_label(attrs: dict) -> str:
    classification = (attrs.get("classification") or "").strip()
    if classification == "base":
        return "base confiable"
    if classification == "variable" or attrs.get("is_variable") is True:
        return "variable"
    if classification == "seasonal":
        return "estacional"
    if classification == "one_time":
        return "único"
    return "sin clasificar"


def _income_source_line(source: dict) -> str:
    attrs = _resource_attrs(source)
    schedules = attrs.get("schedules") or []
    if schedules:
        schedule_bits = [
            _day_label(row.get("expected_day_from"), row.get("expected_day_to"))
            for row in schedules[:3]
        ]
        timing = ", ".join(schedule_bits)
    else:
        timing = _day_label(attrs.get("expected_day_from"), attrs.get("expected_day_to"))

    return (
        f"• {attrs.get('name') or 'Ingreso'} — "
        f"{_fmt_cop(attrs.get('expected_amount'))}/mes · "
        f"{_classification_label(attrs)} · {timing}"
    )


def _income_transaction_line(transaction: dict) -> str:
    attrs = _resource_attrs(transaction)
    date = str(attrs.get("date") or "")[:10]
    concept = attrs.get("concept") or "Ingreso"
    return f"• {date} — {concept} — {_fmt_cop(attrs.get('amount'))}"


def _variation_line(actual: int, projected: int) -> str:
    delta = actual - projected
    if projected <= 0:
        return "• Variación vs plan: sin plan de ingresos comparable"
    if delta == 0:
        return "• Variación vs plan: en línea con lo esperado"
    sign = "+" if delta > 0 else "-"
    return f"• Variación vs plan: {sign}{_fmt_cop(abs(delta))}"


def _send_income_summary(api: RailsApiPort, messenger: MessengerPort) -> None:
    now_col = datetime.now(COLOMBIA_TZ)
    month = now_col.month
    year = now_col.year

    summary = api.get_summary(month, year)
    sources = [source for source in api.get_income_sources() if _resource_attrs(source).get("active", True)]
    transactions = api.get_transactions(month, year)
    income_transactions = [
        txn for txn in transactions
        if _resource_attrs(txn).get("transaction_type") == "income"
        and _resource_attrs(txn).get("status") == "confirmed"
    ]

    balance = summary.get("balance") or {}
    plan = summary.get("monthly_plan") or {}
    liquidity = summary.get("liquidity") or {}
    actual_income = int(balance.get("income_confirmed") or 0)
    base_income = int(plan.get("base_budget_income") or 0)
    variable_income = int(plan.get("expected_variable_income") or 0)
    if base_income <= 0 and variable_income <= 0:
        base_income = sum(
            int(_resource_attrs(source).get("expected_amount") or 0)
            for source in sources
            if _classification_label(_resource_attrs(source)) == "base confiable"
        )
        variable_income = sum(
            int(_resource_attrs(source).get("expected_amount") or 0)
            for source in sources
            if _classification_label(_resource_attrs(source)) != "base confiable"
        )
    projected_income = base_income + variable_income

    source_lines = [_income_source_line(source) for source in sources[:8]]
    if not source_lines:
        source_lines = [
            "• No hay fuentes proyectadas activas. Podés configurarlas desde la UI o escribirme <code>configurar ingresos</code>."
        ]

    recent_income_lines = [_income_transaction_line(txn) for txn in income_transactions[:6]]
    if not recent_income_lines:
        recent_income_lines = [ "• No hay ingresos confirmados registrados este mes." ]

    message = "\n".join([
        f"📥 <b>Ingresos — {MESES[month - 1].capitalize()} {year}</b>",
        "",
        "<b>Real del mes</b>",
        f"• Confirmado: <b>{_fmt_cop(actual_income)}</b>",
        f"• Plan esperado: {_fmt_cop(projected_income)} ({_fmt_cop(base_income)} base + {_fmt_cop(variable_income)} variable)",
        _variation_line(actual_income, projected_income),
        f"• Pendiente proyectado este mes: {_fmt_cop(liquidity.get('pending_income'))}",
        "",
        "<b>Fuentes proyectadas</b>",
        *source_lines,
        "",
        "<b>Registrado este mes</b>",
        *recent_income_lines,
        "",
        "Para crear o corregir una fuente, decímelo en una frase o hacelo desde la UI."
    ])

    messenger.send_message(message)


def _apply_preflight(
    api: RailsApiPort,
    messenger: MessengerPort,
    *,
    initial_message: str,
    command: str | None = None,
    text: str | None = None,
) -> str | None:
    intent = detect_preflight_intent(command=command, text=text)
    if not intent:
        return initial_message

    if intent == "income_setup":
        from flows import income_wizard

        income_wizard.trigger(api, messenger)
        return None

    if intent == "debt_status":
        return initial_message

    now_col = datetime.now(COLOMBIA_TZ)
    result = run_preflight(api, intent=intent, now=now_col)
    action = result.get("action")

    if action == "block" and (result.get("wizard") or {}).get("type") == "budget_planning":
        from flows import budget_wizard

        budget_wizard.trigger(api, messenger, reason=result.get("message"))
        return None

    if action == "soft_nudge":
        return inject_soft_nudge(initial_message, result)

    return initial_message


def _run_conversation(
    api: RailsApiPort,
    messenger: MessengerPort,
    initial_message: str,
    source_event_id: str | None = None,
    prior_messages: list[dict] | None = None,
) -> str | None:
    """Returns the assistant's final response text (for conversation history), or None."""
    now_col = datetime.now(COLOMBIA_TZ)
    state = {
        "responded": False,
        "mutated": False,
        "source_event_id": source_event_id,
        "last_response": None,
    }
    provider = build_llm_provider()
    final_text = provider.run_agent(
        system_prompt=SYSTEM_PROMPT,
        tools=build_tools(),
        tool_map=build_tool_map(api, messenger, now_col, state),
        initial_message=initial_message,
        max_iterations=12,
        model=resolve_llm_model(),
        prior_messages=prior_messages,
    )

    if state["responded"]:
        return state["last_response"]

    if final_text:
        logger.warning("[chat_agent] provider returned direct text without send_telegram tool")
        messenger.send_message(normalize_telegram_html(final_text))
        return final_text

    if state["mutated"]:
        messenger.send_message("✅ Listo.")
        return None

    messenger.send_message("⚠️ No pude cerrar bien la respuesta. Intentá de nuevo.")
    return None


def handle_command(api: RailsApiPort, messenger: MessengerPort, parsed: ParsedUpdate) -> None:
    command = parsed.command or ""

    if command not in COMMAND_PROMPTS:
        messenger.send_message(f"❓ No conozco el comando <code>/{command}</code>.\n\n{HELP_TEXT}")
        return

    # Comandos que disparan un wizard directamente
    if COMMAND_PROMPTS[command] == "__income_wizard__":
        from flows import income_wizard
        income_wizard.trigger(api, messenger)
        return

    if COMMAND_PROMPTS[command] == "__income_summary__":
        _send_income_summary(api, messenger)
        return

    try:
        now_col = datetime.now(COLOMBIA_TZ)
        date_context = (
            f"Fecha actual en Colombia: {now_col.strftime('%d/%m/%Y')}\n"
            f"Mes actual: {now_col.month}, año actual: {now_col.year}\n"
        )
        initial_message = _apply_preflight(
            api,
            messenger,
            initial_message=date_context + COMMAND_PROMPTS[command],
            command=command,
            text=parsed.text,
        )
        if initial_message is None:
            return

        _run_conversation(api, messenger, initial_message, source_event_id=None)
    except Exception as exc:
        logger.error("[chat_agent] command error: %s", exc, exc_info=True)
        messenger.send_message("❌ Tuve un problema. Intentá de nuevo.")


CONVERSATION_KEY = "telegram"


def handle_message(api: RailsApiPort, messenger: MessengerPort, parsed: ParsedUpdate) -> None:
    text = (parsed.text or "").strip()
    if not text:
        return

    prior_messages = get_history(CONVERSATION_KEY)

    initial_message = (
        "Mensaje nuevo del usuario en Telegram. "
        "Interpretalo y actuá en tiempo real usando las herramientas disponibles. "
        "Si es un gasto o ingreso claro, registralo. "
        "Si es una corrección o borrado, usá transacciones recientes para resolverlo. "
        "Si hay ambigüedad real, pedí una aclaración breve.\n\n"
        f"{telegram_context(parsed)}\n\n"
        f"Mensaje: {text}"
    )

    try:
        initial_message = _apply_preflight(
            api,
            messenger,
            initial_message=initial_message,
            text=text,
        )
        if initial_message is None:
            return

        response = _run_conversation(
            api, messenger, initial_message,
            source_event_id=event_source_id(parsed),
            prior_messages=prior_messages or None,
        )
        if response:
            append_history(CONVERSATION_KEY, text, response)
    except Exception as exc:
        logger.error("[chat_agent] realtime error: %s", exc, exc_info=True)
        messenger.send_message("❌ No pude procesar eso. Intentá de nuevo.")
