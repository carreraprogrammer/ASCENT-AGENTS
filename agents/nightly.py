"""
agents/nightly.py — Revisión nocturna migrada al Brain.

Diferencias vs revision_nocturna.py original:
- Usa RailsApiPort (inyectado) en lugar de llamadas directas a requests
- Usa MessengerPort (inyectado) en lugar de _tg() directo
- Usa services/claude_client.py para el loop agentic
- Incluye alertas de burn_rate si el summary las trae
- Menciona si el plan del mes está aprobado o falta aprobar
- Gmail sigue siendo IMAP directo (no pasa por Rails)
"""

import os
import imaplib
import email
import json
import re
from datetime import datetime, timezone, timedelta

from ports.rails_api import RailsApiPort
from ports.messenger import MessengerPort
from services.claude_client import run_agent
from adapters.rails_http import BASE_URL as API_BASE_URL, build_auth_headers

COLOMBIA_TZ = timezone(timedelta(hours=-5))
MESES = ["ENE", "FEB", "MAR", "ABR", "MAY", "JUN", "JUL", "AGO", "SEP", "OCT", "NOV", "DIC"]

GMAIL_ADDR = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_PASS = os.environ.get("GMAIL_APP_PASSWORD", "")
NIGHTLY_MODEL = "claude-sonnet-4-6"


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS GMAIL
# ══════════════════════════════════════════════════════════════════════════════

def _extract_body(msg) -> str:
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() in ("text/plain", "text/html"):
                try:
                    body += part.get_payload(decode=True).decode("utf-8", errors="ignore")
                except Exception:
                    pass
    else:
        try:
            body = msg.get_payload(decode=True).decode("utf-8", errors="ignore")
        except Exception:
            pass
    body = re.sub(r"<[^>]+>", " ", body)
    body = re.sub(r"\s+", " ", body).strip()
    return body[:3000]


