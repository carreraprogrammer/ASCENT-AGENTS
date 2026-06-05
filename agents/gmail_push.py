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


def _get_header(payload: dict, name: str) -> str:
    for h in payload.get("headers", []):
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _extract_part(part: dict) -> str:
    mime = part.get("mimeType", "")
    body = part.get("body", {})
    data = body.get("data", "")
    parts = part.get("parts", [])

    if mime == "text/plain" and data:
        try:
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="ignore")
        except Exception:
            return ""
    if parts:
        return "".join(_extract_part(p) for p in parts)
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
        payload = resp.json().get("payload", {})

        from_raw = _get_header(payload, "From")
        subject  = _get_header(payload, "Subject")
        raw      = _extract_part(payload)
        raw      = re.sub(r"<[^>]+>", " ", raw)
        raw      = re.sub(r"\s+", " ", raw).strip()[:3000]

        if not raw:
            return None

        return {"id": message_id, "from": from_raw, "subject": subject, "body": raw}
    except Exception as e:
        logger.warning("[GmailPush] fetch message %s error: %s", message_id, e)
        return None


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

    def create_transaction(inp: dict) -> dict:
        try:
            payload = _normalize_transaction_payload(inp)
            r = httpx.post(
                f"{API_BASE_URL}/api/v1/transactions",
                headers=headers,
                json=payload,
                timeout=15,
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
                        "detail": body.get("errors", [{}])[0].get("detail", "")}
            return {"ok": False, "status_code": r.status_code, "error": r.text[:300]}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    return {"create_transaction": create_transaction}


TOOLS = [
    {
        "name": "create_transaction",
        "description": (
            "Registra una transacción financiera detectada en el correo. "
            "SIEMPRE incluí metadata.source_event_id = id del correo Gmail para prevenir duplicados. "
            "Si ya_existed=true, la transacción ya fue registrada por el análisis nocturno — no es un error. "
            "source debe ser 'gmail'. status='confirmed' si el monto y tipo son claros, 'pending' si hay duda."
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
2. Si es financiero: extraer fecha, concepto, monto y tipo (expense/income) y llamar create_transaction.
3. Si NO es financiero (publicidad, newsletters, confirmaciones de cuenta): ignorar completamente.

Reglas críticas:
- SIEMPRE incluir metadata.source_event_id = id del correo (campo "id" de cada email).
- source SIEMPRE es "gmail".
- Si el correo menciona tarjeta de crédito/TC: payment_source="credit_card".
- Si menciona débito/Nequi/transferencia: payment_source="debit".
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
            emails.append(msg)

    if not emails:
        logger.info("[GmailPush] account=%s mensajes sin contenido útil", account_id)
        return

    logger.info("[GmailPush] account=%s procesando %d correos", account_id, len(emails))

    emails_text = "\n\n---\n\n".join(
        f"[ID: {e['id']}]\nDe: {e['from']}\nAsunto: {e['subject']}\n\n{e['body']}"
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
