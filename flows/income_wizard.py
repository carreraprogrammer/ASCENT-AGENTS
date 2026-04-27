"""
flows/income_wizard.py — Wizard conversacional del perfil de ingresos.

Objetivo:
- capturar el ingreso base confiable
- capturar opcionalmente un ingreso variable
- construir income_sources con cadence + schedules

No duplica fuentes para modelar quincenal/semanal.
"""

from __future__ import annotations

import logging
import re

from ports.rails_api import RailsApiPort
from ports.messenger import MessengerPort

logger = logging.getLogger(__name__)

MONTHLY_WINDOWS = [
    ("Inicio de mes (1-5)", "early", 1, 5),
    ("Primera semana (1-7)", "week1", 1, 7),
    ("Primera quincena (1-15)", "q1", 1, 15),
    ("Mediados (10-20)", "mid", 10, 20),
    ("Segunda quincena (15-31)", "q2", 15, 31),
    ("Fin de mes (25-31)", "late", 25, 31),
]

DAY_OPTIONS = [1, 5, 10, 15, 20, 25, 30]

RELIABILITY_OPTIONS = [
    ("25% — muy incierto", 25),
    ("50% — la mitad de los meses", 50),
    ("75% — casi siempre llega", 75),
    ("100% — prácticamente fijo", 100),
]


def _fmt_cop(n: int) -> str:
    return f"${n:,.0f}".replace(",", ".")


def _parse_monto(text: str) -> int | None:
    normalized = text.strip().lower().replace("$", "").replace(".", "").replace(",", "")
    try:
        if normalized.endswith("m"):
            return int(float(normalized[:-1]) * 1_000_000)
        if normalized.endswith("k"):
            return int(float(normalized[:-1]) * 1_000)
        value = int(float(re.sub(r"[^\d.]", "", normalized) or "0"))
        return value if value > 0 else None
    except (ValueError, AttributeError):
        return None


def _day_window(day: int) -> tuple[int, int]:
    return max(1, day - 2), min(31, day + 2)


def _build_schedules(cadence: str, *, amount: int, window_key: str | None = None, day_1: int | None = None, day_2: int | None = None) -> list[dict]:
    if cadence == "biweekly":
        first = day_1 or 5
        second = day_2 or 20
        day_from_1, day_to_1 = _day_window(first)
        day_from_2, day_to_2 = _day_window(second)
        return [
            {
                "ordinal": 1,
                "label": "Quincena 1",
                "expected_day_from": day_from_1,
                "expected_day_to": day_to_1,
                "expected_amount": amount,
            },
            {
                "ordinal": 2,
                "label": "Quincena 2",
                "expected_day_from": day_from_2,
                "expected_day_to": day_to_2,
                "expected_amount": amount,
            },
        ]

    if cadence == "weekly":
        weekly_windows = [
            ("Semana 1", 1, 7),
            ("Semana 2", 8, 14),
            ("Semana 3", 15, 21),
            ("Semana 4", 22, 31),
        ]
        schedules: list[dict] = []
        for index, (label, day_from, day_to) in enumerate(weekly_windows, start=1):
            schedules.append(
                {
                    "ordinal": index,
                    "label": label,
                    "expected_day_from": day_from,
                    "expected_day_to": day_to,
                    "expected_amount": amount,
                }
            )
        return schedules

    if cadence == "monthly":
        selected = next((item for item in MONTHLY_WINDOWS if item[1] == window_key), MONTHLY_WINDOWS[3])
        return [
            {
                "ordinal": 1,
                "label": selected[0],
                "expected_day_from": selected[2],
                "expected_day_to": selected[3],
                "expected_amount": amount,
            }
        ]

    return [
        {
            "ordinal": 1,
            "label": "Mes completo",
            "expected_day_from": 1,
            "expected_day_to": 31,
            "expected_amount": amount,
        }
    ]


def _summary_range(schedules: list[dict]) -> tuple[int, int]:
    return (
        min(int(schedule["expected_day_from"]) for schedule in schedules),
        max(int(schedule["expected_day_to"]) for schedule in schedules),
    )


def _monthly_total_from_amount(cadence: str, amount: int) -> int:
    if cadence == "biweekly":
        return amount * 2
    if cadence == "weekly":
        return amount * 4
    return amount


