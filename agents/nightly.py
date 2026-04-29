"""
agents/nightly.py — Revisión nocturna migrada al Brain.

Diferencias vs revision_nocturna.py original:
- Usa RailsApiPort (inyectado) en lugar de llamadas directas a requests
- Usa MessengerPort (inyectado) en lugar de _tg() directo
- Usa una capa LLM multi-provider para el loop agentic
- Incluye alertas de burn_rate si el summary las trae
- Menciona si el plan del mes está aprobado o falta aprobar
- Gmail sigue siendo IMAP directo (no pasa por Rails)
"""

import os
import imaplib
import email
import json
import re
import calendar
from datetime import datetime, timezone, timedelta

from ports.rails_api import RailsApiPort
from ports.messenger import MessengerPort
from adapters.rails_http import BASE_URL as API_BASE_URL, build_auth_headers
from services.llm_factory import build_llm_provider, resolve_llm_model

COLOMBIA_TZ = timezone(timedelta(hours=-5))
MESES = ["ENE", "FEB", "MAR", "ABR", "MAY", "JUN", "JUL", "AGO", "SEP", "OCT", "NOV", "DIC"]
MESES_FULL = [
    "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
    "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre",
]

GMAIL_ADDR = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_PASS = os.environ.get("GMAIL_APP_PASSWORD", "")


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
        "id":               t.get("id"),
        "date":             a.get("date"),
        "concept":          a.get("concept"),
        "product":          a.get("product"),
        "amount":           a.get("amount"),
        "type":             a.get("transaction_type"),
        "status":           a.get("status"),
        "category_code":    a.get("category_code"),
        "subcategory_code": a.get("subcategory_code"),
        "category_id":      cat_ref["id"] if cat_ref else None,
        "subcategory_id":   sub_ref["id"] if sub_ref else None,
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

    def settle_credit_card_payments(inp: dict) -> dict:
        """Liquida transacciones de tarjeta de crédito pendientes en orden FIFO."""
        import httpx
        amount = inp.get("amount", 0)
        try:
            r = httpx.post(
                f"{API_BASE_URL}/api/v1/transactions/settle_credit_card",
                headers=build_auth_headers(),
                json={"amount": amount},
                timeout=15,
            )
            r.raise_for_status()
            return {"ok": True, **r.json().get("data", {})}
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
        "get_completeness":              get_completeness,
        "get_telegram_messages":         get_telegram_messages,
        "get_gmail_emails":              get_gmail_emails,
        "get_transactions":              get_transactions,
        "get_balance":                   get_balance,
        "get_pending_transactions":      get_pending_transactions,
        "get_summary":                   get_summary,
        "create_transaction":            create_transaction,
        "update_transaction":            update_transaction,
        "settle_credit_card_payments":   settle_credit_card_payments,
        "send_telegram":                 send_telegram,
        "send_poll":                     send_poll,
        "create_milestone":              lambda p: api.create_milestone(p["code"], p.get("metadata", {})),
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
            "Resumen completo del mes: balance, burn_rate por categoría, monthly_plan, overflow_status, liquidity, deudas y contexto financiero. "
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
            "Llámala siempre para saber qué reportó el usuario hoy antes de revisar Gmail."
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
                "product":          {"type": "string", "description": "Identificador del producto financiero extraído del email (ej: el código de tarjeta o billetera mencionado por el banco)."},
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
            "Envía un mensaje al usuario. Soporta inline_keyboard para botones interactivos. "
            "Callback data: 'cat:{id}:{subcat_code}' | 'confirm:{id}' | 'skip:{id}' | "
            "'pay:{id}:{product}' | 'wizard:open:{YYYY-MM}' | 'wizard:snooze:{YYYY-MM}'."
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
        "name": "settle_credit_card_payments",
        "description": (
            "Liquida compras de tarjeta de crédito pendientes de pago al banco. "
            "Llamar cuando Gmail detecta un abono/pago a TC. "
            "Nunca crear una transacción de gasto para un abono a TC — usar este tool en su lugar. "
            "Marca las compras pendientes como pagadas en orden FIFO hasta agotar el monto del abono."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "amount": {"type": "integer", "description": "Monto del abono en COP (entero positivo)"},
            },
            "required": ["amount"],
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
    {
        "name": "create_milestone",
        "description": (
            "Registra un hito financiero (logro o setback). "
            "Idempotente: si el hito ya existe para el mismo día, devuelve el existente sin error. "
            "Llamalo cuando detectes automáticamente condiciones de hito al revisar el summary."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "enum": [
                        "debt_paid_off", "first_debt_paid_off", "debt_free",
                        "emergency_fund_reached", "first_monthly_plan",
                        "three_months_planned", "investment_started",
                        "month_positive_balance", "discretionary_under_budget",
                        "overflow_deployed", "new_debt_acquired", "payment_missed",
                        "plan_not_confirmed", "extra_debt_payment",
                    ],
                    "description": "Código del hito.",
                },
                "metadata": {
                    "type": "object",
                    "description": "Contexto del hito. Para debt_paid_off incluir debt_name y amount.",
                },
            },
            "required": ["code"],
        },
    },
]