def _fetch_gmail_emails() -> dict:
    remitentes = [
        "BANCO_DAVIVIENDA@davivienda.com",
        "notificaciones@nequi.com.co",
        "somos@nequi.com.co",
        "notificaciones@davivienda.com",
    ]
    # Use Colombia timezone — nightly runs at 4am UTC = 11pm Colombia (next UTC day)
    hoy_str = datetime.now(COLOMBIA_TZ).date().strftime("%d-%b-%Y")
    emails = []
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(GMAIL_ADDR, GMAIL_PASS)
        mail.select("inbox")
        for remitente in remitentes:
            _, data = mail.search(None, f'(FROM "{remitente}" SINCE "{hoy_str}")')
            for uid in data[0].split():
                _, msg_data = mail.fetch(uid, "(RFC822)")
                msg = email.message_from_bytes(msg_data[0][1])
                body = _extract_body(msg)
                if body:
                    emails.append({
                        "from": remitente,
                        "subject": str(msg.get("Subject", "")),
                        "body": body,
                    })
        mail.logout()
        return {"ok": True, "emails": emails, "total": len(emails)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# BUILD TOOL MAP — cierre sobre los puertos inyectados
# ══════════════════════════════════════════════════════════════════════════════

def _flatten_transaction(t: dict) -> dict:
    a = t.get("attributes", t)  # soporta JSON:API y dicts planos
    cat_ref = t.get("relationships", {}).get("category", {}).get("data")
    sub_ref = t.get("relationships", {}).get("subcategory", {}).get("data")
    return {
        "id":             t.get("id"),
        "date":           a.get("date"),
        "concept":        a.get("concept"),
        "product":        a.get("product"),
        "amount":         a.get("amount"),
        "type":           a.get("transaction_type"),
        "status":         a.get("status"),
        "category_id":    cat_ref["id"] if cat_ref else None,
        "subcategory_id": sub_ref["id"] if sub_ref else None,
    }


def build_tool_map(api: RailsApiPort, messenger: MessengerPort) -> dict:
    now_col = datetime.now(COLOMBIA_TZ)

    def get_telegram_messages(_input: dict) -> dict:
        """
        Devuelve las transacciones registradas HOY desde Telegram/chat en tiempo real.
        Desde el 15-Apr-2026 el Brain procesa mensajes en tiempo real — ya no se guardan
        en TelegramUpdate de Rails. Las transacciones creadas por el chat tienen source=telegram.
        """
        try:
            hoy = now_col.date().isoformat()
            txns_raw = api.get_transactions(now_col.month, now_col.year)

            telegram_txns = []
            for t in txns_raw:
                a = t.get("attributes", t)
                txn_date = (a.get("date") or "")[:10]
                if txn_date == hoy and a.get("source") in ("telegram", "chat"):
                    telegram_txns.append({
                        "id":       t.get("id"),
                        "concepto": a.get("concept"),
                        "monto":    a.get("amount"),
                        "tipo":     a.get("transaction_type"),
                        "estado":   a.get("status"),
                        "fuente":   a.get("source"),
                        "hora":     a.get("created_at", "")[:16],
                    })

            return {
                "ok": True,
                "nota": (
                    "Los mensajes de Telegram se procesan en tiempo real por el chat agent. "
                    "Estas son las transacciones ya registradas hoy desde el chat. "
                    "NO volver a registrarlas — ya existen en la DB."
                ),
                "transacciones_hoy_telegram": telegram_txns,
                "total": len(telegram_txns),
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def get_gmail_emails(_input: dict) -> dict:
        return _fetch_gmail_emails()

    def get_transactions(inp: dict) -> dict:
        month = inp.get("month", now_col.month)
        year = inp.get("year", now_col.year)
        try:
            txns_raw = api.get_transactions(month, year)
            txns = [_flatten_transaction(t) for t in txns_raw]
            return {"ok": True, "transactions": txns, "total": len(txns), "month": month, "year": year}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def get_balance(inp: dict) -> dict:
        month = inp.get("month", now_col.month)
        year = inp.get("year", now_col.year)
        try:
            data = api.get_balance()
            data["nota"] = (
                "balance_confirmed = ingresos_confirmados - gastos_confirmados. "
                "Usa balance_confirmed para reportar estado actual."
            )
            return {"ok": True, **data}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def get_pending_transactions(_input: dict) -> dict:
        try:
            txns_raw = api.get_pending_transactions()
            txns = [_flatten_transaction(t) for t in txns_raw]
            return {"ok": True, "pending": txns, "total": len(txns)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def get_summary(_input: dict) -> dict:
        """Resumen completo del mes: balance, burn_rate, deudas, contexto financiero."""
        try:
            return {"ok": True, **api.get_summary(now_col.month, now_col.year)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def get_completeness(_input: dict) -> dict:
        """Estado de completitud del contexto financiero del usuario."""
        try:
            import httpx
            r = httpx.get(
                f"{API_BASE_URL}/api/v1/completeness",
                headers=build_auth_headers(),
                params={"month": now_col.month, "year": now_col.year},
                timeout=15,
            )
            r.raise_for_status()
            return {"ok": True, **r.json().get("data", {})}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def create_transaction(inp: dict) -> dict:
        try:
            import httpx
            r = httpx.post(
                f"{API_BASE_URL}/api/v1/transactions",
                headers=build_auth_headers(),
                json=inp, timeout=15,
            )
            if r.status_code == 201:
                data = r.json()["data"]
                return {"ok": True, "created": True, "id": data["id"],
                        "concept": data["attributes"]["concept"],
                        "amount": data["attributes"]["amount"],
                        "status": data["attributes"]["status"]}
            if r.status_code == 409:
                body = r.json()
                return {"ok": True, "created": False, "already_existed": True,
                        "existing_id": body.get("existing_id"),
                        "detail": body.get("errors", [{}])[0].get("detail", "")}
            return {"ok": False, "status_code": r.status_code, "error": r.text[:300]}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def update_transaction(inp: dict) -> dict:
        txn_id = inp.pop("id")
        try:
            result = api.update_transaction(txn_id, **inp)
            data = result.get("data", {}).get("attributes", result)
            return {"ok": True, "updated": txn_id,
                    "concept": data.get("concept"), "status": data.get("status")}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def send_telegram(inp: dict) -> dict:
        try:
            if "inline_keyboard" in inp:
                buttons = inp["inline_keyboard"]
                messenger.send_with_buttons(inp["mensaje"], buttons)
            else:
                messenger.send_message(inp["mensaje"])
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def send_poll(inp: dict) -> dict:
        import httpx
        chat_id = int(os.environ.get("TELEGRAM_CHAT_ID", "0"))
        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        try:
            r = httpx.post(
                f"https://api.telegram.org/bot{bot_token}/sendPoll",
                json={"chat_id": chat_id, "question": inp["question"],
                      "options": inp["options"], "is_anonymous": False},
                timeout=15,
            )
            result = r.json().get("result", {})
            return {"ok": r.is_success, "poll_id": result.get("poll", {}).get("id"),
                    "message_id": result.get("message_id")}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    return {
        "get_completeness":         get_completeness,
        "get_telegram_messages":    get_telegram_messages,
        "get_gmail_emails":         get_gmail_emails,
        "get_transactions":         get_transactions,
        "get_balance":              get_balance,
        "get_pending_transactions": get_pending_transactions,
        "get_summary":              get_summary,
        "create_transaction":       create_transaction,
        "update_transaction":       update_transaction,
        "send_telegram":            send_telegram,
        "send_poll":                send_poll,
    }


# ── Herramientas para Claude ──────────────────────────────────────────────────

TOOLS = [
    {
        "name": "get_completeness",
        "description": (
            "Estado de completitud del contexto financiero: income_profile, debts, recurring_expenses, strategy, monthly_plan. "
            "Llámalo PRIMERO. Si hay dimensiones missing o partial, inclúyelas al final del reporte como sección ⚙️ de gaps."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_summary",
        "description": (
            "Resumen completo del mes: balance, burn_rate por categoría, monthly_plan, overflow_status, deudas y contexto financiero. "
            "Llámalo después de get_completeness para ver alertas de presupuesto y estado del plan del mes. "
            "Si burn_rate.categories tiene alertas, inclúyelas en el mensaje de Telegram."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_telegram_messages",
        "description": (
            "Devuelve las transacciones registradas HOY desde Telegram (source=telegram). "
            "Los mensajes se procesan en tiempo real — esta herramienta muestra lo que el chat agent ya registró. "
            "Llámala siempre para saber qué reportó Daniel hoy antes de revisar Gmail."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_gmail_emails",
        "description": "Busca correos bancarios de HOY (Davivienda, Nequi).",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_transactions",
        "description": "Transacciones del mes para deduplicar. NO uses esto para calcular balance.",
        "input_schema": {
            "type": "object",
            "properties": {
                "month": {"type": "integer"},
                "year": {"type": "integer"},
            },
            "required": [],
        },
    },
    {
        "name": "get_balance",
        "description": (
            "Balance real del mes calculado por la API. "
            "SIEMPRE úsalo antes del resumen — nunca sumes manualmente."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "month": {"type": "integer"},
                "year": {"type": "integer"},
            },
            "required": [],
        },
    },
    {
        "name": "get_pending_transactions",
        "description": "Transacciones con status=pending de días anteriores.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "create_transaction",
        "description": (
            "Registra una transacción nueva. "
            "Si devuelve already_existed=true (HTTP 409), la transacción YA EXISTE — no volver a intentar, no es un error. "
            "La dedup la maneja la API: mismo date+amount+product+tipo = rechazado para fuentes telegram/gmail."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date":             {"type": "string"},
                "concept":          {"type": "string"},
                "product":          {"type": "string", "enum": ["nequi", "tc0004", "tc0002", "debito", "bre-b"]},
                "amount":           {"type": "integer"},
                "transaction_type": {"type": "string", "enum": ["expense", "income"]},
                "status":           {"type": "string", "enum": ["confirmed", "pending"]},
                "subcategory_code": {
                    "type": "string",
                    "enum": [
                        "salario", "freelance", "reembolso", "arriendo_recibido", "otros_ingreso",
                        "arriendo", "creditos", "seguros", "servicios_publicos", "colegiaturas",
                        "mercado", "gasolina", "transporte", "salud", "celular",
                        "restaurantes", "delivery", "ocio", "ropa", "tecnologia", "suscripciones",
                        "cursos", "libros", "suplementos", "herramientas", "ahorro_voluntario",
                        "regalos", "salidas", "familia", "donaciones",
                    ],
                },
                "source": {"type": "string", "enum": ["telegram", "gmail", "manual"]},
            },
            "required": ["date", "concept", "amount", "transaction_type", "status"],
        },
    },
    {
        "name": "update_transaction",
        "description": "Actualiza una transacción existente por ID.",
        "input_schema": {
            "type": "object",
            "properties": {
                "id":               {"type": "string"},
                "concept":          {"type": "string"},
                "status":           {"type": "string", "enum": ["confirmed", "pending"]},
                "subcategory_code": {"type": "string"},
                "amount":           {"type": "integer"},
            },
            "required": ["id"],
        },
    },
    {
        "name": "send_telegram",
        "description": (
            "Envía un mensaje a Daniel. Soporta inline_keyboard para botones interactivos. "
            "Callback data: 'cat:{id}:{subcat_code}' | 'confirm:{id}' | 'skip:{id}'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "mensaje": {"type": "string", "description": "HTML. Soporta <b>, <i>. Máx 4096 chars."},
                "inline_keyboard": {
                    "type": "array",
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "text":          {"type": "string"},
                                "callback_data": {"type": "string"},
                            },
                        },
                    },
                },
            },
            "required": ["mensaje"],
        },
    },
    {
        "name": "send_poll",
        "description": "Encuesta nativa de Telegram. Úsala solo cuando inline_keyboard no resuelve el problema.",
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "options":  {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 10},
            },
            "required": ["question", "options"],
        },
    },
]


