"""
agents/gmail_push.py — Procesador de notificaciones push de Gmail en tiempo real.

Flujo:
  1. Rails recibe el webhook de Pub/Sub y llama POST /agents/gmail-push
  2. Este agente obtiene el token de Gmail desde Rails
  3. Consulta Gmail History API para obtener solo los mensajes nuevos desde el último historyId
  4. Para cada mensaje nuevo, extrae el contenido y lo pasa al LLM
  5. El LLM decide si es financiero y crea la transacción via create_transaction

El análisis nocturno continúa como safety net — cualquier correo perdido aquí lo captura el nocturno.
source_event_id = Gmail message ID garantiza que nunca se creen duplicados.
"""

import base64
import html
import logging
import re
from datetime import datetime, timezone, timedelta

import httpx

from adapters.rails_http import BASE_URL as API_BASE_URL, build_auth_headers
from services.llm_factory import build_llm_provider, resolve_llm_model

logger = logging.getLogger(__name__)

COLOMBIA_TZ = timezone(timedelta(hours=-5))

GMAIL_HISTORY_URL = "https://gmail.googleapis.com/gmail/v1/users/me/history"
GMAIL_MESSAGE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/{}"

CARD_PAYMENT_RE = re.compile(
    r"(descuento\s+pago|pago|abono|abonad[oa]|pago\s+m[ií]nimo|d[eé]bito).*"
    r"(tarjeta\s+de\s+cr[eé]dito|tarjeta\s+credito|\btc\b)"
    r"|"
    r"(tarjeta\s+de\s+cr[eé]dito|tarjeta\s+credito|\btc\b).*"
    r"(pago|abono|abonad[oa]|pago\s+m[ií]nimo)",
    re.IGNORECASE,
)
SELF_TRANSFER_RE = re.compile(
    r"(env[ií]o|transferencia\s+enviada).*(bre-b|llave).*(daniel\s+(carrera|alejandro)|1085333083)",
    re.IGNORECASE,
)
INBOUND_TRANSFER_RE = re.compile(
    r"(abono|transferencia|recibiste|recibido).*(bre-b|llave|cta\s+de\s+ahorros|cuenta\s+de\s+ahorros)",
    re.IGNORECASE,
)


def _get_header(payload: dict, name: str) -> str:
    for h in payload.get("headers", []):
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _decode_body_data(data: str) -> str:
    if not data:
        return ""

    try:
        padding = "=" * (-len(data) % 4)
        return base64.urlsafe_b64decode(data + padding).decode("utf-8", errors="ignore")
    except Exception:
        return ""


def _html_to_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>", "\n", raw)
    raw = re.sub(r"(?i)</(p|div|tr|li|table|h[1-6])>", "\n", raw)
    raw = re.sub(r"<[^>]+>", " ", raw)
    return html.unescape(raw)


def _extract_part(part: dict) -> str:
    mime = part.get("mimeType", "")
    body = part.get("body", {})
    data = body.get("data", "")
    parts = part.get("parts", [])

    if mime == "text/plain" and data:
        return _decode_body_data(data)
    if mime == "text/html" and data:
        return _html_to_text(_decode_body_data(data))
    if parts:
        plain_parts = []
        fallback_parts = []
        for p in parts:
            text = _extract_part(p)
            if not text:
                continue
            if p.get("mimeType") == "text/plain":
                plain_parts.append(text)
            else:
                fallback_parts.append(text)
        return "\n".join(plain_parts or fallback_parts)
    return ""


