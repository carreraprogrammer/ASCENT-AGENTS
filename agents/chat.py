"""
agents/chat.py — Agente conversacional en tiempo real.

Responsabilidades:
- interpretar mensajes de Telegram en tiempo real
- registrar / corregir / borrar transacciones con tools
- pedir aclaraciones cortas cuando falte contexto
- delegar la idempotencia técnica al backend mediante source_event_id
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx

from adapters.rails_http import BASE_URL as API_BASE_URL, build_auth_headers
from ports.messenger import MessengerPort, ParsedUpdate
from ports.rails_api import RailsApiPort
from services.claude_client import run_agent

logger = logging.getLogger(__name__)

COLOMBIA_TZ = timezone(timedelta(hours=-5))
API_URL = API_BASE_URL
CHAT_MODEL = "claude-sonnet-4-6"

SYSTEM_PROMPT = """\
Sos el asistente financiero personal de Daniel.

Tu trabajo es resolver en tiempo real lo que Daniel pide por Telegram:
- registrar gastos o ingresos
- corregir transacciones recientes
- borrar transacciones
- responder métricas o estado financiero
- activar el wizard de contexto financiero si lo pide

Reglas:
- Usá solo datos reales de la API.
- Sé muy conciso. Idealmente 1 o 2 frases. Nunca más de 4 líneas.
- No muestres tu proceso de razonamiento.
- No digas "voy a", "entendí", "paso 1", ni expliques herramientas.
- Cuando falte contexto, preguntá una sola cosa por vez.
- Si la aclaración cabe en 2 o 3 opciones, preferí send_telegram con inline_keyboard.
- Para Telegram usá texto plano o HTML simple (<b>, <i>). No uses markdown tipo **texto**.
- Cuando hables de plata, formateá en pesos colombianos.
- Si el mensaje describe un gasto o ingreso claro, actuá de una vez.
- Si el usuario quiere corregir o borrar "ese gasto", usá transacciones recientes para inferir a cuál se refiere.
- La deduplicación semántica vive en vos: decidí si corresponde crear, actualizar, ignorar o preguntar.
- La idempotencia técnica vive en el backend: no intentes deduplicar por date+amount en tus tools.
- Si el usuario habla de mover plata entre cuentas propias, eso NO es ingreso ni gasto. No lo registres.
- Si el usuario pide que algo no cuente para el análisis nocturno, no inventes una transacción para eso.
- Para crear o actualizar transacciones:
  - la API espera date en DD/MM/YYYY o DD/MM
  - no uses YYYY-MM-DD
- Al final usá send_telegram una sola vez.
"""

_COMMAND_PROMPTS = {
    "resumen": (
        "Necesito un resumen ejecutivo de mi situación financiera de este mes. "
        "Consultá el summary y devolveme solo lo importante."
    ),
    "presupuesto": (
        "Mostrame cómo voy con mis presupuestos este mes, categoría por categoría, "
        "con alertas claras si voy mal."
    ),
    "deudas": (
        "Resumime el estado actual de mis deudas, saldos, cuotas y estrategia."
    ),
    "balance": (
        "Decime cuánto tengo disponible ahora mismo con ingresos y gastos reales."
    ),
}

_HELP_TEXT = """\
📊 <b>Comandos disponibles</b>

/resumen — Resumen del mes
/presupuesto — Estado de presupuestos
/deudas — Estado de deudas
/balance — Saldo disponible