def _build_system_prompt() -> str:
    now_col = datetime.now(COLOMBIA_TZ)
    hoja = MESES[now_col.month - 1]
    return f"""Eres el coach financiero personal de el usuario.
Ejecutas la revisión nocturna de sus finanzas: lees gastos del día, los registras en la API, y le envías un resumen con coaching.

═══ CONTEXTO ═══
- Meta: convertirse en alguien que merece ganar $15,000 USD/mes — no es solo dinero, es identidad
- Trabaja en Acme y empresa propia Globex en crecimiento
- Directo, reflexivo, le molesta el texto genérico o condescendiente
- Honestidad brutal > falsa motivación
- Mes actual: {hoja} | Fecha: {now_col.strftime("%d/%m/%Y")} | Hora Colombia: {now_col.strftime("%H:%M")}

═══ PRODUCTOS FINANCIEROS ═══
- nequi     → billetera Nequi
- tc0004    → TC Viajes (terminación XXXX) — CRÉDITO
- tc0002    → TC Davivienda (terminación XXXX) — CRÉDITO
- debito    → Cuenta ahorros / débito Davivienda
- bre-b     → transferencias Bre-B

═══ SUBCATEGORÍAS VÁLIDAS ═══
INGRESO:      salario | freelance | reembolso | arriendo_recibido | otros_ingreso
COMPROMETIDO: arriendo | creditos | seguros | servicios_publicos | colegiaturas
NECESARIO:    mercado | gasolina | transporte | salud | celular
DISCRECIONAL: restaurantes | delivery | ocio | ropa | tecnologia | suscripciones
INVERSIÓN:    cursos | libros | suplementos | herramientas | ahorro_voluntario
SOCIAL:       regalos | salidas | familia | donaciones

═══ REGLA CRÍTICA — PAGOS A TARJETA DE CRÉDITO ═══
Cuando Gmail muestra "Abono TC", "Pago TC", "Pago tarjeta", "Pago mínimo":
- Es un movimiento de caja — las compras individuales YA están registradas
- Registrar como: transaction_type=expense, subcategory_code=creditos, product=debito, status=confirmed
- Concepto: "Pago TC Viajes" o "Pago TC Davivienda"
- NUNCA descomponer el abono en compras individuales
- NUNCA duplicar compras ya existentes

═══ DEDUPLICACIÓN ═══
1. Telegram + Gmail mismo gasto → registrar UNA sola vez
2. Duplicado = misma fecha + mismo monto (±2%) + mismo producto
3. Dos montos iguales mismo día DISTINTO producto → son distintos, registrar ambos
4. Verifica contra get_transactions antes de registrar

═══ TELEGRAM EN TIEMPO REAL ═══
Los mensajes de Telegram se procesan en tiempo real por el chat agent (desde 15-Apr-2026).
- get_telegram_messages devuelve TRANSACCIONES YA REGISTRADAS hoy con source=telegram
- NO son mensajes crudos — son transacciones ya en la DB
- NO volver a registrarlas. Solo úsalas para cruzar con Gmail y detectar duplicados o sin registrar
- Si hay una transacción en Gmail que coincide con una de Telegram → mismo gasto, no duplicar

═══ NUEVO: ALERTAS DE PRESUPUESTO ═══
Si get_summary devuelve burn_rate.categories con alertas:
- Inclúyelas en el resumen bajo la sección "⚠️ Alertas de presupuesto"
- Sé específico: "Discrecional va en $762k proyectado vs $500k presupuestado"
- Si no hay presupuestos configurados, omite esta sección sin mencionarla

═══ NUEVO: ESTADO DEL PLAN QUINCENAL ═══
Si es día 1-5 o 15-20 del mes, menciona al final del resumen:
- Si hay budgets configurados: "✅ Plan del mes aprobado"
- Si NO hay budgets: "📋 Falta aprobar el plan del mes — el wizard te lo envía esta mañana"

═══ NUEVO: OVERFLOW DEL MES ═══
Si get_summary devuelve overflow_status:
- Si overflow_status.status == "available", menciona en una línea:
  - cuánto ingreso extra ya entró sobre la base del plan
  - a dónde debería ir según overflow_rule
- Si status == "waiting", no inventes overflow; solo omite la sección salvo que sea relevante para explicar el mes
- El ingreso extra NO debe presentarse como permiso para inflar el presupuesto base

═══ CONTEXTO FINANCIERO ═══
Si get_summary o get_financial_context devuelve phase=null o data=null:
- Mencionalo al final del resumen: "⚙️ Falta configurar tu contexto financiero — escribí configurar contexto financiero y te guío en 3 pasos."
- No hagas el resumen incompleto por esto, usá los datos disponibles.

═══ LECTURA CONDUCTUAL ═══
No te limites a listar movimientos. Interpretá el patrón:
- discretionary alto → señalá gasto elegido y dónde conviene meter fricción
- investment bajo o cero → señalá que casi no hubo construcción de futuro
- committed alto → señalá que la presión es estructural, no solo de autocontrol
- social visible → nombralo como gasto relacional, no como ruido
Máximo 2 bullets conductuales. Tono directo, no sermoneador.

═══ PROCESAMIENTO DE CALLBACKS ═══
Si get_telegram_messages devuelve resolved_callbacks:
  - type "categorize": update_transaction(id=..., subcategory_code=..., status="confirmed")
  - type "confirm":    update_transaction(id=..., status="confirmed")
  - type "skip":       update_transaction(id=..., clarification_resolved_at=fecha_hoy)

═══ FLUJO RECOMENDADO ═══
1. get_completeness → detectar gaps de contexto ANTES de todo
2. get_summary → alertas de presupuesto + estado plan quincenal + overflow si aplica
3. get_telegram_messages → transacciones ya registradas hoy desde el chat (source=telegram)
4. get_gmail_emails → cargos bancarios del día
5. Cruzar Gmail vs Telegram: si coinciden monto+producto → mismo gasto, NO duplicar
6. get_transactions → lista completa del mes para dedup adicional (NO para balance)
7. get_balance → balance real (SIEMPRE antes del resumen)
8. get_pending_transactions → pendientes de días anteriores
9. Registrar solo los gastos de Gmail que NO estén ya en Telegram/transactions → create_transaction
10. Para gastos inciertos → create_transaction(pending) + send_telegram con botones
11. send_telegram → resumen con sección ⚙️ de gaps si aplica

═══ RESUMEN FINAL ═══
💰 <b>Revisión Daniel 15K — {now_col.strftime("%d/%m/%Y")}</b>

📥 <b>Registrado hoy:</b>
• [lista gastos — si no hay nada, decirlo en una línea]

📊 <b>Balance {hoja}:</b>
[números de get_balance: ingresos, gastos, balance_confirmed]

[coaching 1-2 líneas, específico, honesto]
[alertas de burn_rate si aplica]
[pendientes con botones — UN mensaje por pendiente]

⚙️ <b>El sistema necesita esto para ayudarte mejor:</b>
[SOLO si get_completeness devuelve dimensiones missing o partial]
• income_profile missing → "Necesito conocer tus fuentes de ingreso para armar un plan real."
• monthly_plan missing → "No hay plan confirmado para este mes. Sin eso el coaching es genérico."
• strategy missing → "Falta tu estrategia financiera. Escribí 'configurar contexto financiero'."
• recurring_expenses missing → "Sin gastos fijos registrados no puedo calcular tu margen real."
[Si completeness está todo sufficient, omitir esta sección completamente]"""


def run_nightly(api: RailsApiPort, messenger: MessengerPort) -> None:
    now_col = datetime.now(COLOMBIA_TZ)
    fecha = now_col.strftime("%d/%m/%Y")
    print(f"\n=== Revisión nocturna Brain — {fecha} ===\n")

    tool_map = build_tool_map(api, messenger)

    run_agent(
        system_prompt=_build_system_prompt(),
        tools=TOOLS,
        tool_map=tool_map,
        initial_message=f"Ejecuta la revisión nocturna para hoy {fecha}.",
        max_iterations=25,
        model=NIGHTLY_MODEL,
    )

    print("\n✅ Revisión nocturna completada.")
