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
from services.transaction_rules import (
    TRANSACTION_CREATION_RULES,
    normalize_categories,
    quick_category_buttons,
    should_force_pending,
    transaction_guard_reason,
)

logger = logging.getLogger(__name__)

COLOMBIA_TZ = timezone(timedelta(hours=-5))

GMAIL_HISTORY_URL = "https://gmail.googleapis.com/gmail/v1/users/me/history"
GMAIL_MESSAGE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/{}"


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
    reason = transaction_guard_reason(_email_text(email))
    return bool(reason), reason


def _force_pending_email(email: dict) -> bool:
    return should_force_pending(_email_text(email))


def _fetch_today_transactions(account_id: int) -> list[dict]:
    """Transacciones ya registradas hoy — contexto para que el agente NO duplique
    un mismo pago que llega en dos correos distintos (ej: notificación del banco +
    confirmación del operador/PSE) con remitentes o textos diferentes."""
    from agents.nightly import _flatten_transaction

    now_col = datetime.now(COLOMBIA_TZ)
    try:
        resp = httpx.get(
            f"{API_BASE_URL}/api/v1/transactions",
            headers=build_auth_headers(str(account_id)),
            params={"month": now_col.month, "year": now_col.year, "page": 1, "per_page": 100},
            timeout=15,
        )
        resp.raise_for_status()
        rows = resp.json().get("data", [])
    except Exception as e:
        logger.warning("[GmailPush] fetch today transactions error: %s", e)
        return []

    today = now_col.strftime("%Y-%m-%d")
    return [t for t in (_flatten_transaction(r) for r in rows) if t.get("date") == today]


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
    category_cache: list[dict] = []

    def emit_ui_event(event_type: str, payload: dict) -> None:
        try:
            response = httpx.post(
                f"{API_BASE_URL}/api/v1/agent_events",
                headers=headers,
                json={"event_type": event_type, "payload": payload},
                timeout=15,
            )
            response.raise_for_status()
        except Exception as e:
            logger.warning("[GmailPush] emit_ui_event %s error: %s", event_type, e)

    def notify_transaction_created(transaction: dict, payload: dict) -> None:
        txn_id = transaction.get("id")
        attrs = transaction.get("attributes", {})
        status = attrs.get("status") or payload.get("status")
        concept = attrs.get("concept") or payload.get("concept") or "Transacción"
        amount = attrs.get("amount") or payload.get("amount")
        transaction_type = attrs.get("transaction_type") or payload.get("transaction_type")
        subcategory_code = attrs.get("subcategory_code") or payload.get("subcategory_code")

        amount_text = f"${int(float(amount)):,}".replace(",", ".") if amount is not None else "monto pendiente"
        title = "Transacción creada desde Gmail"
        body = f"Creé {concept} por {amount_text}."
        emit_ui_event("data_changed", {"resource": "transactions"})

        needs_review = status == "pending" or not subcategory_code
        if not needs_review:
            emit_ui_event(
                "show_card",
                {
                    "title": title,
                    "body": f"{body} Clasificación: {subcategory_code}.",
                    "tone": "success",
                    "transaction_id": txn_id,
                },
            )
            return

        if not category_cache:
            get_categories({})

        buttons = []
        if status == "pending":
            buttons.append({"text": "Confirmar", "callback_data": f"confirm:{txn_id}"})
        buttons.extend(
            {"text": button["text"], "callback_data": f"cat:{txn_id}:{button['code']}"}
            for button in quick_category_buttons(category_cache, transaction_type)
        )
        buttons.append({"text": "Luego", "callback_data": f"skip:{txn_id}"})

        emit_ui_event(
            "show_quick_replies",
            {
                "title": title,
                "body": f"{body} Necesito que revises la clasificación.",
                "buttons": buttons[:7],
                "transaction_id": txn_id,
            },
        )

    def get_categories(_inp: dict) -> dict:
        nonlocal category_cache
        try:
            r = httpx.get(
                f"{API_BASE_URL}/api/v1/categories",
                headers=headers,
                timeout=15,
            )
            r.raise_for_status()
            rows = r.json().get("data", [])
            category_cache = normalize_categories(rows)
            return {"ok": True, "categories": category_cache}
        except Exception as e:
            logger.warning("[GmailPush] get_categories error: %s", e)
            return {"ok": False, "error": str(e)}

    def get_classification_hints(inp: dict) -> dict:
        merchant = (inp.get("merchant") or "").strip()
        if not merchant:
            return {"ok": False, "error": "merchant requerido"}
        try:
            r = httpx.get(
                f"{API_BASE_URL}/api/v1/transactions/classification_hints",
                headers=headers,
                params={"merchant": merchant},
                timeout=15,
            )
            r.raise_for_status()
            return {"ok": True, **(r.json().get("data") or {})}
        except Exception as e:
            logger.warning("[GmailPush] get_classification_hints error: %s", e)
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
            guard_reason = transaction_guard_reason(raw_text)
            if guard_reason:
                logger.info(
                    "[GmailPush] blocked transaction source_event_id=%s reason=%s concept=%s",
                    source_event_id,
                    guard_reason,
                    payload.get("concept"),
                )
                return {"ok": True, "created": False, "ignored": True, "reason": guard_reason}
            if should_force_pending(raw_text) and payload.get("status") != "pending":
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
                notify_transaction_created(data, payload)
                return {"ok": True, "created": True, "id": data["id"],
                        "concept": data["attributes"]["concept"],
                        "amount": data["attributes"]["amount"],
                        "status": data["attributes"]["status"],
                        "subcategory_code": data["attributes"].get("subcategory_code"),
                        "payment_source": data["attributes"].get("payment_source")}
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

    return {
        "get_categories": get_categories,
        "get_classification_hints": get_classification_hints,
        "create_transaction": create_transaction,
    }


