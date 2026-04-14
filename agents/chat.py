"""
agents/chat.py — Agente conversacional en tiempo real.

Se activa cuando el usuario envía un slash command (/resumen, /deudas, /chat …).
Es 100% read-only: solo puede leer datos y enviar mensajes. No crea ni modifica
ningún registro. Si explota, el agente nocturno corre igual esa noche.

Herramientas disponibles (todas de lectura):
  get_summary, get_transactions, get_budgets, get_debts,
  get_balance, get_financial_context, get_income_sources,
  get_recurring_obligations, send_telegram
"""

import logging
from datetime import datetime, timezone, timedelta

from ports.rails_api import RailsApiPort
from ports.messenger import MessengerPort, ParsedUpdate
from services.claude_client import run_agent

logger = logging.getLogger(__name__)

COLOMBIA_TZ = timezone(timedelta(hours=-5))

SYSTEM_PROMPT = """\
Sos el asistente financiero personal de Daniel, un coach financiero real que
habla con naturalidad colombiana (vos / sos / bacano / parce). No sos un bot
corporativo — sos directo, honesto y sin rodeos.

Tu trabajo ahora es responder la pregunta o comando que Daniel acaba de mandar.

Reglas:
- Usá los datos reales que traés de la API. Nada inventado.
- Sé conciso: esto es Telegram, no un PDF. Máximo 3-4 párrafos o una lista corta.
- Si los datos no son suficientes para responder bien, decilo claramente.
- Cuando hables de plata, siempre en pesos colombianos formateados ($1.500.000).
- No repitas los números crudos — interpretálos. "Llevás el 78% del presupuesto
  en discrecional con 18 días por delante" es más útil que "gastaste $480.000".
- Podés usar emojis con moderación (📊 ✅ ⚠️ 💰).
- Al final del mensaje usá `send_telegram` para enviar la respuesta.
- Solo una llamada a send_telegram por respuesta.

Tenés acceso a estas herramientas de lectura:
  get_summary, get_transactions, get_budgets, get_debts,
  get_balance, get_financial_context, get_income_sources,
  get_recurring_obligations, send_telegram
"""

# Mapeo comando → mensaje inicial para Claude
# Cada uno es una instrucción en primera persona desde el punto de vista de Daniel
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
/chat <pregunta> — Cualquier pregunta libre sobre tus finanzas
"""


def handle_command(
    api: RailsApiPort,
    messenger: MessengerPort,
    parsed: ParsedUpdate,
) -> None:
    """
    Punto de entrada. Llamado desde el webhook cuando intent == COMMAND.
    Corre sincrónicamente — el webhook lo lanza en asyncio.create_task.
    """
    command = parsed.command or ""
    args    = parsed.command_args

    # /chat sin argumentos → ayuda
    if command == "chat" and not args:
        messenger.send_message(_HELP_TEXT)
        return

    # Comando desconocido
    if command not in _COMMAND_PROMPTS and command != "chat":
        messenger.send_message(
            f"❓ No conozco el comando <code>/{command}</code>.\n\n{_HELP_TEXT}"
        )
        return

    # Construir el mensaje inicial para Claude
    if command == "chat":
        initial_message = args  # pregunta libre
    else:
        initial_message = _COMMAND_PROMPTS[command]

    now_col = datetime.now(COLOMBIA_TZ)

    try:
        run_agent(
            system_prompt=SYSTEM_PROMPT,
            tools=_build_tools(),
            tool_map=_build_tool_map(api, messenger, now_col),
            initial_message=initial_message,
            max_iterations=8,  # más corto que el nocturno — respuesta rápida
        )
    except Exception as e:
        logger.error("[chat_agent] error: %s", e, exc_info=True)
        messenger.send_message("❌ Tuve un problema consultando tus datos. Intentá de nuevo.")


# ── Tool definitions ──────────────────────────────────────────────────────────

def _build_tools() -> list[dict]:
    return [
        {
            "name": "get_summary",
            "description": "Resumen financiero del mes: balance, burn rate por categoría, deudas, contexto.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "month": {"type": "integer", "description": "Número de mes (1-12)"},
                    "year":  {"type": "integer", "description": "Año (ej: 2026)"},
                },
                "required": ["month", "year"],
            },
        },
        {
            "name": "get_transactions",
            "description": "Lista de transacciones del mes.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "month": {"type": "integer"},
                    "year":  {"type": "integer"},
                },
                "required": ["month", "year"],
            },
        },
        {
            "name": "get_budgets",
            "description": "Presupuestos por categoría del mes.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "month": {"type": "integer"},
                    "year":  {"type": "integer"},
                },
                "required": ["month", "year"],
            },
        },
        {
            "name": "get_debts",
            "description": "Lista de deudas activas con saldos y cuotas.",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "get_balance",
            "description": "Saldo disponible actual (ingresos confirmados menos gastos confirmados).",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "get_financial_context",
            "description": "Fase financiera actual (debt_payoff, emergency_fund, etc.) y estrategia.",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "get_income_sources",
            "description": "Fuentes de ingreso esperadas: nombre, monto, rango de días de llegada.",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "get_recurring_obligations",
            "description": "Obligaciones fijas recurrentes: arriendo, créditos, etc.",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "send_telegram",
            "description": "Envía la respuesta final al usuario por Telegram. Llamar una sola vez al final.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "Texto en HTML de Telegram."},
                },
                "required": ["message"],
            },
        },
    ]


def _build_tool_map(
    api: RailsApiPort,
    messenger: MessengerPort,
    now: datetime,
) -> dict:
    month = now.month
    year  = now.year

    return {
        "get_summary":               lambda _: api.get_summary(month, year),
        "get_transactions":          lambda p: api.get_transactions(
                                         p.get("month", month), p.get("year", year)
                                     ),
        "get_budgets":               lambda p: api.get_budgets(
                                         p.get("month", month), p.get("year", year)
                                     ),
        "get_debts":                 lambda _: api.get_debts(),
        "get_balance":               lambda _: api.get_balance(),
        "get_financial_context":     lambda _: api.get_financial_context(),
        "get_income_sources":        lambda _: api.get_income_sources(),
        "get_recurring_obligations": lambda _: api.get_recurring_obligations(),
        "send_telegram":             lambda p: messenger.send_message(p["message"]) or {},
    }
