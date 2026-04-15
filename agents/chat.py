"""
agents/chat.py — Agente conversacional en tiempo real.

Se activa cuando el usuario envía un slash command (/resumen, /deudas, /chat …).
Tiene acceso completo: puede leer, escribir y activar wizards.
Si explota, el agente nocturno corre igual esa noche.
"""

import logging
import os
import httpx
from datetime import datetime, timezone, timedelta

from adapters.rails_http import BASE_URL as API_BASE_URL, build_auth_headers
from ports.rails_api import RailsApiPort
from ports.messenger import MessengerPort, ParsedUpdate
from services.claude_client import run_agent

logger = logging.getLogger(__name__)

COLOMBIA_TZ = timezone(timedelta(hours=-5))

API_URL   = API_BASE_URL

SYSTEM_PROMPT = """\
Sos el asistente financiero personal de Daniel, un coach financiero real que
habla con naturalidad colombiana (vos / sos / bacano / parce). No sos un bot
corporativo — sos directo, honesto y sin rodeos.

Tu trabajo es responder lo que Daniel pide: consultar datos, hacer cambios,
o activar wizards de configuración.

Reglas:
- Usá los datos reales de la API. Nada inventado.
- Sé conciso: Telegram, no PDF. Máximo 3-4 párrafos o una lista corta.
- Cuando hables de plata, siempre en pesos colombianos formateados ($1.500.000).
- No repitas números crudos — interpretálos.
- Podés usar emojis con moderación (📊 ✅ ⚠️ 💰).
- Al final usá `send_telegram` para enviar la respuesta. Solo una vez.
- Para cambios importantes (borrar una deuda, cambiar contexto financiero),
  confirmá brevemente antes de ejecutar si el pedido no es explícito.
- Si el usuario pide configurar el contexto financiero, usá `trigger_financial_context_wizard`.
"""

_COMMAND_PROMPTS = {
    "resumen": (
        "Necesito el resumen de mi situación financiera de este mes. "
        "Consultá el summary, analizá el burn rate y las alertas, y mandame "
        "un resumen ejecutivo con lo más importante."
    ),
    "presupuesto": (
        "¿Cómo voy con mis presupuestos este mes? Mostrámelos categoría por "
        "categoría con cuánto gasté vs el límite y si voy bien o mal."
    ),
    "deudas": (
        "¿Cómo están mis deudas? Resúmeme los saldos actuales, las cuotas "
        "mensuales y si voy bien con la estrategia de pago."
    ),
    "balance": (
        "¿Cuánto tengo disponible ahora mismo? Dame el balance actual con "
        "ingresos confirmados vs gastos confirmados y proyectados."
    ),
}

_HELP_TEXT = """\
📊 <b>Comandos disponibles</b>

/resumen — Resumen del mes con burn rate y alertas
/presupuesto — Cómo vas categoría por categoría
/deudas — Estado de tus deudas y estrategia de pago
/balance — Saldo disponible ahora mismo
/chat <pregunta> — Cualquier pregunta o acción sobre tus finanzas

Ejemplos de lo que podés pedirme:
• "actualizá el saldo del Préstamo Personal a $28.500.000"
• "configurar contexto financiero"
• "agregá un gasto fijo: Spotify $21.900"
• "borrá la deuda Préstamo familiar"
"""


def handle_command(
    api: RailsApiPort,
    messenger: MessengerPort,
    parsed: ParsedUpdate,
) -> None:
    command = parsed.command or ""
    args    = parsed.command_args

    if command == "chat" and not args:
        messenger.send_message(_HELP_TEXT)
        return

    if command not in _COMMAND_PROMPTS and command != "chat":
        messenger.send_message(
            f"❓ No conozco el comando <code>/{command}</code>.\n\n{_HELP_TEXT}"
        )
        return

    initial_message = args if command == "chat" else _COMMAND_PROMPTS[command]
    now_col = datetime.now(COLOMBIA_TZ)

    try:
        run_agent(
            system_prompt=SYSTEM_PROMPT,
            tools=_build_tools(),
            tool_map=_build_tool_map(api, messenger, now_col),
            initial_message=initial_message,
            max_iterations=12,
        )
    except Exception as e:
        logger.error("[chat_agent] error: %s", e, exc_info=True)
        messenger.send_message("❌ Tuve un problema. Intentá de nuevo.")


# ── Tool definitions ──────────────────────────────────────────────────────────