def _amount_prompt(cadence: str, *, variable: bool = False) -> str:
    subject = "ese ingreso variable" if variable else "ese ingreso"
    if cadence == "biweekly":
        return f"¿Cuánto recibís por <b>quincena</b> de {subject}?\nEjemplo: <code>3200000</code> o <code>3.2M</code>"
    if cadence == "weekly":
        return f"¿Cuánto recibís por <b>semana</b> de {subject}?\nEjemplo: <code>800000</code> o <code>800k</code>"
    if cadence == "irregular":
        return f"¿Cuánto suele llegar cuando entra {subject}?\nEjemplo: <code>2900000</code> o <code>2.9M</code>"
    return f"¿Cuánto recibís de {subject} en total al <b>mes</b>?\nEjemplo: <code>6400000</code> o <code>6.4M</code>"


def _profile_summary(name: str, amount: int, cadence: str, schedules: list[dict], reliability: int | None = None) -> str:
    monthly_total = _monthly_total_from_amount(cadence, amount)
    amount_line = f"<b>{name}</b> — {_fmt_cop(amount)}"
    if cadence in {"biweekly", "weekly"}:
        amount_line = f"<b>{name}</b> — {_fmt_cop(amount)} por {'quincena' if cadence == 'biweekly' else 'semana'}"

    lines = [
        amount_line,
        f"Cadencia: <b>{cadence}</b>",
        f"Total mensual esperado: <b>{_fmt_cop(monthly_total)}</b>",
    ]
    if reliability is not None:
        lines.append(f"Confiabilidad: <b>{reliability}%</b>")
    lines.append("Ventanas:")
    lines.extend(
        f"• {schedule['label']}: {schedule['expected_day_from']}-{schedule['expected_day_to']} por {_fmt_cop(int(schedule['expected_amount']))}"
        for schedule in schedules
    )
    return "\n".join(lines)


def trigger(api: RailsApiPort, messenger: MessengerPort, reason: str | None = None) -> dict | None:
    existing = api.get_active_pending_action()
    if existing and existing.get("action_type") == "income_setup":
        if reason:
            messenger.send_message(reason)
        _go_to_step(api, messenger, existing["id"], existing.get("context", {}), existing.get("current_step") or 1)
        return existing

    action = api.create_pending_action(
        action_type="income_setup",
        total_steps=10,
        context={},
    )
    if reason:
        messenger.send_message(reason)
    _go_to_step(api, messenger, action["id"], {}, 1)
    return action


def handle_update(
    api: RailsApiPort,
    messenger: MessengerPort,
    pending_action: dict,
    update_type: str,
    payload: dict,
    callback_query_id: str | None = None,
) -> None:
    step = pending_action.get("current_step", 1)
    ctx = pending_action.get("context") or {}
    pa_id = pending_action["id"]

    if callback_query_id:
        messenger.answer_callback(callback_query_id, "✅")

    if update_type == "callback_query":
        _handle_callback(api, messenger, pa_id, step, ctx, payload.get("data", ""))
    elif update_type == "message":
        _handle_message(api, messenger, pa_id, step, ctx, payload.get("text", "").strip())


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

    if key.startswith("base_cadence:"):
        cadence = key.split(":")[1]
        ctx["base_cadence"] = cadence
        _go_to_step(api, messenger, pa_id, ctx, 3)
        return

    if key.startswith("var_cadence:"):
        cadence = key.split(":")[1]
        ctx["var_cadence"] = cadence
        _go_to_step(api, messenger, pa_id, ctx, 8)
        return

    if key.startswith("base_window:"):
        ctx["base_window_key"] = key.split(":")[1]
        _go_to_step(api, messenger, pa_id, ctx, 5)
        return

    if key.startswith("var_window:"):
        ctx["var_window_key"] = key.split(":")[1]
        _go_to_step(api, messenger, pa_id, ctx, 10)
        return

    if key.startswith("base_day1:"):
        ctx["base_day_1"] = int(key.split(":")[1])
        _go_to_step(api, messenger, pa_id, ctx, 5)
        return

    if key.startswith("base_day2:"):
        ctx["base_day_2"] = int(key.split(":")[1])
        _go_to_step(api, messenger, pa_id, ctx, 5)
        return

    if key.startswith("var_day1:"):
        ctx["var_day_1"] = int(key.split(":")[1])
        _go_to_step(api, messenger, pa_id, ctx, 10)
        return

    if key.startswith("var_day2:"):
        ctx["var_day_2"] = int(key.split(":")[1])
        _go_to_step(api, messenger, pa_id, ctx, 10)
        return

    if key == "var:yes":
        _go_to_step(api, messenger, pa_id, ctx, 6)
        return

    if key == "var:no":
        _save_profiles(api, messenger, pa_id, ctx, include_variable=False)
        return

    if key.startswith("rel:"):
        ctx["var_reliability"] = int(key.split(":")[1])
        _save_profiles(api, messenger, pa_id, ctx, include_variable=True)
        return

    if key == "var:skip":
        _save_profiles(api, messenger, pa_id, ctx, include_variable=False)
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
        return

    if step == 3:
        amount = _parse_monto(text)
        if amount:
            ctx["base_amount"] = amount
            next_step = _next_schedule_step("base", ctx.get("base_cadence", "monthly"))
            _go_to_step(api, messenger, pa_id, ctx, next_step)
        else:
            messenger.send_message("No entendí el monto. Escríbelo como <code>6400000</code> o <code>6.4M</code>.")
        return

    if step == 6:
        ctx["var_name"] = text
        _go_to_step(api, messenger, pa_id, ctx, 7)
        return

    if step == 8:
        amount = _parse_monto(text)
        if amount:
            ctx["var_amount"] = amount
            next_step = _next_schedule_step("var", ctx.get("var_cadence", "monthly"))
            _go_to_step(api, messenger, pa_id, ctx, next_step)
        else:
            messenger.send_message("No entendí el monto. Escríbelo como <code>2900000</code> o <code>2.9M</code>.")
        return

    messenger.send_message("Usá los botones o responde el dato solicitado para avanzar.")


