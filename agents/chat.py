"""
agents/chat.py — Agente conversacional en tiempo real.

Maneja slash commands útiles (/resumen, /deudas, /balance) y también
mensajes normales de Telegram. El chat ya no es un modo aparte: la
conversación está siempre disponible.
"""

import logging
import httpx
from datetime import datetime, timezone, timedelta, date

from adapters.rails_http import BASE_URL as API_BASE_URL, build_auth_headers
from ports.rails_api import RailsApiPort
from ports.messenger import MessengerPort, ParsedUpdate
from services.claude_client import run_agent

logger = logging.getLogger(__name__)

COLOMBIA_TZ = timezone(timedelta(hours=-5))

API_URL   = API_BASE_URL
CHAT_MODEL = "claude-haiku-4-5"

SYSTEM_PROMPT = """\
Sos el asistente financiero personal de Daniel, un coach financiero real que
habla con naturalidad colombiana (vos / sos / bacano / parce). No sos un bot
corporativo — sos directo, honesto y sin rodeos.

Tu trabajo es responder lo que Daniel pide: registrar gastos o ingresos,
corregirlos, borrarlos, consultar datos, hacer cambios, o activar wizards de
configuración.

Reglas:
- Usá los datos reales de la API. Nada inventado.
- Sé conciso: Telegram, no PDF. Máximo 3-4 párrafos o una lista corta.
- Cuando hables de plata, siempre en pesos colombianos formateados ($1.500.000).
- No repitas números crudos — interpretálos.
- Podés usar emojis con moderación (📊 ✅ ⚠️ 💰).
- Al final usá `send_telegram` para enviar la respuesta. Solo una vez.
- Si el usuario manda un gasto o ingreso claro, registralo en tiempo real.
  Si queda claro, respondé algo corto como "✅ Registrado".
- Si el usuario quiere corregir o borrar "ese gasto", usá transacciones
  recientes para inferir a cuál se refiere. Si hay ambigüedad real, preguntá.
- Para cambios destructivos, si el pedido es explícito podés ejecutarlo.
  Si no sabés a qué registro se refiere, preguntá antes de tocar nada.
- Si tenés suficiente contexto para categorizar, hacelo de una vez.
- Si no tenés suficiente contexto de categoría o subcategoría, registrá igual
  y pedí la aclaración más corta posible.
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

También podés escribirme normal, sin comandos:
• "actualizá el saldo del Préstamo Personal a $28.500.000"
• "configurar contexto financiero"
• "agregá un gasto fijo: Spotify $21.900"
• "borrá la deuda Préstamo familiar"
• "pollo 14000"
• "olvidá ese gasto"
• "corregí ese gasto, fueron 36 mil"
"""


def _flatten_transaction(t: dict) -> dict:
    attributes = t.get("attributes", t)
    category_ref = t.get("relationships", {}).get("category", {}).get("data")
    subcategory_ref = t.get("relationships", {}).get("subcategory", {}).get("data")
    return {
        "id": t.get("id"),
        "date": attributes.get("date"),
        "concept": attributes.get("concept"),
        "product": attributes.get("product"),
        "amount": attributes.get("amount"),
        "transaction_type": attributes.get("transaction_type"),
        "status": attributes.get("status"),
        "source": attributes.get("source"),
        "category_id": category_ref["id"] if category_ref else None,
        "subcategory_id": subcategory_ref["id"] if subcategory_ref else None,
        "metadata": attributes.get("metadata") or {},
    }


def _run_conversation(
    api: RailsApiPort,
    messenger: MessengerPort,
    initial_message: str,
) -> None:
    now_col = datetime.now(COLOMBIA_TZ)
    state = {"responded": False, "mutated": False}
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
        messenger.send_message(final_text)
        return

    if state["mutated"]:
        messenger.send_message("✅ Listo.")
        return

    messenger.send_message("⚠️ No pude cerrar bien la respuesta. Intentá de nuevo.")