def _month_end_context(now: datetime) -> dict:
    """
    Computes next-month variables using the Colombia-timezone date.

    Returns a dict with:
      - is_month_end      : bool  — True if day is 28, 29, 30 or 31
      - day_of_month      : int
      - days_until_month_end : int  (0 on the last day of the month)
      - next_month_name   : str   — e.g. "Mayo"
      - next_month_yyyy_mm: str   — e.g. "2026-05"
    """
    day = now.day
    year, month = now.year, now.month
    last_day = calendar.monthrange(year, month)[1]
    days_until_end = last_day - day

    # Next month (wraps Dec → Jan of next year)
    if month == 12:
        nm_year, nm_month = year + 1, 1
    else:
        nm_year, nm_month = year, month + 1

    return {
        "is_month_end":          day >= 28,
        "day_of_month":          day,
        "days_until_month_end":  days_until_end,
        "next_month_name":       MESES_FULL[nm_month - 1],
        "next_month_yyyy_mm":    f"{nm_year}-{nm_month:02d}",
    }


def _month_end_alert_block(me: dict) -> str:
    """
    Returns the full ALERTA FIN DE MES section text, pre-rendered so it can
    be safely embedded in the outer f-string without nested-quote conflicts.
    """
    day        = me["day_of_month"]
    days_left  = me["days_until_month_end"]
    nm_name    = me["next_month_name"]
    nm_yyyymm  = me["next_month_yyyy_mm"]
    dias_word  = "día" if days_left == 1 else "días"

    header = (
        f"Hoy es día {day} del mes. "
        + ("⚠️ Estamos en zona de cierre de mes." if me["is_month_end"] else "No es fin de mes — omitir esta sección completamente.")
    )

    if not me["is_month_end"]:
        return header

    detail = (
        f"Si hoy es día 28, 29, 30 o 31 del mes actual (y lo es — día {day}):\n\n"
        f"1. El mes siguiente es {nm_name} ({nm_yyyymm}).\n"
        f"   Faltan {days_left} {dias_word} para que empiece.\n\n"
        f"2. Verificá en el resultado de get_summary si ya existe un monthly_plan confirmado para {nm_yyyymm}.\n"
        f"   - Buscá en el campo monthly_plan del summary o en cualquier plan con period_start que contenga {nm_yyyymm}.\n"
        f"   - Si el summary no lo reporta explícitamente, asumí que NO existe plan.\n\n"
        f"3. Si NO existe plan confirmado para {nm_name}:\n"
        f"   - Al final del resumen nocturno, antes de la sección ⚙️ de gaps, incluí esta sección especial:\n\n"
        f"   ━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"   📅 {nm_name} empieza en {days_left} {dias_word}.\n"
        f"   ¿Armamos el plan financiero para {nm_name}?\n\n"
        f"   Ya tengo sugerencias basadas en tus últimos 3 meses.\n"
        f"   ━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"   Envialo con send_telegram usando inline_keyboard con dos botones en filas separadas:\n"
        f'   [[ {{"text": "Armar plan ahora 📋", "callback_data": "wizard:open:{nm_yyyymm}"}} ],\n'
        f'    [ {{"text": "Recordarme mañana ⏰", "callback_data": "wizard:snooze:{nm_yyyymm}"}} ]]\n\n'
        f"   Tono: primera persona, directo. No es un recordatorio genérico — es una acción concreta.\n\n"
        f"4. Si YA existe plan confirmado para {nm_name}: omitir esta sección completamente.\n\n"
        f"5. Si es fin de mes pero el usuario ya presionó \"Recordarme mañana\" "
        f"(detectás un mensaje de snooze reciente en get_telegram_messages): omitir también."
    )
    return f"{header}\n\n{detail}"


