"""
agents/gmail_office.py — ASC-3: backfill clasificado por la oficina, sin LLM.

Un agente supervisado por una persona (la "oficina") hace el papel del modelo: lee los correos,
aplica el SYSTEM_PROMPT y las TRANSACTION_CREATION_RULES de agents/gmail_push.py y decide cada
transacción. Este módulo le da lo que el LLM recibe y aplica sus decisiones por el mismo camino:

  - extract_headers: los correos de un rango (misma lista y consulta de Gmail que el backfill),
    con su estado: new, known (su id ya es source_event_id), guard (_should_ignore_email) o unreadable;
  - extract_bodies: el mismo texto que gmail_push le da al LLM y el force_pending del correo;
  - day_context: "TRANSACCIONES YA REGISTRADAS HOY" de un día pasado;
  - categories / hints: las herramientas get_categories y get_classification_hints de _build_tool_map;
  - apply_decisions: cada decisión pasa por las guardas del correo (_prepare_email), las reglas de
    clasificación por historial (get_classification_hints), la deduplicación contra el día y la
    envoltura del backfill (guarded_create: ids conocidos, quitar metas, create_transaction de
    _build_tool_map(notify=False), que aplica transaction_guard_reason y should_force_pending).

Nada se escribe a disco ni a logs. apply_decisions devuelve solo conteos.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, datetime
from email.utils import parseaddr
from typing import Callable, Iterable, Iterator

import httpx

from agents.gmail_backfill import (
    BackfillDeps,
    _make_dry_run_recorder,
    _sid,
    guarded_create,
    month_key,
)
from agents.gmail_push import (
    COLOMBIA_TZ,
    GMAIL_MESSAGE_URL,
    TOOLS,
    _fetch_day_transactions,
    _force_pending_email,
    _get_header,
    _prepare_email,
    _should_ignore_email,
)

SUBJECT_MAX = 120
DEFAULT_MAX_CHARS = 1500
MAX_BODY_IDS = 25

SKIP_REASONS = ("non_financial", "duplicate", "transfer_own", "other")
_CREATE_SCHEMA = next(t for t in TOOLS if t["name"] == "create_transaction")["input_schema"]["properties"]
# Campos que acepta create_transaction (TOOLS) más product (lo normaliza el payload) y los vínculos a
# metas, que se aceptan solo para quitarlos como hace el backfill.
TRANSACTION_FIELDS = set(_CREATE_SCHEMA) | {"product", "savings_goal_id", "sinking_fund_id"}

APPLY_KEYS = (
    "decisions", "invalid",
    "created", "pending", "guard_email", "guard_tool", "duplicate_id", "duplicate_day",
    "unreadable", "force_pending", "goal_link_stripped", "errors",
    "hints_dominant", "hints_ambiguous", "hints_undetermined", "hints_error",
) + tuple(f"skipped_{r}" for r in SKIP_REASONS)


# ── Remitente sin parte local ─────────────────────────────────────────────────

_EMAIL_RE = re.compile(r"[\w.!#$%&'*+/=?^`{|}~-]+@([\w-]+(?:\.[\w-]+)+)")


def sender_label(from_raw: str) -> str:
    """'Banco <alertas@banco.test>' → 'Banco @banco.test'. Nunca la parte local de un correo."""
    name, addr = parseaddr(from_raw or "")
    domain = addr.rsplit("@", 1)[1] if "@" in addr else ""
    name = _EMAIL_RE.sub(r"@\1", name or "").strip().strip('"').strip()
    if not domain:  # sin dirección parseable: se limpia todo el texto
        return _EMAIL_RE.sub(r"@\1", from_raw or "").strip()[:SUBJECT_MAX]
    label = f"{name} @{domain}" if name and name != f"@{domain}" else f"@{domain}"
    return label[:SUBJECT_MAX]


def _subject(subject: str) -> str:
    return re.sub(r"\s+", " ", subject or "").strip()[:SUBJECT_MAX]


# ── Gmail (solo lectura) ──────────────────────────────────────────────────────

def fetch_message_day(access_token: str, message_id: str) -> date | None:
    """Día (hora de Colombia) en que llegó el correo, por su internalDate. GET format=minimal."""
    try:
        resp = httpx.get(GMAIL_MESSAGE_URL.format(message_id),
                         headers={"Authorization": f"Bearer {access_token}"},
                         params={"format": "minimal"}, timeout=15)
        resp.raise_for_status()
        ms = int(resp.json().get("internalDate"))
        return datetime.fromtimestamp(ms / 1000, COLOMBIA_TZ).date()
    except Exception:
        return None


def _known_ids(deps: BackfillDeps, months: Iterable[tuple[int, int]]) -> tuple[dict, set[str]]:
    month_rows = {key: list(deps.fetch_month_rows(*key)) for key in sorted(set(months))}
    known = {sid for rows in month_rows.values() for row in rows if (sid := _sid(row))}
    return month_rows, known


# ── Modo 1: encabezados ───────────────────────────────────────────────────────

def extract_headers(days: list[date], deps: BackfillDeps, *, only_new: bool = False) -> Iterator[dict]:
    """Una línea por correo del rango y una final {"summary": {mes: {estado: n}}}."""
    days = sorted(days)
    _rows, known = _known_ids(deps, ((d.year, d.month) for d in days))
    summary: dict[str, dict[str, int]] = defaultdict(lambda: {"new": 0, "known": 0, "guard": 0, "unreadable": 0})
    for d in days:
        for mid in deps.list_message_ids(d):
            line = {"id": mid, "day": d.isoformat(), "from": "", "subject": ""}
            msg = None if mid in known else deps.fetch_message(mid)
            if mid in known:
                line["state"] = "known"
            elif not msg:
                line["state"] = "unreadable"
            else:
                line["from"] = sender_label(msg.get("from", ""))
                line["subject"] = _subject(msg.get("subject", ""))
                ignore, reason = _should_ignore_email(msg)
                line["state"] = "guard" if ignore else "new"
                if ignore:
                    line["reason"] = reason
            summary[month_key(d)][line["state"]] += 1
            if not only_new or line["state"] == "new":
                yield line
    yield {"summary": dict(sorted(summary.items()))}


# ── Modo 2: cuerpos, contexto del día y categorías ────────────────────────────

def extract_bodies(ids: list[str], deps: BackfillDeps, message_day: Callable[[str], date | None], *,
                   max_chars: int = DEFAULT_MAX_CHARS) -> Iterator[dict]:
    if len(ids) > MAX_BODY_IDS:
        raise ValueError(f"máximo {MAX_BODY_IDS} ids por llamada")
    for mid in ids:
        msg = deps.fetch_message(mid)
        if not msg:
            yield {"id": mid, "state": "unreadable"}
            continue
        day = message_day(mid)
        line = {
            "id": mid,
            "day": day.isoformat() if day else None,
            "from": sender_label(msg.get("from", "")),
            "subject": _subject(msg.get("subject", "")),
            # El mismo texto que build_initial_message le pasa al LLM (_fetch_message ya colapsa espacios).
            "text": re.sub(r"\s+", " ", msg.get("body", "")).strip()[:max_chars],
            "force_pending": _force_pending_email(msg),
        }
        ignore, reason = _should_ignore_email(msg)
        if ignore:
            line["guard"] = reason
        yield line


def day_context(account_id: int, day: date, deps: BackfillDeps) -> dict:
    """Lo que el LLM recibe como "TRANSACCIONES YA REGISTRADAS HOY" para ese día."""
    rows = list(deps.fetch_month_rows(day.year, day.month))
    sources = {str(r.get("id")): (r.get("attributes", r).get("source")) for r in rows}
    txns = _fetch_day_transactions(account_id, day, month_rows=rows)
    return {"day": day.isoformat(), "registered": [
        {"concept": t.get("concept"), "amount": t.get("amount"), "transaction_type": t.get("type"),
         "status": t.get("status"), "source": sources.get(str(t.get("id")))}
        for t in txns
    ]}


def categories(tool_map: dict) -> dict:
    return {"categories": tool_map["get_categories"]({})}


def hints(tool_map: dict, merchants: list[str]) -> Iterator[dict]:
    for merchant in merchants:
        yield {"merchant": merchant, "hints": tool_map["get_classification_hints"]({"merchant": merchant})}


# ── Modo 3: aplicar decisiones ────────────────────────────────────────────────

def _empty_apply_stats() -> dict:
    return {k: 0 for k in APPLY_KEYS}


def _valid_day(value) -> date | None:
    try:
        return date.fromisoformat(value) if isinstance(value, str) and len(value) == 10 else None
    except ValueError:
        return None


def validate_decision(item) -> str | None:
    """None si la decisión es válida; si no, un código corto (sin contenido del correo)."""
    if not isinstance(item, dict):
        return "not_object"
    if not isinstance(item.get("email_id"), str) or not item["email_id"].strip():
        return "email_id"
    if _valid_day(item.get("day")) is None:
        return "day"
    decision = item.get("decision")
    if decision == "skip":
        return None if item.get("reason") in SKIP_REASONS else "reason"
    if decision != "create":
        return "decision"
    txn = item.get("transaction")
    if not isinstance(txn, dict):
        return "transaction"
    if set(txn) - TRANSACTION_FIELDS:
        return "unknown_field"
    if "date" in txn and _valid_day(txn["date"]) is None:
        return "date"
    if not isinstance(txn.get("concept"), str) or not txn["concept"].strip():
        return "concept"
    amount = txn.get("amount")
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        return "amount"
    for key in ("transaction_type", "status", "payment_source"):
        enum = _CREATE_SCHEMA[key].get("enum")
        if key in txn and txn[key] not in enum:
            return key
    if "transaction_type" not in txn:
        return "transaction_type"
    if "metadata" in txn and not isinstance(txn["metadata"], dict):
        return "metadata"
    if "hints_for" in item and not isinstance(item["hints_for"], str):
        return "hints_for"
    return None


def apply_hints(inp: dict, merchant: str, tool_map: dict, stats: dict) -> None:
    """Reglas de SYSTEM_PROMPT ("Clasificación con memoria de patrones") aplicadas mecánicamente
    sobre la salida de get_classification_hints."""
    meta = inp.setdefault("metadata", {})
    res = tool_map["get_classification_hints"]({"merchant": merchant})
    if not res.get("ok"):
        stats["hints_error"] += 1
        meta.setdefault("classification_source", "agent")
        return
    dominant = res.get("dominant")
    candidates = [c for c in (res.get("candidates") or []) if c.get("subcategory_code")]
    if dominant and dominant.get("subcategory_code"):
        stats["hints_dominant"] += 1
        inp["subcategory_code"] = dominant["subcategory_code"]
        inp["status"] = "confirmed"
        meta["classification_source"] = "pattern"
    elif len(candidates) >= 2:
        stats["hints_ambiguous"] += 1
        inp["subcategory_code"] = candidates[0]["subcategory_code"]
        inp["status"] = "pending"
        meta["suggested_subcategories"] = [c["subcategory_code"] for c in candidates[:2]]
        meta["classification_source"] = "agent"
    else:  # samples=0 (o un solo candidato sin dominante): queda lo que decidió la oficina
        stats["hints_undetermined"] += 1
        meta["classification_source"] = "agent"


def _same_movement(inp: dict, day_txns: list[dict]) -> bool:
    """Deduplicación semántica de SYSTEM_PROMPT: mismo monto y misma naturaleza ya registrados ese día."""
    for t in day_txns:
        try:
            same_amount = float(t.get("amount")) == float(inp["amount"])
        except (TypeError, ValueError):
            continue
        if same_amount and t.get("type") == inp.get("transaction_type"):
            return True
    return False


def apply_decisions(account_id: int, decisions: list, deps: BackfillDeps, *, dry_run: bool = False,
                    created_ids: list[str] | None = None) -> dict:
    """Aplica las decisiones de la oficina. Devuelve solo conteos (nunca conceptos ni montos)."""
    stats = _empty_apply_stats()
    created_ids = created_ids if created_ids is not None else []
    valid: list[tuple[date, int, dict]] = []
    for i, item in enumerate(decisions):
        stats["decisions"] += 1
        if validate_decision(item):
            stats["invalid"] += 1
            continue
        valid.append((_valid_day(item["day"]), i, item))
    valid.sort(key=lambda x: (x[0], x[1]))  # día por día, en orden cronológico

    month_rows, known_ids = _known_ids(deps, ((d.year, d.month) for d, _i, _it in valid))
    base_tools = deps.build_tool_map()
    inner_create = _make_dry_run_recorder([0]) if dry_run else base_tools["create_transaction"]
    run_rows: dict[date, list[dict]] = defaultdict(list)

    for d, _i, item in valid:
        if item["decision"] == "skip":
            stats[f"skipped_{item['reason']}"] += 1
            continue
        email_id = item["email_id"].strip()
        if email_id in known_ids:  # mismo resultado que guarded_create, sin volver a leer Gmail
            stats["duplicate_id"] += 1
            continue
        msg = deps.fetch_message(email_id)
        if not msg:
            stats["unreadable"] += 1
            continue
        email, _reason = _prepare_email(msg)
        if email is None:
            stats["guard_email"] += 1
            continue

        inp = {k: v for k, v in item["transaction"].items()}
        inp["metadata"] = dict(inp.get("metadata") or {})
        inp.setdefault("date", d.isoformat())
        if not inp.get("subcategory_code") and item.get("hints_for", "").strip():
            apply_hints(inp, item["hints_for"].strip(), base_tools, stats)
        inp.setdefault("status", "pending")
        if email.get("force_pending"):  # "Force pending: sí" → SIEMPRE pending
            stats["force_pending"] += 1
            inp["status"] = "pending"
        meta = inp["metadata"]
        meta.setdefault("raw_text", email.get("body", "")[:DEFAULT_MAX_CHARS])
        meta.setdefault("subject", email.get("subject", ""))
        meta["source_event_id"] = email_id
        inp["source"] = "gmail"

        day_rows = month_rows.get((d.year, d.month), []) + run_rows[d]
        if not item.get("not_duplicate") and _same_movement(
                inp, _fetch_day_transactions(account_id, d, month_rows=day_rows)):
            stats["duplicate_day"] += 1
            continue
        guarded_create(inp, inner_create=inner_create, known_ids=known_ids, stats=stats, day=d,
                       day_rows=run_rows[d], created_ids=created_ids, dry_run=dry_run)
    return stats
