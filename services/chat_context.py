"""Helpers de contexto, parsing y normalización para el chat financiero."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

from ports.messenger import ParsedUpdate

COLOMBIA_TZ = timezone(timedelta(hours=-5))


def normalize_telegram_html(text: str) -> str:
    if not text:
        return text

    normalized = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    normalized = re.sub(r"__(.+?)__", r"<i>\1</i>", normalized)
    return normalized


def flatten_transaction(raw: dict) -> dict:
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


def parse_api_date(raw_date: str | None, fallback_year: int | None = None) -> date | None:
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


def event_source_id(parsed: ParsedUpdate) -> str | None:
    raw = parsed.raw or {}
    message = raw.get("message") or {}

    message_id = message.get("message_id")
    if message_id is not None:
        return f"telegram:message:{message_id}"

    update_id = raw.get("update_id")
    if update_id is not None:
        return f"telegram:update:{update_id}"

    return None


def telegram_context(parsed: ParsedUpdate) -> str:
    message_payload = parsed.raw.get("message", {}) if parsed.raw else {}
    telegram_ts = message_payload.get("date")

    if telegram_ts:
        ts_col = datetime.fromtimestamp(telegram_ts, tz=timezone.utc).astimezone(COLOMBIA_TZ)
        return (
            f"Fecha real del mensaje: {ts_col.strftime('%d/%m/%Y')}\n"
            f"Hora real en Colombia: {ts_col.strftime('%H:%M')}\n"
            f"source_event_id técnico: {event_source_id(parsed) or 'no-disponible'}\n"
            "Si el usuario no especifica fecha, usá esa fecha del mensaje. "
            "Si creás una transacción, mandá date como DD/MM/YYYY."
        )

    now_col = datetime.now(COLOMBIA_TZ)
    return (
        f"Fecha actual en Colombia: {now_col.strftime('%d/%m/%Y')}\n"
        f"Hora actual en Colombia: {now_col.strftime('%H:%M')}\n"
        f"source_event_id técnico: {event_source_id(parsed) or 'no-disponible'}\n"
        "Si el usuario no especifica fecha, usá la fecha actual en Colombia. "
        "Si creás una transacción, mandá date como DD/MM/YYYY."
    )