def _next_schedule_step(prefix: str, cadence: str) -> int:
    if prefix == "base":
        if cadence == "monthly":
            return 4
        if cadence == "biweekly":
            return 4
        return 5

    if cadence == "monthly":
        return 9
    if cadence == "biweekly":
        return 9
    return 10


def _go_to_step(api: RailsApiPort, messenger: MessengerPort, pa_id: int | str, ctx: dict, step: int) -> None:
    api.update_pending_action(pa_id, current_step=step, context=ctx)

    if step == 1:
        messenger.send_message(
            "Vamos a registrar tus ingresos para calcular el presupuesto real.\n\n"
            "<b>¿Cómo se llama tu ingreso más seguro?</b>\n"
            "Ejemplo: <i>Salario empresa</i>"
        )
        return

    if step == 2:
        messenger.send_with_buttons(
            text=(
                f"<b>{ctx.get('base_name', 'Ingreso base')}</b>\n\n"
                "¿Cada cuánto llega?"
            ),
            buttons=[
                [{"text": "Mensual", "callback_data": "wi:base_cadence:monthly"}],
                [{"text": "Quincenal", "callback_data": "wi:base_cadence:biweekly"}],
                [{"text": "Semanal", "callback_data": "wi:base_cadence:weekly"}],
                [{"text": "Irregular", "callback_data": "wi:base_cadence:irregular"}],
            ],
        )
        return

    if step == 3:
        messenger.send_message(
            _amount_prompt(ctx.get("base_cadence", "monthly"))
        )
        return

    if step == 4:
        if ctx.get("base_cadence") == "monthly":
            messenger.send_with_buttons(
                text="¿En qué parte del mes suele llegar ese ingreso base?",
                buttons=[[{"text": label, "callback_data": f"wi:base_window:{key}"}] for label, key, _, _ in MONTHLY_WINDOWS],
            )
            return

        messenger.send_with_buttons(
            text="Seleccioná el <b>primer día de pago</b> aproximado de ese ingreso quincenal.",
            buttons=[[{"text": f"Día {day}", "callback_data": f"wi:base_day1:{day}"}] for day in DAY_OPTIONS],
        )
        return

    if step == 5:
        if ctx.get("base_cadence") == "biweekly" and not ctx.get("base_day_2"):
            messenger.send_with_buttons(
                text="Seleccioná el <b>segundo día de pago</b> aproximado.",
                buttons=[[{"text": f"Día {day}", "callback_data": f"wi:base_day2:{day}"}] for day in DAY_OPTIONS],
            )
            return

        messenger.send_with_buttons(
            text="¿Tenés además algún <b>ingreso variable o extra</b>?",
            buttons=[
                [{"text": "Sí, agregar uno", "callback_data": "wi:var:yes"}],
                [{"text": "No, solo ese", "callback_data": "wi:var:no"}],
            ],
        )
        return

    if step == 6:
        messenger.send_message(
            "¿Cómo se llama ese ingreso variable?\n"
            "Ejemplo: <i>Freelance cliente</i>"
        )
        return

    if step == 7:
        messenger.send_with_buttons(
            text=(
                f"<b>{ctx.get('var_name', 'Ingreso variable')}</b>\n\n"
                "¿Cada cuánto llega?"
            ),
            buttons=[
                [{"text": "Mensual", "callback_data": "wi:var_cadence:monthly"}],
                [{"text": "Quincenal", "callback_data": "wi:var_cadence:biweekly"}],
                [{"text": "Semanal", "callback_data": "wi:var_cadence:weekly"}],
                [{"text": "Irregular", "callback_data": "wi:var_cadence:irregular"}],
            ],
        )
        return

    if step == 8:
        messenger.send_message(
            _amount_prompt(ctx.get("var_cadence", "irregular"), variable=True)
        )
        return

    if step == 9:
        if ctx.get("var_cadence") == "monthly":
            messenger.send_with_buttons(
                text="¿En qué parte del mes suele llegar ese ingreso variable?",
                buttons=[[{"text": label, "callback_data": f"wi:var_window:{key}"}] for label, key, _, _ in MONTHLY_WINDOWS],
            )
            return

        messenger.send_with_buttons(
            text="Seleccioná el <b>primer día</b> aproximado de llegada para ese ingreso variable.",
            buttons=[[{"text": f"Día {day}", "callback_data": f"wi:var_day1:{day}"}] for day in DAY_OPTIONS],
        )
        return

    if step == 10:
        if ctx.get("var_cadence") == "biweekly" and not ctx.get("var_day_2"):
            messenger.send_with_buttons(
                text="Seleccioná el <b>segundo día</b> aproximado de llegada.",
                buttons=[[{"text": f"Día {day}", "callback_data": f"wi:var_day2:{day}"}] for day in DAY_OPTIONS],
            )
            return

        messenger.send_with_buttons(
            text="¿Qué tan seguido llega ese ingreso variable?",
            buttons=[
                [{"text": label, "callback_data": f"wi:rel:{pct}"}]
                for label, pct in RELIABILITY_OPTIONS
            ] + [[{"text": "Mejor lo omito", "callback_data": "wi:var:skip"}]],
        )
        return

    logger.error("[income_wizard] step desconocido: %d", step)