def _fetch_new_messages(access_token: str, start_history_id: str) -> list[dict]:
    """Usa la History API para obtener solo los message IDs agregados desde start_history_id."""
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        resp = httpx.get(
            GMAIL_HISTORY_URL,
            headers=headers,
            params={"startHistoryId": start_history_id, "historyTypes": "messageAdded"},
            timeout=15,
        )
        if resp.status_code == 404:
            logger.warning("[GmailPush] historyId %s inválido o expirado", start_history_id)
            return []
        resp.raise_for_status()
        history = resp.json().get("history", [])
    except Exception as e:
        logger.error("[GmailPush] History API error: %s", e)
        return []

    # Extraer IDs únicos de mensajes agregados
    seen = set()
    message_ids = []
    for record in history:
        for msg in record.get("messagesAdded", []):
            mid = msg.get("message", {}).get("id")
            if mid and mid not in seen:
                seen.add(mid)
                message_ids.append(mid)

    return message_ids


def _fetch_message(access_token: str, message_id: str) -> dict | None:
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        resp = httpx.get(
            GMAIL_MESSAGE_URL.format(message_id),
            headers=headers,
            params={"format": "full"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        payload = data.get("payload", {})

        from_raw = _get_header(payload, "From")
        subject  = _get_header(payload, "Subject")
        snippet  = data.get("snippet", "")
        raw      = _extract_part(payload)
        raw      = raw or snippet
        raw      = re.sub(r"\s+", " ", raw).strip()[:3000]

        if not raw:
            return None

        return {"id": message_id, "from": from_raw, "subject": subject, "body": raw}
    except Exception as e:
        logger.warning("[GmailPush] fetch message %s error: %s", message_id, e)
        return None


def _email_text(email: dict) -> str:
    return " ".join(
        str(email.get(key, ""))
        for key in ("from", "subject", "body")
    )


def _should_ignore_email(email: dict) -> tuple[bool, str | None]:
    text = _email_text(email)
    if CARD_PAYMENT_RE.search(text):
        return True, "credit_card_payment"
    if SELF_TRANSFER_RE.search(text):
        return True, "self_transfer"
    return False, None


def _force_pending_email(email: dict) -> bool:
    text = _email_text(email)
    return bool(INBOUND_TRANSFER_RE.search(text))


def _get_gmail_token(account_id: int) -> tuple[str | None, list[str]]:
    """Obtiene access_token fresco y bank_senders desde Rails."""
    try:
        resp = httpx.get(
            f"{API_BASE_URL}/api/v1/me/email_connection/token",
            headers=build_auth_headers(str(account_id)),
            timeout=15,
        )
        if not resp.is_success:
            return None, []
        data = resp.json().get("data", {})
        return data.get("access_token"), data.get("bank_senders", [])
    except Exception as e:
        logger.error("[GmailPush] get token account=%s error: %s", account_id, e)
        return None, []


def _build_tool_map(account_id: int) -> dict:
    from agents.nightly import _normalize_transaction_payload

    headers = build_auth_headers(str(account_id))

    def get_categories(_inp: dict) -> dict:
        try:
            r = httpx.get(
                f"{API_BASE_URL}/api/v1/categories",
                headers=headers,
                timeout=15,
            )
            r.raise_for_status()
            rows = r.json().get("data", [])
            categories = []
            for raw in rows:
                attrs = raw.get("attributes", raw)
                subcategories = raw.get("relationships", {}).get("subcategories", {}).get("data", [])
                categories.append({
                    "id": raw.get("id"),
                    "name": attrs.get("name"),
                    "code": attrs.get("code"),
                    "category_type": attrs.get("category_type"),
                    "subcategories": [
                        {
                            "id": sub.get("id"),
                            "name": sub.get("attributes", {}).get("name"),
                            "code": sub.get("attributes", {}).get("code"),
                        }
                        for sub in subcategories
                    ],
                })
            return {"ok": True, "categories": categories}
        except Exception as e:
            logger.warning("[GmailPush] get_categories error: %s", e)
            return {"ok": False, "error": str(e)}

    def create_transaction(inp: dict) -> dict:
        try:
            payload = _normalize_transaction_payload(inp)
            source_event_id = (payload.get("metadata") or {}).get("source_event_id")
            raw_text = " ".join(
                str(value)
                for value in [
                    payload.get("concept"),
                    payload.get("product"),
                    (payload.get("metadata") or {}).get("raw_text"),
                    (payload.get("metadata") or {}).get("subject"),
                ]
                if value
            )
            payload["source"] = "gmail"
            if CARD_PAYMENT_RE.search(raw_text):
                logger.info(
                    "[GmailPush] blocked credit card payment source_event_id=%s concept=%s",
                    source_event_id,
                    payload.get("concept"),
                )
                return {"ok": True, "created": False, "ignored": True, "reason": "credit_card_payment"}
            if SELF_TRANSFER_RE.search(raw_text):
                logger.info(
                    "[GmailPush] blocked self transfer source_event_id=%s concept=%s",
                    source_event_id,
                    payload.get("concept"),
                )
                return {"ok": True, "created": False, "ignored": True, "reason": "self_transfer"}
            if INBOUND_TRANSFER_RE.search(raw_text) and payload.get("status") != "pending":
                logger.info(
                    "[GmailPush] forcing pending inbound transfer source_event_id=%s concept=%s",
                    source_event_id,
                    payload.get("concept"),
                )
                payload["status"] = "pending"

            logger.info(
                "[GmailPush] create_transaction amount=%s concept=%s type=%s source_event_id=%s",
                payload.get("amount"),
                payload.get("concept"),
                payload.get("transaction_type"),
                source_event_id,
            )
            r = httpx.post(
                f"{API_BASE_URL}/api/v1/transactions",
                headers=headers,
                json=payload,
                timeout=15,
            )
            if r.status_code == 201:
                data = r.json()["data"]
                logger.info("[GmailPush] create_transaction OK id=%s", data["id"])
                return {"ok": True, "created": True, "id": data["id"],
                        "concept": data["attributes"]["concept"],
                        "amount": data["attributes"]["amount"],
                        "status": data["attributes"]["status"]}
            if r.status_code == 409:
                body = r.json()
                logger.info("[GmailPush] create_transaction duplicate source_event_id=%s", source_event_id)
                return {"ok": True, "created": False, "already_existed": True,
                        "detail": body.get("errors", [{}])[0].get("detail", "")}
            logger.warning("[GmailPush] create_transaction failed status=%s body=%s", r.status_code, r.text[:300])
            return {"ok": False, "status_code": r.status_code, "error": r.text[:300]}
        except Exception as e:
            logger.error("[GmailPush] create_transaction error: %s", e)
            return {"ok": False, "error": str(e)}

    return {"get_categories": get_categories, "create_transaction": create_transaction}


TOOLS = [
    {
        "name": "get_categories",
        "description": "Devuelve categorías y subcategorías disponibles. Usala antes de create_transaction para elegir subcategory_code.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "create_transaction",
        "description": (
            "Registra una transacción financiera detectada en el correo. "
            "SIEMPRE incluí metadata.source_event_id = id del correo Gmail para prevenir duplicados. "
            "Si ya_existed=true, la transacción ya fue registrada por el análisis nocturno — no es un error. "
            "source debe ser 'gmail'. status='confirmed' si el monto, tipo y subcategoría son claros; "
            "status='pending' si hay duda real. No registra pagos/abonos de tarjeta de crédito."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date":             {"type": "string", "description": "YYYY-MM-DD"},
                "concept":          {"type": "string"},
                "amount":           {"type": "integer", "description": "En pesos colombianos, sin decimales"},
                "transaction_type": {"type": "string", "enum": ["expense", "income"]},
                "status":           {"type": "string", "enum": ["confirmed", "pending"]},
                "payment_source":   {"type": "string", "enum": ["credit_card", "debit", "cash"]},
                "subcategory_code": {"type": "string"},
                "source":           {"type": "string", "enum": ["gmail"]},
                "metadata":         {
                    "type": "object",
                    "description": "OBLIGATORIO: incluir source_event_id con el message id del correo."
                },
            },
            "required": ["date", "concept", "amount", "transaction_type", "status", "metadata"],
        },
    }
]

SYSTEM_PROMPT = """Eres el asistente financiero de Daniel 15K procesando correos bancarios en tiempo real.

Recibirás uno o más correos de Gmail. Tu tarea:
1. Determinar si cada correo es una notificación financiera (transacción bancaria, pago, transferencia, recarga).
2. Si es financiero: llamar get_categories, extraer fecha, concepto, monto, tipo y subcategoría, y llamar create_transaction.
3. Si NO es financiero (publicidad, newsletters, confirmaciones de cuenta): ignorar completamente.

Reglas críticas:
- SIEMPRE incluir metadata.source_event_id = id del correo (campo "id" de cada email).
- SIEMPRE incluir metadata.raw_text y metadata.subject con el texto fuente relevante.
- source SIEMPRE es "gmail".
- Compras con tarjeta de crédito/TC: registrar con payment_source="credit_card".
- Pagos/abonos/descuentos de tarjeta de crédito NO son gasto nuevo. Si el correo dice "Pago tarjeta",
  "Descuento Pago Tarjeta de Crédito", "Abono TC", "se han abonado", "pago mínimo" o similar: IGNORAR.
- Si menciona débito/Nequi/transferencia: payment_source="debit".
- Transferencias internas o hacia el mismo Daniel NO son ingreso/gasto real. Ignorarlas.
- Abonos/transferencias entrantes por Bre-B/llaves sin origen claro no se confirman automáticamente:
  si decides registrarlas, usa status="pending" para revisión del usuario.
- Siempre intenta asignar subcategory_code con get_categories.
  Si la subcategoría es clara, confirmed; si no, pending o sin subcategory_code para que aparezca en revisión.
- Davivienda puede enviar correos solo en HTML o snippets con asunto "DAVIVIENDA".
  Si el texto trae "Valor Transacción", fecha y monto, es financiero aunque el cuerpo sea breve.
- No envíes mensajes al usuario. Solo registra transacciones silenciosamente.
- Si un correo no tiene suficiente información para determinar monto o tipo, ignóralo."""


def run_gmail_push(account_id: int, history_id: str) -> None:
    """
    Procesa los correos nuevos desde history_id para el account dado.
    Llamado en background desde el endpoint /agents/gmail-push.
    """
    logger.info("[GmailPush] account=%s history_id=%s", account_id, history_id)

    access_token, bank_senders = _get_gmail_token(account_id)
    if not access_token:
        logger.warning("[GmailPush] account=%s sin token de Gmail", account_id)
        return

    message_ids = _fetch_new_messages(access_token, history_id)
    if not message_ids:
        logger.info("[GmailPush] account=%s sin mensajes nuevos", account_id)
        return

    emails = []
    for mid in message_ids:
        msg = _fetch_message(access_token, mid)
        if msg:
            should_ignore, reason = _should_ignore_email(msg)
            if should_ignore:
                logger.info("[GmailPush] ignoring email id=%s reason=%s", mid, reason)
                continue
            if _force_pending_email(msg):
                msg["force_pending"] = True
            emails.append(msg)

    if not emails:
        logger.info("[GmailPush] account=%s mensajes sin contenido útil", account_id)
        return

    logger.info("[GmailPush] account=%s procesando %d correos", account_id, len(emails))

    emails_text = "\n\n---\n\n".join(
        f"[ID: {e['id']}]\nDe: {e['from']}\nAsunto: {e['subject']}\n"
        f"Force pending: {'sí' if e.get('force_pending') else 'no'}\n\n{e['body']}"
        for e in emails
    )
    initial_message = f"Procesa estos {len(emails)} correo(s) de Gmail:\n\n{emails_text}"

    tool_map = _build_tool_map(account_id)
    provider = build_llm_provider()
    provider.run_agent(
        system_prompt=SYSTEM_PROMPT,
        tools=TOOLS,
        tool_map=tool_map,
        initial_message=initial_message,
        max_iterations=10,
        model=resolve_llm_model(),
    )

    logger.info("[GmailPush] account=%s completado", account_id)