def _build_system_prompt() -> str:
    now_col = datetime.now(COLOMBIA_TZ)
    hoja = MESES[now_col.month - 1]
    me = _month_end_context(now_col)
    alert_block = _month_end_alert_block(me)
    return f"""Eres el coach financiero personal del usuario.
Ejecutas la revisión nocturna de sus finanzas: lees gastos del día, los registras en la API, y le envías un resumen con coaching.

═══ ESTILO DE COACHING ═══
- Directo, reflexivo. El texto genérico o condescendiente no aporta.
- Honestidad brutal > falsa motivación.
- Coaching basado en patrones reales de los datos — nunca en frases motivacionales vacías.
- Mes actual: {hoja} | Fecha: {now_col.strftime("%d/%m/%Y")} | Hora: {now_col.strftime("%H:%M")}
- Día del mes: {me["day_of_month"]} | Días hasta fin de mes: {me["days_until_month_end"]}
- Mes siguiente: {me["next_month_name"]} ({me["next_month_yyyy_mm"]})

═══ SUBCATEGORÍAS VÁLIDAS ═══

committed (Comprometido):
  arriendo, creditos, seguros, servicios_publicos, colegiaturas

necessary (Necesario):
  mercado, gasolina, transporte, salud, celular

discretionary (Discrecional):
  restaurantes, delivery, ocio, ropa, tecnologia, suscripciones

investment (Inversión):
  cursos, libros, suplementos, herramientas, ahorro_voluntario

social (Social):
  regalos, salidas, familia, donaciones

income (Ingreso):
  salario, freelance, reembolso, arriendo_recibido, otros_ingreso

unknown: usá cuando la categoría no está clara — subcategory_code = null

═══ REGLA DE AMBIGÜEDAD EN SUBCATEGORÍA ═══
- Clasificar directamente si el contexto hace clara la subcategoría
- Preguntar solo si la diferencia de subcategoría cambia el análisis conductual:
  * "Fui a restaurante con mis papás" → preguntar: ¿discretionary/restaurantes o social/salidas?
  * "Compré audífonos Sony" → preguntar: ¿discretionary/tecnologia o investment/herramientas?
  * "Pagué el arriendo" → clasificar directamente: committed/arriendo
  * "Compré en el Éxito" → clasificar directamente: necessary/mercado
- Para montos menores a 50.000 COP con contexto claro, no preguntar — clasificar directamente
- El usuario siempre puede cambiar la clasificación después

═══ REGLA CRÍTICA — PAGOS A TARJETA DE CRÉDITO ═══
Cuando Gmail muestra "Abono TC", "Pago TC", "Pago tarjeta", "Pago mínimo", "se han abonado":
- NO crear transacción de gasto — las compras individuales ya están registradas con payment_source=credit_card
- Extraer el monto del abono del email (número en COP)
- Llamar settle_credit_card_payments(amount=monto_del_abono)
- El sistema marcará como pagadas las compras más antiguas pendientes en orden FIFO
- Reportar en el resumen: "Abono de $X a TC — N compras saldadas, quedan $Y pendientes"
- Si settled_count == 0: "No había compras de TC pendientes registradas en el sistema"
- NUNCA crear un gasto por el abono, NUNCA descomponer el abono en compras individuales

═══ REGLA — CUOTAS DE DEUDA EN TARJETA DE CRÉDITO ═══
Las deudas diferidas en TC (ej. compra de celular en cuotas a 0%) NO son compras nuevas.
Cuando Gmail muestra el débito mensual de una cuota (deuda diferida en TC, ej. un celular a cuotas a 0%):
- NO llamar settle_credit_card_payments — la cuota ya está en el modelo de deuda
- Si se registra como transacción, usar payment_source: "debit" (es plata saliendo de la
  cuenta de ahorros para servir la deuda), nunca payment_source: "credit_card"
- Una cuota mensual de deuda diferida es una obligación estructural ya capturada como
  Debt + recurring_obligation, no una compra cotidiana en crédito

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
- Tratá overflow_status.realized_overflow como ingreso extra confirmado, NO como dinero libre automáticamente.
- Usá liquidity.safe_to_deploy y overflow_status.deployable_overflow como guardrail obligatorio.
- Si overflow_status.status == "available" y deployable_overflow > 0, menciona:
  - cuánto ingreso extra entró sobre la base del plan
  - cuánto está realmente disponible para mover
  - a dónde debería ir según overflow_rule
- Si overflow_status.status == "blocked_by_liquidity" o liquidity.safe_to_deploy <= 0:
  - NO recomiendes abonar a deuda, inversión ni colchón
  - explicá que hubo ingreso extra, pero está reservado por obligaciones próximas / buffer
  - si hace falta, usá la frase: "hay overflow de ingreso, pero no hay overflow desplegable"
- Si status == "waiting", no inventes overflow; solo omite la sección salvo que sea relevante para explicar el mes
- El ingreso extra NO debe presentarse como permiso para inflar el presupuesto base
- Ninguna recomendación de snowball puede superar overflow_status.deployable_overflow ni liquidity.safe_to_deploy

═══ CONTEXTO FINANCIERO ═══
Si get_summary o get_financial_context devuelve phase=null o data=null:
- Mencionalo al final del resumen: "⚙️ Falta configurar tu contexto financiero — escribí configurar contexto financiero y te guío en 3 pasos."
- No hagas el resumen incompleto por esto, usá los datos disponibles.

═══ SUBCATEGORÍAS PENDIENTES ═══
Después de registrar los gastos del día, revisá las transacciones del mes con subcategory_code = null:
1. Llamá get_transactions para obtener la lista completa del mes.
2. Para cada transacción sin subcategoría, intentá asignarla basándote en:
   - descripción del concepto
   - monto (montos menores a 50.000 COP con descripción clara → clasificar directamente)
   - historial de transacciones similares del mismo mes
3. Si podés determinarla con confianza → update_transaction con subcategory_code.
4. Si quedan transacciones sin subcategoría que no pudiste resolver:
   - Agrupalas en un solo mensaje de Telegram al final del resumen.
   - Formato: "📂 Estas transacciones aún no tienen subcategoría — ¿me ayudás a clasificarlas?"
   - Enviá los botones de subcategoría solo para las transacciones ambiguas (no para las que ya resolviste).
   - Usá inline_keyboard con callback_data: 'cat:{{id}}:{{subcat_code}}' para cada opción.
5. Si todas tienen subcategoría asignada, omitir esta sección en el resumen.

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

═══ ALERTA FIN DE MES ═══
{alert_block}

═══ DETECCIÓN AUTOMÁTICA DE HITOS ═══
Durante la revisión nocturna, después de obtener get_summary y get_balance, verificá si alguna condición de hito aplica para HOY y llamá create_milestone si corresponde. Es idempotente — si ya existe para el día, no pasa nada.

Condiciones a revisar:
- balance.balance_confirmed > 0 al final del mes (día ≥ 28) → month_positive_balance (metadata: {{amount: balance_confirmed}})
- burn_rate.categories alguna con spent < budget * 0.9 en categoría discretionary → discretionary_under_budget
- overflow_status.realized_overflow > 0 y overflow_status.rule != null → overflow_deployed solo si hay evidencia de abono extra a deuda/ahorro
- plan no confirmado (monthly_plan.status != "confirmed") al día 5+ → plan_not_confirmed

No llames create_milestone por condiciones que no se verificaron con datos reales de la API.

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
11. Resolver subcategorías pendientes: get_transactions → asignar las que se puedan → agrupar ambiguas
12. send_telegram → resumen con sección ⚙️ de gaps si aplica + sección 📂 de subcategorías pendientes si aplica
13. Fin de mes (días 28–31): si no existe plan para el mes siguiente, agregar sección de alerta al resumen (puede ir dentro del mismo send_telegram del paso 12) con inline_keyboard de dos botones.

═══ RESUMEN FINAL ═══
💰 <b>Revisión nocturna — {now_col.strftime("%d/%m/%Y")}</b>

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

    provider = build_llm_provider()
    provider.run_agent(
        system_prompt=_build_system_prompt(),
        tools=TOOLS,
        tool_map=tool_map,
        initial_message=f"Ejecuta la revisión nocturna para hoy {fecha}.",
        max_iterations=25,
        model=resolve_llm_model(),
    )

    print("\n✅ Revisión nocturna completada.")