def handle_command(api: RailsApiPort, messenger: MessengerPort, parsed: ParsedUpdate) -> None:
    command = parsed.command or ""

    if command not in _COMMAND_PROMPTS:
        messenger.send_message(
            f"❓ No conozco el comando <code>/{command}</code>.\n\n{_HELP_TEXT}"
        )
        return

    initial_message = _COMMAND_PROMPTS[command]

    try:
        _run_conversation(api, messenger, initial_message)
    except Exception as e:
        logger.error("[chat_agent] error: %s", e, exc_info=True)
        messenger.send_message("❌ Tuve un problema. Intentá de nuevo.")


def handle_message(api: RailsApiPort, messenger: MessengerPort, parsed: ParsedUpdate) -> None:
    text = (parsed.text or "").strip()
    if not text:
        return

    message_payload = parsed.raw.get("message", {})
    telegram_ts = message_payload.get("date")
    if telegram_ts:
        ts_col = datetime.fromtimestamp(telegram_ts, tz=timezone.utc).astimezone(COLOMBIA_TZ)
        telegram_context = (
            f"Fecha real del mensaje en Telegram: {ts_col.strftime('%Y-%m-%d')}\n"
            f"Hora real del mensaje en Colombia: {ts_col.strftime('%H:%M')}\n"
            "Usa esa fecha por defecto si el usuario no especifica otra. "
            "No inventes fechas ni años distintos."
        )
    else:
        ts_col = datetime.now(COLOMBIA_TZ)
        telegram_context = (
            f"Fecha actual en Colombia: {ts_col.strftime('%Y-%m-%d')}\n"
            f"Hora actual en Colombia: {ts_col.strftime('%H:%M')}\n"
            "Si el usuario no especifica fecha, usa la fecha de hoy en Colombia."
        )

    initial_message = (
        "Mensaje nuevo de Daniel en Telegram. "
        "Interpretalo y actuá en tiempo real usando las herramientas disponibles. "
        "Si es un gasto o ingreso claro, registralo. "
        "Si es una corrección o borrado, buscá la transacción correcta y actualizala. "
        "Si hay ambigüedad real, pedí una aclaración breve.\n\n"
        f"{telegram_context}\n\n"
        f"Mensaje: {text}"
    )

    try:
        _run_conversation(api, messenger, initial_message)
    except Exception as e:
        logger.error("[chat_agent] realtime error: %s", e, exc_info=True)
        messenger.send_message("❌ No pude procesar eso. Intentá de nuevo.")


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
            "name": "get_recent_transactions",
            "description": "Transacciones recientes, ordenadas de más nueva a más vieja. Úsalo para corregir o borrar 'ese gasto'.",
            "input_schema": {"type": "object", "properties": {
                "days": {"type": "integer", "minimum": 1, "maximum": 31},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            }, "required": []},
        },
        {
            "name": "get_categories",
            "description": "Categorías y subcategorías disponibles para clasificar gastos e ingresos.",
            "input_schema": {"type": "object", "properties": {}, "required": []},
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
        # ── Escritura: transacciones ─────────────────────────────────────────
        {
            "name": "create_transaction",
            "description": "Crea un gasto o ingreso. Usá source=telegram para mensajes que llegan por Telegram.",
            "input_schema": {"type": "object", "properties": {
                "date": {"type": "string", "description": "YYYY-MM-DD"},
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
            }, "required": ["date", "concept", "amount", "transaction_type", "status"]},
        },
        {
            "name": "update_transaction",
            "description": "Corrige una transacción existente por ID.",
            "input_schema": {"type": "object", "properties": {
                "id": {"type": "string"},
                "date": {"type": "string", "description": "YYYY-MM-DD"},
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
            }, "required": ["id"]},
        },
        {
            "name": "delete_transaction",
            "description": "Elimina una transacción por ID.",
            "input_schema": {"type": "object", "properties": {
                "id": {"type": "string"},
            }, "required": ["id"]},
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

def _build_tool_map(api: RailsApiPort, messenger: MessengerPort, now: datetime, state: dict | None = None) -> dict:
    month, year = now.month, now.year
    today = now.date()
    state = state or {"responded": False, "mutated": False}

    def _patch(path: str, body: dict) -> dict:
        r = httpx.patch(
            f"{API_URL}{path}",
            headers=build_auth_headers(),
            json=body, timeout=15,
        )
        r.raise_for_status()
        state["mutated"] = True
        return r.json().get("data", {})

    def _delete(path: str) -> dict:
        r = httpx.delete(
            f"{API_URL}{path}",
            headers=build_auth_headers(),
            timeout=15,
        )
        r.raise_for_status()
        state["mutated"] = True
        return {"ok": True}

    def _post(path: str, body: dict) -> dict:
        r = httpx.post(
            f"{API_URL}{path}",
            headers=build_auth_headers(),
            json=body, timeout=15,
        )
        r.raise_for_status()
        state["mutated"] = True
        return r.json().get("data", {})

    def _normalize_categories() -> list[dict]:
        categories = []
        for raw in api.get_categories():
            attributes = raw.get("attributes", raw)
            subcategories = raw.get("relationships", {}).get("subcategories", {}).get("data", [])
            categories.append({
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
            })
        return categories

    def _get_recent_transactions(input_data: dict) -> dict:
        days = int(input_data.get("days", 7))
        limit = int(input_data.get("limit", 10))
        cutoff = today - timedelta(days=max(days - 1, 0))

        month_keys = {(today.year, today.month), (cutoff.year, cutoff.month)}
        rows = []
        for tx_year, tx_month in month_keys:
            try:
                rows.extend(api.get_transactions(tx_month, tx_year))
            except Exception as exc:
                logger.warning("[chat_agent] recent transactions fetch failed for %s-%s: %s", tx_year, tx_month, exc)

        flattened = []
        for row in rows:
            flat = _flatten_transaction(row)
            try:
                tx_date = date.fromisoformat(str(flat["date"]))
            except Exception:
                continue
            if tx_date < cutoff:
                continue
            flat["_sort_key"] = tx_date.isoformat()
            flattened.append(flat)

        flattened.sort(key=lambda item: (item["_sort_key"], str(item.get("id"))), reverse=True)
        for item in flattened:
            item.pop("_sort_key", None)

        return {"transactions": flattened[:limit], "total": len(flattened[:limit]), "days": days}

    def trigger_fc_wizard(_):
        from flows import financial_context_wizard
        financial_context_wizard.trigger(api, messenger)
        return {"ok": True, "message": "Wizard iniciado en Telegram."}

    return {
        "get_summary":               lambda _: api.get_summary(month, year),
        "get_transactions":          lambda p: api.get_transactions(p.get("month", month), p.get("year", year)),
        "get_recent_transactions":   _get_recent_transactions,
        "get_categories":            lambda _: {"categories": _normalize_categories()},
        "get_budgets":               lambda p: api.get_budgets(p.get("month", month), p.get("year", year)),
        "get_debts":                 lambda _: api.get_debts(),
        "get_balance":               lambda _: api.get_balance(),
        "get_financial_context":     lambda _: api.get_financial_context(),
        "get_income_sources":        lambda _: api.get_income_sources(),
        "get_recurring_obligations": lambda _: api.get_recurring_obligations(),

        "create_transaction":        lambda p: _post("/api/v1/transactions", p),
        "update_transaction":        lambda p: _patch(f"/api/v1/transactions/{p.pop('id')}", p),
        "delete_transaction":        lambda p: _delete(f"/api/v1/transactions/{p['id']}"),

        "update_financial_context":       lambda p: api.update_financial_context(**p),
        "trigger_financial_context_wizard": trigger_fc_wizard,

        "update_debt":  lambda p: _patch(f"/api/v1/debts/{p.pop('id')}", p),
        "delete_debt":  lambda p: _delete(f"/api/v1/debts/{p['id']}"),

        "create_recurring_obligation": lambda p: _post("/api/v1/recurring_obligations", p),
        "update_recurring_obligation": lambda p: _patch(f"/api/v1/recurring_obligations/{p.pop('id')}", p),
        "delete_recurring_obligation": lambda p: _delete(f"/api/v1/recurring_obligations/{p['id']}"),

        "send_telegram": lambda p: state.update({"responded": True}) or messenger.send_message(p["message"]) or {},
    }