También podés escribirme normal:
• "pollo 14000"
• "olvidá ese gasto"
• "corregí ese gasto, eran tamales"
• "configurar contexto financiero"
"""


def _normalize_telegram_html(text: str) -> str:
    if not text:
        return text

    normalized = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    normalized = re.sub(r"__(.+?)__", r"<i>\1</i>", normalized)
    return normalized


def _flatten_transaction(raw: dict) -> dict:
    attributes = raw.get("attributes", raw)
    relationships = raw.get("relationships", {})
    category_ref = relationships.get("category", {}).get("data")
    subcategory_ref = relationships.get("subcategory", {}).get("data")

    return {
        "id": raw.get("id"),
        "date": attributes.get("date"),
        "concept": attributes.get("concept"),
        "product": attributes.get("product"),
        "amount": attributes.get("amount"),
        "transaction_type": attributes.get("transaction_type"),
        "status": attributes.get("status"),
        "source": attributes.get("source"),
        "year": attributes.get("year"),
        "month": attributes.get("month"),
        "metadata": attributes.get("metadata") or {},
        "category_id": category_ref["id"] if category_ref else None,
        "subcategory_id": subcategory_ref["id"] if subcategory_ref else None,
    }


def _parse_api_date(raw_date: str | None, fallback_year: int | None = None) -> date | None:
    if not raw_date:
        return None

    value = str(raw_date).strip()
    if not value:
        return None

    try:
        return date.fromisoformat(value)
    except ValueError:
        pass

    parts = value.split("/")
    if len(parts) == 3:
        try:
            day, month, year = map(int, parts)
            return date(year, month, day)
        except ValueError:
            return None

    if len(parts) == 2 and fallback_year:
        try:
            day, month = map(int, parts)
            return date(fallback_year, month, day)
        except ValueError:
            return None

    return None


def _event_source_id(parsed: ParsedUpdate) -> str | None:
    raw = parsed.raw or {}
    message = raw.get("message") or {}

    message_id = message.get("message_id")
    if message_id is not None:
        return f"telegram:message:{message_id}"

    update_id = raw.get("update_id")
    if update_id is not None:
        return f"telegram:update:{update_id}"

    return None


def _telegram_context(parsed: ParsedUpdate) -> str:
    message_payload = parsed.raw.get("message", {}) if parsed.raw else {}
    telegram_ts = message_payload.get("date")

    if telegram_ts:
        ts_col = datetime.fromtimestamp(telegram_ts, tz=timezone.utc).astimezone(COLOMBIA_TZ)
        return (
            f"Fecha real del mensaje: {ts_col.strftime('%d/%m/%Y')}\n"
            f"Hora real en Colombia: {ts_col.strftime('%H:%M')}\n"
            f"source_event_id técnico: {_event_source_id(parsed) or 'no-disponible'}\n"
            "Si el usuario no especifica fecha, usá esa fecha del mensaje. "
            "Si creás una transacción, mandá date como DD/MM/YYYY."
        )

    now_col = datetime.now(COLOMBIA_TZ)
    return (
        f"Fecha actual en Colombia: {now_col.strftime('%d/%m/%Y')}\n"
        f"Hora actual en Colombia: {now_col.strftime('%H:%M')}\n"
        f"source_event_id técnico: {_event_source_id(parsed) or 'no-disponible'}\n"
        "Si el usuario no especifica fecha, usá la fecha actual en Colombia. "
        "Si creás una transacción, mandá date como DD/MM/YYYY."
    )


def _build_tools() -> list[dict[str, Any]]:
    return [
        {
            "name": "get_summary",
            "description": "Resumen financiero del mes: balance, burn rate, deudas y contexto.",
            "input_schema": {
                "type": "object",
                "properties": {"month": {"type": "integer"}, "year": {"type": "integer"}},
                "required": ["month", "year"],
            },
        },
        {
            "name": "get_transactions",
            "description": "Transacciones del mes.",
            "input_schema": {
                "type": "object",
                "properties": {"month": {"type": "integer"}, "year": {"type": "integer"}},
                "required": ["month", "year"],
            },
        },
        {
            "name": "get_recent_transactions",
            "description": "Transacciones recientes. Úsalo para corregir o borrar 'ese gasto'.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "minimum": 1, "maximum": 31},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                },
                "required": [],
            },
        },
        {
            "name": "get_categories",
            "description": "Categorías y subcategorías disponibles.",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "get_budgets",
            "description": "Presupuestos del mes.",
            "input_schema": {
                "type": "object",
                "properties": {"month": {"type": "integer"}, "year": {"type": "integer"}},
                "required": ["month", "year"],
            },
        },
        {
            "name": "get_debts",
            "description": "Deudas activas.",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "get_balance",
            "description": "Saldo disponible actual.",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "get_financial_context",
            "description": "Contexto financiero actual.",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "get_income_sources",
            "description": "Fuentes de ingreso.",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "get_recurring_obligations",
            "description": "Gastos recurrentes.",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "create_transaction",
            "description": (
                "Crea una transacción. Para Telegram usa source=telegram. "
                "No inventes source_event_id: el sistema lo inyecta automáticamente. "
                "La API espera date en DD/MM/YYYY o DD/MM."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "date": {"type": "string"},
                    "concept": {"type": "string"},
                    "product": {"type": "string"},
                    "amount": {"type": "integer"},
                    "transaction_type": {"type": "string", "enum": ["expense", "income"]},
                    "status": {"type": "string", "enum": ["confirmed", "pending", "projected"]},
                    "category_id": {"type": "integer"},
                    "subcategory_id": {"type": "integer"},
                    "category_code": {"type": "string"},
                    "subcategory_code": {"type": "string"},
                    "source": {"type": "string", "enum": ["telegram", "gmail", "manual"]},
                    "metadata": {"type": "object"},
                },
                "required": ["date", "concept", "amount", "transaction_type", "status"],
            },
        },
        {
            "name": "update_transaction",
            "description": "Corrige una transacción existente por ID.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "date": {"type": "string"},
                    "concept": {"type": "string"},
                    "product": {"type": "string"},
                    "amount": {"type": "integer"},
                    "status": {"type": "string", "enum": ["confirmed", "pending", "projected"]},
                    "category_id": {"type": "integer"},
                    "subcategory_id": {"type": "integer"},
                    "category_code": {"type": "string"},
                    "subcategory_code": {"type": "string"},
                    "clarification_resolved_at": {"type": "string"},
                    "metadata": {"type": "object"},
                },
                "required": ["id"],
            },
        },
        {
            "name": "delete_transaction",
            "description": "Elimina una transacción por ID.",
            "input_schema": {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        },
        {
            "name": "update_financial_context",
            "description": "Actualiza el contexto financiero.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "phase": {
                        "type": "string",
                        "enum": ["debt_payoff", "emergency_fund", "investing", "wealth_building"],
                    },
                    "strategy": {"type": "string", "enum": ["snowball", "avalanche"]},
                    "reward_pct": {"type": "integer", "minimum": 1, "maximum": 100},
                    "notes": {"type": "string"},
                },
                "required": [],
            },
        },
        {
            "name": "trigger_financial_context_wizard",
            "description": "Activa el wizard guiado de contexto financiero en Telegram.",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "update_debt",
            "description": "Actualiza una deuda.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "current_balance": {"type": "integer"},
                    "monthly_payment": {"type": "integer"},
                    "interest_rate": {"type": "number"},
                    "status": {"type": "string", "enum": ["active", "paid_off", "paused", "disputed"]},
                    "notes": {"type": "string"},
                },
                "required": ["id"],
            },
        },
        {
            "name": "delete_debt",
            "description": "Elimina una deuda por ID.",
            "input_schema": {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        },
        {
            "name": "create_recurring_obligation",
            "description": "Crea un gasto fijo recurrente.",
            "input_schema": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "amount": {"type": "integer"}, "notes": {"type": "string"}},
                "required": ["name", "amount"],
            },
        },
        {
            "name": "update_recurring_obligation",
            "description": "Actualiza un gasto fijo recurrente.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                    "amount": {"type": "integer"},
                    "active": {"type": "boolean"},
                    "notes": {"type": "string"},
                },
                "required": ["id"],
            },
        },
        {
            "name": "delete_recurring_obligation",
            "description": "Desactiva un gasto fijo recurrente.",
            "input_schema": {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        },
        {
            "name": "send_telegram",
            "description": (
                "Envía la respuesta final. Soporta inline_keyboard. "
                "Para callbacks rápidos usa chat:... como callback_data."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "message": {"type": "string"},
                    "mensaje": {"type": "string"},
                    "inline_keyboard": {
                        "type": "array",
                        "items": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "text": {"type": "string"},
                                    "callback_data": {"type": "string"},
                                },
                                "required": ["text", "callback_data"],
                            },
                        },
                    },
                },
                "required": [],
            },
        },
    ]


def _send_telegram(messenger: MessengerPort, payload: dict) -> dict:
    message = payload.get("message") or payload.get("mensaje") or ""
    normalized = _normalize_telegram_html(message)

    if payload.get("inline_keyboard"):
        messenger.send_with_buttons(normalized, payload["inline_keyboard"])
    else:
        messenger.send_message(normalized)

    return {"ok": True}


def _build_tool_map(
    api: RailsApiPort,
    messenger: MessengerPort,
    now: datetime,
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    month, year = now.month, now.year
    today = now.date()
    state = state or {"responded": False, "mutated": False, "source_event_id": None}

    def _patch(path: str, body: dict) -> dict:
        response = httpx.patch(
            f"{API_URL}{path}",
            headers=build_auth_headers(),
            json=body,
            timeout=15,
        )
        response.raise_for_status()
        state["mutated"] = True
        return response.json().get("data", {})

    def _delete(path: str) -> dict:
        response = httpx.delete(
            f"{API_URL}{path}",
            headers=build_auth_headers(),
            timeout=15,
        )
        response.raise_for_status()
        state["mutated"] = True
        return {"ok": True}

    def _post(path: str, body: dict) -> dict:
        response = httpx.post(
            f"{API_URL}{path}",
            headers=build_auth_headers(),
            json=body,
            timeout=15,
        )
        response.raise_for_status()
        state["mutated"] = True
        return response.json().get("data", {})

    def _normalize_categories() -> list[dict]:
        categories: list[dict] = []
        for raw in api.get_categories():
            attributes = raw.get("attributes", raw)
            subcategories = raw.get("relationships", {}).get("subcategories", {}).get("data", [])
            categories.append(
                {
                    "id": raw.get("id"),
                    "name": attributes.get("name"),
                    "code": attributes.get("code"),
                    "category_type": attributes.get("category_type"),
                    "subcategories": [
                        {
                            "id": sub.get("id"),
                            "name": sub.get("attributes", {}).get("name"),
                            "code": sub.get("attributes", {}).get("code"),
                        }
                        for sub in subcategories
                    ],
                }
            )
        return categories

    def _get_recent_transactions(input_data: dict) -> dict:
        days = int(input_data.get("days", 7))
        limit = int(input_data.get("limit", 10))
        cutoff = today - timedelta(days=max(days - 1, 0))
        month_keys = {(today.year, today.month), (cutoff.year, cutoff.month)}

        rows: list[dict] = []
        for tx_year, tx_month in month_keys:
            try:
                rows.extend(api.get_transactions(tx_month, tx_year))
            except Exception as exc:
                logger.warning(
                    "[chat_agent] recent transactions fetch failed for %s-%s: %s",
                    tx_year,
                    tx_month,
                    exc,
                )

        flattened: list[dict] = []
        for row in rows:
            flat = _flatten_transaction(row)
            tx_date = _parse_api_date(flat.get("date"), fallback_year=flat.get("year"))
            if not tx_date or tx_date < cutoff:
                continue

            flat["_sort_key"] = (
                tx_date.isoformat(),
                flat.get("id") or "",
            )
            flattened.append(flat)

        flattened.sort(key=lambda item: item["_sort_key"], reverse=True)
        for item in flattened:
            item.pop("_sort_key", None)

        recent = flattened[:limit]
        return {"transactions": recent, "total": len(recent), "days": days}

    def _create_transaction(input_data: dict) -> dict:
        payload = dict(input_data)
        metadata = dict(payload.get("metadata") or {})

        if payload.get("source") == "telegram" and state.get("source_event_id"):
            metadata["source_event_id"] = state["source_event_id"]

        if metadata:
            payload["metadata"] = metadata

        return _post("/api/v1/transactions", payload)

    def _trigger_fc_wizard(_: dict) -> dict:
        from flows import financial_context_wizard

        financial_context_wizard.trigger(api, messenger)
        return {"ok": True, "message": "Wizard iniciado en Telegram."}

    return {
        "get_summary": lambda p: api.get_summary(p.get("month", month), p.get("year", year)),
        "get_transactions": lambda p: api.get_transactions(p.get("month", month), p.get("year", year)),
        "get_recent_transactions": _get_recent_transactions,
        "get_categories": lambda _: {"categories": _normalize_categories()},
        "get_budgets": lambda p: api.get_budgets(p.get("month", month), p.get("year", year)),
        "get_debts": lambda _: api.get_debts(),
        "get_balance": lambda _: api.get_balance(),
        "get_financial_context": lambda _: api.get_financial_context(),
        "get_income_sources": lambda _: api.get_income_sources(),
        "get_recurring_obligations": lambda _: api.get_recurring_obligations(),
        "create_transaction": _create_transaction,
        "update_transaction": lambda p: _patch(f"/api/v1/transactions/{p.pop('id')}", p),
        "delete_transaction": lambda p: _delete(f"/api/v1/transactions/{p['id']}"),
        "update_financial_context": lambda p: api.update_financial_context(**p),
        "trigger_financial_context_wizard": _trigger_fc_wizard,
        "update_debt": lambda p: _patch(f"/api/v1/debts/{p.pop('id')}", p),
        "delete_debt": lambda p: _delete(f"/api/v1/debts/{p['id']}"),
        "create_recurring_obligation": lambda p: _post("/api/v1/recurring_obligations", p),
        "update_recurring_obligation": lambda p: _patch(f"/api/v1/recurring_obligations/{p.pop('id')}", p),
        "delete_recurring_obligation": lambda p: _delete(f"/api/v1/recurring_obligations/{p['id']}"),
        "send_telegram": lambda p: state.update({"responded": True}) or _send_telegram(messenger, p),
    }


def _run_conversation(
    api: RailsApiPort,
    messenger: MessengerPort,
    initial_message: str,
    source_event_id: str | None = None,
) -> None:
    now_col = datetime.now(COLOMBIA_TZ)
    state = {"responded": False, "mutated": False, "source_event_id": source_event_id}
    final_text = run_agent(
        system_prompt=SYSTEM_PROMPT,
        tools=_build_tools(),
        tool_map=_build_tool_map(api, messenger, now_col, state),
        initial_message=initial_message,
        max_iterations=12,
        model=CHAT_MODEL,
    )

    if state["responded"]:
        return

    if final_text:
        messenger.send_message(_normalize_telegram_html(final_text))
        return

    if state["mutated"]:
        messenger.send_message("✅ Listo.")
        return

    messenger.send_message("⚠️ No pude cerrar bien la respuesta. Intentá de nuevo.")


def handle_command(api: RailsApiPort, messenger: MessengerPort, parsed: ParsedUpdate) -> None:
    command = parsed.command or ""

    if command not in _COMMAND_PROMPTS:
        messenger.send_message(f"❓ No conozco el comando <code>/{command}</code>.\n\n{_HELP_TEXT}")
        return

    try:
        _run_conversation(api, messenger, _COMMAND_PROMPTS[command], source_event_id=None)
    except Exception as exc:
        logger.error("[chat_agent] command error: %s", exc, exc_info=True)
        messenger.send_message("❌ Tuve un problema. Intentá de nuevo.")


def handle_message(api: RailsApiPort, messenger: MessengerPort, parsed: ParsedUpdate) -> None:
    text = (parsed.text or "").strip()
    if not text:
        return

    initial_message = (
        "Mensaje nuevo de Daniel en Telegram. "
        "Interpretalo y actuá en tiempo real usando las herramientas disponibles. "
        "Si es un gasto o ingreso claro, registralo. "
        "Si es una corrección o borrado, usá transacciones recientes para resolverlo. "
        "Si hay ambigüedad real, pedí una aclaración breve.\n\n"
        f"{_telegram_context(parsed)}\n\n"
        f"Mensaje: {text}"
    )

    try:
        _run_conversation(api, messenger, initial_message, source_event_id=_event_source_id(parsed))
    except Exception as exc:
        logger.error("[chat_agent] realtime error: %s", exc, exc_info=True)
        messenger.send_message("❌ No pude procesar eso. Intentá de nuevo.")