def _build_tools() -> list[dict]:
    return [
        # ── Lectura ──────────────────────────────────────────────────────────
        {
            "name": "get_summary",
            "description": "Resumen financiero del mes: balance, burn rate, deudas, contexto.",
            "input_schema": {"type": "object", "properties": {
                "month": {"type": "integer"}, "year": {"type": "integer"},
            }, "required": ["month", "year"]},
        },
        {
            "name": "get_transactions",
            "description": "Transacciones del mes.",
            "input_schema": {"type": "object", "properties": {
                "month": {"type": "integer"}, "year": {"type": "integer"},
            }, "required": ["month", "year"]},
        },
        {
            "name": "get_budgets",
            "description": "Presupuestos por categoría del mes.",
            "input_schema": {"type": "object", "properties": {
                "month": {"type": "integer"}, "year": {"type": "integer"},
            }, "required": ["month", "year"]},
        },
        {
            "name": "get_debts",
            "description": "Lista de deudas activas con saldos y cuotas.",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "get_balance",
            "description": "Saldo disponible actual.",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "get_financial_context",
            "description": "Fase financiera actual y estrategia de pago de deudas.",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "get_income_sources",
            "description": "Fuentes de ingreso esperadas.",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "get_recurring_obligations",
            "description": "Gastos fijos recurrentes: arriendo, créditos, suscripciones, etc.",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        # ── Escritura: contexto financiero ────────────────────────────────────
        {
            "name": "update_financial_context",
            "description": (
                "Actualiza la fase financiera, estrategia y reward %. "
                "Usá trigger_financial_context_wizard si el usuario quiere hacerlo paso a paso."
            ),
            "input_schema": {"type": "object", "properties": {
                "phase":      {"type": "string", "enum": ["debt_payoff", "emergency_fund", "investing", "wealth_building"]},
                "strategy":   {"type": "string", "enum": ["snowball", "avalanche"]},
                "reward_pct": {"type": "integer", "minimum": 1, "maximum": 100},
                "notes":      {"type": "string"},
            }, "required": []},
        },
        {
            "name": "trigger_financial_context_wizard",
            "description": "Activa el wizard guiado de configuración del contexto financiero en Telegram.",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        # ── Escritura: deudas ─────────────────────────────────────────────────
        {
            "name": "update_debt",
            "description": "Actualiza saldo, cuota, tasa o notas de una deuda existente.",
            "input_schema": {"type": "object", "properties": {
                "id":              {"type": "string"},
                "current_balance": {"type": "integer"},
                "monthly_payment": {"type": "integer"},
                "interest_rate":   {"type": "number"},
                "status":          {"type": "string", "enum": ["active", "paid_off", "paused", "disputed"]},
                "notes":           {"type": "string"},
            }, "required": ["id"]},
        },
        {
            "name": "delete_debt",
            "description": "Elimina una deuda por ID. Confirmar antes si el pedido no es explícito.",
            "input_schema": {"type": "object", "properties": {
                "id": {"type": "string"},
            }, "required": ["id"]},
        },
        # ── Escritura: gastos fijos ───────────────────────────────────────────
        {
            "name": "create_recurring_obligation",
            "description": "Crea un nuevo gasto fijo recurrente.",
            "input_schema": {"type": "object", "properties": {
                "name":   {"type": "string"},
                "amount": {"type": "integer"},
                "notes":  {"type": "string"},
            }, "required": ["name", "amount"]},
        },
        {
            "name": "update_recurring_obligation",
            "description": "Actualiza nombre, monto o notas de un gasto fijo.",
            "input_schema": {"type": "object", "properties": {
                "id":     {"type": "string"},
                "name":   {"type": "string"},
                "amount": {"type": "integer"},
                "active": {"type": "boolean"},
                "notes":  {"type": "string"},
            }, "required": ["id"]},
        },
        {
            "name": "delete_recurring_obligation",
            "description": "Desactiva un gasto fijo (no se borra, se marca inactivo).",
            "input_schema": {"type": "object", "properties": {
                "id": {"type": "string"},
            }, "required": ["id"]},
        },
        # ── Envío ─────────────────────────────────────────────────────────────
        {
            "name": "send_telegram",
            "description": "Envía la respuesta final. Llamar una sola vez al final.",
            "input_schema": {"type": "object", "properties": {
                "message": {"type": "string", "description": "HTML de Telegram."},
            }, "required": ["message"]},
        },
    ]


# ── Tool map ──────────────────────────────────────────────────────────────────

def _build_tool_map(api: RailsApiPort, messenger: MessengerPort, now: datetime) -> dict:
    month, year = now.month, now.year

    def _patch(path: str, body: dict) -> dict:
        r = httpx.patch(
            f"{API_URL}{path}",
            headers=build_auth_headers(),
            json=body, timeout=15,
        )
        r.raise_for_status()
        return r.json().get("data", {})

    def _delete(path: str) -> dict:
        r = httpx.delete(
            f"{API_URL}{path}",
            headers=build_auth_headers(),
            timeout=15,
        )
        r.raise_for_status()
        return {"ok": True}

    def _post(path: str, body: dict) -> dict:
        r = httpx.post(
            f"{API_URL}{path}",
            headers=build_auth_headers(),
            json=body, timeout=15,
        )
        r.raise_for_status()
        return r.json().get("data", {})

    def trigger_fc_wizard(_):
        from flows import financial_context_wizard
        financial_context_wizard.trigger(api, messenger)
        return {"ok": True, "message": "Wizard iniciado en Telegram."}

    return {
        "get_summary":               lambda _: api.get_summary(month, year),
        "get_transactions":          lambda p: api.get_transactions(p.get("month", month), p.get("year", year)),
        "get_budgets":               lambda p: api.get_budgets(p.get("month", month), p.get("year", year)),
        "get_debts":                 lambda _: api.get_debts(),
        "get_balance":               lambda _: api.get_balance(),
        "get_financial_context":     lambda _: api.get_financial_context(),
        "get_income_sources":        lambda _: api.get_income_sources(),
        "get_recurring_obligations": lambda _: api.get_recurring_obligations(),

        "update_financial_context":       lambda p: api.update_financial_context(**p),
        "trigger_financial_context_wizard": trigger_fc_wizard,

        "update_debt":  lambda p: _patch(f"/api/v1/debts/{p.pop('id')}", p),
        "delete_debt":  lambda p: _delete(f"/api/v1/debts/{p['id']}"),

        "create_recurring_obligation": lambda p: _post("/api/v1/recurring_obligations", p),
        "update_recurring_obligation": lambda p: _patch(f"/api/v1/recurring_obligations/{p.pop('id')}", p),
        "delete_recurring_obligation": lambda p: _delete(f"/api/v1/recurring_obligations/{p['id']}"),

        "send_telegram": lambda p: messenger.send_message(p["message"]) or {},
    }