def _build_payload(prefix: str, ctx: dict, classification: str, reliability: int) -> dict:
    amount = int(ctx[f"{prefix}_amount"])
    cadence = ctx[f"{prefix}_cadence"]
    schedules = _build_schedules(
        cadence,
        amount=amount,
        window_key=ctx.get(f"{prefix}_window_key"),
        day_1=ctx.get(f"{prefix}_day_1"),
        day_2=ctx.get(f"{prefix}_day_2"),
    )
    day_from, day_to = _summary_range(schedules)
    return {
        "name": ctx[f"{prefix}_name"],
        "expected_amount": _monthly_total_from_amount(cadence, amount),
        "expected_day_from": day_from,
        "expected_day_to": day_to,
        "classification": classification,
        "cadence": cadence,
        "reliability_score": reliability,
        "schedules": schedules,
        "is_variable": classification != "base",
    }


def _save_profiles(api: RailsApiPort, messenger: MessengerPort, pa_id: int | str, ctx: dict, *, include_variable: bool) -> None:
    try:
        base_payload = _build_payload("base", ctx, "base", 100)
        api.create_income_source(**base_payload)

        response_lines = [
            "✅ <b>Perfil de ingresos guardado</b>",
            "",
            _profile_summary(
                base_payload["name"],
                int(ctx["base_amount"]),
                str(base_payload["cadence"]),
                list(base_payload["schedules"]),
            ),
        ]

        if include_variable and ctx.get("var_name") and ctx.get("var_amount") and ctx.get("var_cadence"):
            reliability = int(ctx.get("var_reliability", 50))
            variable_payload = _build_payload("var", ctx, "variable", reliability)
            api.create_income_source(**variable_payload)
            response_lines.extend(
                [
                    "",
                    _profile_summary(
                        variable_payload["name"],
                        int(ctx["var_amount"]),
                        str(variable_payload["cadence"]),
                        list(variable_payload["schedules"]),
                        reliability,
                    ),
                ]
            )

        api.update_pending_action(pa_id, status="completed")
        response_lines.extend(
            [
                "",
                "El sistema ya puede calcular tu presupuesto mensual real.",
                "Cuando quieras ajustar esto, escríbeme <code>mis ingresos</code>.",
            ]
        )
        messenger.send_message("\n".join(response_lines))
    except Exception as e:
        logger.error("[income_wizard] error guardando ingresos: %s", e)
        messenger.send_message(f"❌ Error al guardar el perfil de ingresos: {e}. Inténtalo de nuevo.")