TOOLS = [
    {
        "name": "get_categories",
        "description": "Devuelve categorías y subcategorías disponibles. Usala antes de create_transaction para elegir subcategory_code.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_classification_hints",
        "description": (
            "Devuelve el historial de clasificación del usuario para un comercio: candidates con "
            "subcategory_code, count, share y typical_hours, más dominant (ya calculado por la API) "
            "cuando el patrón es consistente. Llamala ANTES de clasificar cada transacción, "
            "pasando el nombre del comercio tal como aparece en el correo."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "merchant": {"type": "string", "description": "Nombre del comercio del correo, ej: 'BOLD CAMILO 1789'"},
            },
            "required": ["merchant"],
        },
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
- DEDUPLICACIÓN SEMÁNTICA: un mismo pago suele generar DOS correos (banco + operador/PSE,
  o débito + comprobante) con remitentes y textos distintos pero el mismo monto. Antes de
  registrar, revisá la lista "TRANSACCIONES YA REGISTRADAS HOY" del mensaje: si el movimiento
  ya está ahí (mismo monto y misma naturaleza, aunque el concepto difiera), NO lo dupliques.
  El source_event_id solo evita reprocesar el MISMO correo; no detecta el mismo pago en dos
  correos distintos — eso es tu responsabilidad.
- SIEMPRE incluir metadata.source_event_id = id del correo (campo "id" de cada email).
- SIEMPRE incluir metadata.raw_text y metadata.subject con el texto fuente relevante.
- source SIEMPRE es "gmail".
- Si menciona débito/Nequi/transferencia: payment_source="debit".
- Clasificación con memoria de patrones — el historial del usuario decide, no tu intuición:
  1. Antes de clasificar, llamá get_classification_hints con el comercio tal como aparece en el correo (ej: "BOLD CAMILO 1789").
  2. Si la respuesta trae dominant → usá ese subcategory_code, status="confirmed" y metadata.classification_source="pattern". No preguntes.
  3. Si trae 2+ candidates sin dominant → status="pending", subcategory_code del primer candidate,
     metadata.suggested_subcategories=[códigos de los 2 primeros] y metadata.classification_source="agent".
     Las typical_hours son evidencia: si la hora del correo coincide con las de un candidate, ese va primero.
  4. Si samples=0 → usá get_categories y tu mejor juicio; metadata.classification_source="agent".
     Subcategoría clara → confirmed; con duda real → pending o sin subcategory_code para que aparezca en revisión.
- Davivienda puede enviar correos solo en HTML o snippets con asunto "DAVIVIENDA".
  Si el texto trae "Valor Transacción", fecha y monto, es financiero aunque el cuerpo sea breve.
- No tienes herramienta de mensajería directa. create_transaction avisará a la app cuando cree una transacción
  y pedirá clasificación con botones si queda pendiente o sin subcategoría.
- Si un correo no tiene suficiente información para determinar monto o tipo, ignóralo.
- Si un correo trae "Force pending: sí", es un movimiento marcado como dudoso (transferencia entrante,
  o transferencia saliente a una llave/Bre-B sin destinatario claro): SIEMPRE registralo con
  create_transaction status="pending". NUNCA lo ignores — el usuario lo revisará y clasificará después.
""" + "\n\n" + TRANSACTION_CREATION_RULES


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

    today_txns = _fetch_today_transactions(account_id)
    if today_txns:
        already = "\n".join(
            f"- id={t['id']} {t.get('type')} ${t.get('amount')} {(t.get('concept') or '').strip()}"
            for t in today_txns
        )
        initial_message += (
            "\n\n═══ TRANSACCIONES YA REGISTRADAS HOY (no las dupliques) ═══\n"
            f"{already}\n\n"
            "Un mismo pago suele llegar en DOS correos distintos (ej: notificación del banco + "
            "confirmación del operador/PSE, o débito de cuenta + comprobante) con remitentes y "
            "textos diferentes pero el MISMO monto. Si el movimiento de un correo ya está en la "
            "lista de arriba (mismo monto y misma naturaleza, aunque el concepto difiera), NO lo "
            "registres de nuevo. Lo mismo aplica entre los correos de este lote: si dos describen "
            "el mismo pago, registralo una sola vez."
        )

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
