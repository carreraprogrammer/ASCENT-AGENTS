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


def _fetch_gmail_emails(since_date: str | None = None) -> dict:
    remitentes = [
        "BANCO_DAVIVIENDA@davivienda.com",
        "notificaciones@nequi.com.co",
        "somos@nequi.com.co",
        "notificaciones@davivienda.com",
    ]
    # Use Colombia timezone — nightly runs at 4am UTC = 11pm Colombia (next UTC day)
    hoy_str = since_date or datetime.now(COLOMBIA_TZ).date().strftime("%d-%b-%Y")
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


def _infer_payment_source(payload: dict) -> str | None:
    if payload.get("transaction_type") != "expense":
        return None

    metadata = payload.get("metadata") or {}
    text = " ".join(
        str(value)
        for value in [
            payload.get("concept"),
            payload.get("product"),
            metadata.get("payment_source"),
            metadata.get("payment_method"),
            metadata.get("card"),
            metadata.get("raw_text"),
            metadata.get("subject"),
        ]
        if value
    ).lower()

    if re.search(r"\btc\s*\d{3,4}\b", text):
        return "credit_card"
    if re.search(r"tarjeta\s+de\s+cr[eé]dito|tarjeta\s+credito|credit\s+card", text):
        return "credit_card"
    if re.search(r"\bnequi\b|d[eé]bito|debito|transferencia|cuenta\s+de\s+ahorros", text):
        return "debit"
    if re.search(r"efectivo|cash", text):
        return "cash"

    return None


def _normalize_transaction_payload(payload: dict) -> dict:
    normalized = dict(payload)
    payment_source = normalized.get("payment_source") or _infer_payment_source(normalized)

    if payment_source:
        normalized["payment_source"] = payment_source

    normalized.pop("credit_card_status", None)

    return normalized


def build_tool_map(api: RailsApiPort, messenger: MessengerPort,
                   target_date: datetime | None = None) -> dict:
    now_col = target_date or datetime.now(COLOMBIA_TZ)

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
        since = now_col.date().strftime("%d-%b-%Y")
        return _fetch_gmail_emails(since_date=since)

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
            payload = _normalize_transaction_payload(inp)
            r = httpx.post(
                f"{API_BASE_URL}/api/v1/transactions",
                headers=build_auth_headers(),
                json=payload, timeout=15,
            )
            if r.status_code == 201:
                data = r.json()["data"]
                return {"ok": True, "created": True, "id": data["id"],
                        "concept": data["attributes"]["concept"],
                        "amount": data["attributes"]["amount"],
                        "status": data["attributes"]["status"],
                        "payment_source": data["attributes"].get("payment_source")}
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

    def get_debts(_input: dict) -> dict:
        import httpx
        try:
            r = httpx.get(
                f"{API_BASE_URL}/api/v1/debts",
                headers=build_auth_headers(),
                timeout=15,
            )
            r.raise_for_status()
            items = r.json().get("data", [])
            debts = [
                {
                    "id":              d.get("id"),
                    "name":            d.get("attributes", d).get("name"),
                    "status":          d.get("attributes", d).get("status"),
                    "current_balance": d.get("attributes", d).get("current_balance"),
                    "monthly_payment": d.get("attributes", d).get("monthly_payment"),
                }
                for d in items
            ]
            active = [d for d in debts if d["status"] == "active"]
            paid   = [d for d in debts if d["status"] == "paid_off"]
            total_balance   = sum(d["current_balance"] or 0 for d in active)
            monthly_payment = sum(d["monthly_payment"] or 0 for d in active)
            months_left = round(total_balance / monthly_payment) if monthly_payment > 0 else None
            return {
                "ok":             True,
                "debts":          debts,
                "total":          len(debts),
                "active_count":   len(active),
                "paid_off_count": len(paid),
                "total_balance":  total_balance,
                "monthly_payment": monthly_payment,
                "months_to_payoff": months_left,
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def get_night_metrics(_input: dict) -> dict:
        """Pre-contextualiza los datos del día: clasifica transacciones como matched/unmatched."""
        import httpx
        today = now_col.date().isoformat()
        try:
            r = httpx.get(
                f"{API_BASE_URL}/api/v1/night_analyses/metrics",
                headers=build_auth_headers(),
                params={"date": today},
                timeout=20,
            )
            r.raise_for_status()
            return {"ok": True, **r.json().get("data", {})}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def create_night_analysis(inp: dict) -> dict:
        """Persiste el análisis nocturno + insight del dashboard. Llamar al FINAL del ciclo."""
        import httpx
        try:
            r = httpx.post(
                f"{API_BASE_URL}/api/v1/night_analyses",
                headers=build_auth_headers(),
                json=inp,
                timeout=30,
            )
            r.raise_for_status()
            return {"ok": True, **r.json().get("data", {})}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    return {
        "get_night_metrics":             get_night_metrics,
        "get_completeness":              get_completeness,
        "get_telegram_messages":         get_telegram_messages,
        "get_gmail_emails":              get_gmail_emails,
        "get_transactions":              get_transactions,
        "get_balance":                   get_balance,
        "get_pending_transactions":      get_pending_transactions,
        "get_summary":                   get_summary,
        "get_debts":                     get_debts,
        "create_transaction":            create_transaction,
        "update_transaction":            update_transaction,
        "send_telegram":                 send_telegram,
        "send_poll":                     send_poll,
        "create_milestone":              lambda p: api.create_milestone(p["code"], p.get("metadata", {})),
        "create_night_analysis":         create_night_analysis,
    }


# ── Herramientas para Claude ──────────────────────────────────────────────────

TOOLS = [
    {
        "name": "get_night_metrics",
        "description": (
            "Pre-contextualiza los datos del día antes de procesar Gmail/Telegram. "
            "Devuelve transactions_context.matched (gastos ESPERADOS con recurring_obligation — NO alarmar), "
            "transactions_context.unmatched (gastos sin obligación — revisar), "
            "category_alerts (estado por categoría vs presupuesto) y health_status/commitment_gap. "
            "Llamar PRIMERO, antes de get_gmail_emails."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
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
            "Resumen completo del mes: balance, burn_rate por categoría, monthly_plan, overflow_status, cash_flow_runway, deudas y contexto financiero. "
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
            "La dedup la maneja la API: mismo date+amount+product+tipo = rechazado para fuentes telegram/gmail. "
            "Incluí payment_source cuando el correo indique el medio de pago: credit_card para compras con tarjeta de crédito, "
            "debit para débito/Nequi/transferencia/cuenta de ahorros, cash para efectivo. "
            "Para ingresos esperados, podés pasar income_source_id; si no, la API intentará vincularlos automáticamente. "
            "Para pagos de obligaciones recurrentes esperadas, podés pasar recurring_obligation_id. "
            "Si el pago corresponde a otro mes, agregá metadata.applies_to_month/year y prepaid_obligation=true."
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
	                "payment_source":   {"type": "string", "enum": ["credit_card", "debit", "cash"]},
	                "income_source_id": {"type": "integer"},
	                "recurring_obligation_id": {"type": "integer"},
	                "metadata":         {"type": "object"},
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
	                "income_source_id": {"type": "integer"},
	                "recurring_obligation_id": {"type": "integer"},
	                "metadata":         {"type": "object"},
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
        "name": "get_debts",
        "description": (
            "Devuelve todas las deudas con estado, saldo y cuota mensual. "
            "Llámalo cuando financial_context.phase=debt_payoff para calcular ritmo de liquidación. "
            "La respuesta incluye active_count, paid_off_count y months_to_payoff calculados."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "create_night_analysis",
        "description": (
            "Persiste el análisis nocturno completo + el insight del dashboard. "
            "Llamar SIEMPRE al FINAL del ciclo, después de send_telegram. "
            "insight.title y body: 1-2 oraciones de coaching directo. "
            "kind = congratulation si comfortable, alert si critical/warning, tip para el resto. "
            "Si hay transacciones que no pudiste resolver (clasificar, deduplicar, o confirmar como deuda), "
            "inclúyelas en metrics.transactions_context.needs_review para que el usuario las revise en la app."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "YYYY-MM-DD"},
                "metrics": {
                    "type": "object",
                    "properties": {
                        "health_status":      {"type": "string", "enum": ["comfortable", "warning", "critical"]},
                        "commitment_gap":     {"type": "integer"},
                        "daily_burn":         {"type": "integer"},
                        "days_to_next_income": {"type": "integer"},
                        "category_alerts":   {"type": "array"},
                        "transactions_context": {
                            "type": "object",
                            "description": "Contexto de transacciones del día. needs_review = transacciones que el agente no pudo resolver.",
                            "properties": {
                                "matched":   {"type": "array"},
                                "unmatched": {"type": "array"},
                                "needs_review": {
                                    "type": "array",
                                    "description": "Transacciones que requieren revisión manual del usuario.",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "transaction_id": {"type": "integer", "description": "ID de la transacción en cuestión"},
                                            "concept":  {"type": "string"},
                                            "amount":   {"type": "integer"},
                                            "date":     {"type": "string"},
                                            "reason":   {"type": "string", "enum": ["unconfirmed", "no_classification", "deduplication_risk", "possible_debt"]},
                                            "notes":    {"type": "string", "description": "Una línea explicando qué no pudiste resolver"},
                                            "suggested_subcategory_code": {"type": "string", "description": "Tu hipótesis de subcategoría si aplica"},
                                        },
                                        "required": ["transaction_id", "concept", "amount", "date", "reason"],
                                    },
                                },
                            },
                        },
                    },
                    "required": ["health_status", "commitment_gap", "daily_burn"],
                },
                "agent_reasoning": {"type": "string", "description": "Razonamiento interno del agente — qué observaste y por qué tomaste estas decisiones. Siempre en español."},
                "insight": {
                    "type": "object",
                    "properties": {
                        "kind":  {"type": "string", "enum": ["tip", "congratulation", "alert", "proposal", "achievement"]},
                        "title": {"type": "string"},
                        "body":  {"type": "string"},
                    },
                    "required": ["kind", "title", "body"],
                },
            },
            "required": ["date", "metrics", "insight"],
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
    Returns the ALERTA FIN DE MES section text.
    Only informs the agent it's end-of-month so it can mention it in the summary.
    No wizard CTA — the user opens the plan wizard from the dashboard.
    """
    day       = me["day_of_month"]
    days_left = me["days_until_month_end"]
    nm_name   = me["next_month_name"]
    dias_word = "día" if days_left == 1 else "días"

    if not me["is_month_end"]:
        return f"Hoy es día {day} del mes. No es fin de mes — omitir esta sección completamente."

    return (
        f"Hoy es día {day} del mes — zona de cierre. "
        f"Faltan {days_left} {dias_word} para que empiece {nm_name}. "
        f"Mencionalo brevemente en el resumen (ej: 'Mayo empieza en {days_left} {dias_word}'). "
        f"NO incluir ningún CTA de plan, botones de wizard, ni preguntar si arman el plan — "
        f"el usuario gestiona eso desde el dashboard."
    )


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

discretionary (Flexible):
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

═══ REGLA — TARJETA DE CRÉDITO ═══
Compras con TC: registrar con create_transaction y payment_source="credit_card". El gasto se imputa al momento de la compra, como cualquier otro gasto confirmado.

Abonos/pagos al banco (email dice "Abono TC", "Pago TC", "Pago tarjeta", "se han abonado", "pago mínimo", o similar): NO crear transacción — es una transferencia entre el banco y la tarjeta, no un gasto nuevo. Ignorar.

Cuotas de deuda diferida en TC (celular a cuotas, etc.): registrar con payment_source="debit" — es plata saliendo de la cuenta de ahorros para servir una deuda ya capturada como Debt + recurring_obligation.

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
- Sé específico: "Flexible va en $762k proyectado vs $500k presupuestado"
- Si no hay presupuestos configurados, omite esta sección sin mencionarla

═══ NUEVO: ESTADO DEL PLAN QUINCENAL ═══
Si es día 1-5 o 15-20 del mes, menciona al final del resumen:
- Si hay budgets configurados: "✅ Plan del mes aprobado"
- Si NO hay budgets: "📋 Falta aprobar el plan del mes — el wizard te lo envía esta mañana"

═══ SALUD DEL FLUJO DE CAJA ═══
Si get_summary devuelve cash_flow_runway, usalo como señal principal de liquidez operativa:
- cash_flow_runway.health_status: "comfortable" | "warning" | "critical"
  - comfortable: hay margen suficiente hasta el próximo ingreso — podés sugerir mover dinero
  - warning: el margen es menor a 2 días de gasto — mencioná la estrechez antes de cualquier sugerencia
  - critical: el saldo no alcanza para llegar al próximo ingreso — NO recomiendes mover nada
- cash_flow_runway.commitment_gap: lo que sobra (o falta si es negativo) después de cubrir obligaciones y burn hasta el próximo ingreso
- cash_flow_runway.days_to_next_income: días hasta el próximo ingreso
- cash_flow_runway.committed_obligations: lista de obligaciones que vencen antes del próximo ingreso
- cash_flow_runway.daily_necessary_burn: gasto diario promedio en categoría "necessary"

REGLAS ESTRICTAS DE FLUJO:
- Si health_status == "critical": NO recomiendes abonar a deuda, inversión ni colchón. El usuario puede no llegar al próximo ingreso.
- Si health_status == "warning": nombrá el margen estrecho primero. Cualquier sugerencia va condicionada.
- Si health_status == "comfortable": podés sugerir mover hasta commitment_gap (si es positivo).
- Ninguna recomendación puede superar commitment_gap cuando es positivo.
- overflow_status.realized_overflow = ingreso extra sobre el plan base — no es dinero libre si el runway está ajustado.
- El ingreso extra NO debe presentarse como permiso para inflar el presupuesto base.

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

═══ NARRATIVA DE PROGRESO EN DEUDAS ═══
Si financial_context.phase == "debt_payoff":
- Llamá get_debts después de get_summary.
- Calculá y mencioná en el resumen: "X/Y deudas liquidadas. A este ritmo, faltan ~Z meses."
  X = paid_off_count, Y = active_count + paid_off_count, Z = months_to_payoff.
- Si months_to_payoff es null (no hay pagos registrados), omití el estimado de meses.
- Ubicalo al final del resumen financiero, antes de las alertas de presupuesto.

═══ DETECCIÓN AUTOMÁTICA DE HITOS ═══
Durante la revisión nocturna, después de obtener get_summary y get_balance, verificá si alguna condición de hito aplica para HOY y llamá create_milestone si corresponde. Es idempotente — si ya existe para el día, no pasa nada.

Condiciones a revisar:
- balance.balance_confirmed > 0 al final del mes (día ≥ 28) → month_positive_balance (metadata: {{amount: balance_confirmed}})
- burn_rate.categories alguna con spent < budget * 0.9 en categoría discretionary → discretionary_under_budget
- overflow_status.realized_overflow > 0 y overflow_status.rule != null → overflow_deployed solo si hay evidencia de abono extra a deuda/ahorro
- plan no confirmado (monthly_plan.status != "confirmed") al día 5+ → plan_not_confirmed

No llames create_milestone por condiciones que no se verificaron con datos reales de la API.

═══ TRANSACCIONES ESPERADAS (NO alarmar) ═══
get_night_metrics devuelve transactions_context.matched: gastos del día que YA tienen recurring_obligation_id (arriendo, crédito, seguro, etc.).
- Son ESPERADOS — el usuario los programó previamente. No mencionarlos como alertas en Telegram.
- Solo mencionar si el delta es > 5% del monto esperado (ej: pagaron $2.6M en lugar de $2.5M → mencionarlo).
- transactions_context.unmatched = gastos sin obligación → estos sí necesitan lectura conductual.

═══ LISTA PARA REVISIÓN DEL USUARIO ═══
La app tiene una sección dedicada donde el usuario resuelve conflictos que vos no podés resolver solo. Cada ítem en needs_review aparece con tu nota y dos botones: "Confirmar/Aplicar" o "Editar". Los conflictos NO se resuelven solos — si no los incluís en el análisis de hoy, el usuario no los ve.

TIPOS DE CONFLICTO:

0. unconfirmed: creaste la transacción como pending porque no estabas seguro de que fuera real o querías que el usuario la confirme. El usuario puede confirmarla o editarla directamente desde la app. REGLA: toda transacción que registres con status="pending" DEBE aparecer en needs_review con reason="unconfirmed".

1. no_classification: no hay suficiente contexto para asignar subcategoría y la transacción quedó con subcategory_code=null. El usuario abre el editor para clasificarla.

2. deduplication_risk: el mismo monto aparece en fuentes distintas (Gmail + Telegram) pero el producto/concepto no coincide exactamente — no estás seguro si es el mismo gasto o dos distintos. El usuario puede eliminar el duplicado o confirmar que son distintos.

3. possible_debt: el concepto sugiere pago a entidad crediticia (ej. "Abono préstamo", "Cuota libre inversión") pero no está registrado como Debt ni recurring_obligation. El usuario abre el editor para vincularlo.

CÓMO MARCAR CONFLICTOS — REGLA ARQUITECTURAL:
Los conflictos NO van en create_night_analysis. Van directamente en la transacción mediante update_transaction con metadata. La app los enlista en tiempo real consultando las transacciones.

- unconfirmed: simplemente dejá la transacción con status="pending". La app la detecta automáticamente.
  No hace falta metadata adicional salvo que quieras dejar una nota: metadata={{ "conflict_notes": "..." }}

- no_classification, deduplication_risk, possible_debt: usá update_transaction con:
  metadata={{ "conflict_reason": "deduplication_risk", "conflict_notes": "Monto $120k en Gmail no coincide con Nequi del mismo día — producto distinto", "suggested_subcategory_code": "creditos" }}
  La transacción puede quedar confirmed o pending según el caso.

El campo needs_review en create_night_analysis ya no se usa — no lo incluyas.

CARRY-OVER: Las transacciones pending de días anteriores siguen apareciendo en la app automáticamente (la app consulta todas las pending de la cuenta). No necesitás re-listarlas vos.

REGLA ESTRICTA: Solo marcá conflictos genuinos. Si tenés suficiente información → resolvé directamente.

═══ FLUJO RECOMENDADO ═══
1. get_night_metrics → pre-contextualizar: saber qué transacciones son ESPERADAS antes de procesar Gmail
2. get_completeness → detectar gaps de contexto
3. get_summary → alertas de presupuesto + estado plan quincenal + overflow si aplica
4. get_telegram_messages → transacciones ya registradas hoy desde el chat (source=telegram)
5. get_gmail_emails → cargos bancarios del día
6. Cruzar Gmail vs Telegram: si coinciden monto+producto → mismo gasto, NO duplicar
7. Si una transacción en Gmail coincide con una en transactions_context.matched → también es esperada, NO alarmar
8. get_transactions → lista completa del mes para dedup adicional (NO para balance)
9. get_balance → balance real (SIEMPRE antes del resumen)
10. get_pending_transactions → pendientes de días anteriores
11. Registrar solo los gastos de Gmail que NO estén ya en Telegram/transactions → create_transaction
12. Para gastos inciertos → create_transaction(pending) + send_telegram con botones
13. Resolver subcategorías pendientes: get_transactions → asignar las que se puedan → agrupar ambiguas
14. send_telegram → resumen con sección ⚙️ de gaps si aplica + sección 📂 de subcategorías pendientes si aplica
15. Fin de mes (días 28–31): mencionarlo brevemente en el resumen ("Mayo empieza en X días"). Sin CTA, sin botones de wizard.
16. create_night_analysis → SIEMPRE al final. Persistir análisis + insight del dashboard.
    insight.kind = congratulation si comfortable y balance positivo | alert si critical o warning con gap negativo | achievement si hay milestone reciente | tip para el resto.
    insight.title y body: 1-2 oraciones de coaching directo en español, basadas en datos reales.
    Si hay transacciones sin resolver → incluirlas en metrics.transactions_context.needs_review.

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


def run_nightly(api: RailsApiPort, messenger: MessengerPort,
                target_date: datetime | None = None) -> None:
    now_col = target_date or datetime.now(COLOMBIA_TZ)
    fecha = now_col.strftime("%d/%m/%Y")
    print(f"\n=== Revisión nocturna Brain — {fecha} ===\n")

    tool_map = build_tool_map(api, messenger, target_date=now_col)

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
