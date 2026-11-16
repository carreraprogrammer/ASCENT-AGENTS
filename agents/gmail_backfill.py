"""
agents/gmail_backfill.py — ASC-3: recupera movimientos de correos de días pasados.

Se comporta como agents/gmail_push.py (mismo SYSTEM_PROMPT, TOOLS, TRANSACTION_CREATION_RULES,
guardas, force pending, _build_tool_map, _get_gmail_token y camino de LLM) con dos diferencias:
  1. los correos salen de Gmail messages.list con una consulta por rango de fechas (paginada),
     no de la History API;
  2. el contexto anti-duplicados es "transacciones ya registradas ESE día", no "hoy".

Además, propio del backfill:
  - día por día en orden cronológico, en lotes pequeños;
  - idempotencia: un correo cuyo id (metadata.source_event_id) ya está registrado no va al LLM;
  - sin eventos de UI por movimiento (notify=False) y un único resumen al final, solo conteos;
  - --dry-run reemplaza create_transaction por un registrador que aplica las mismas guardas;
  - nunca liga un movimiento a una meta de ahorro ni a un fondo (se quitan savings_goal_id y
    sinking_fund_id del payload);
  - Gmail solo se lee (messages.list y messages.get).

Nunca registra en logs ni imprime contenido de correos, conceptos, montos individuales ni tokens:
el resultado son conteos agregados por mes.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Callable, Iterable

import httpx

from adapters.rails_http import BASE_URL as API_BASE_URL, build_auth_headers
from agents.gmail_push import (
    COLOMBIA_TZ,
    SYSTEM_PROMPT,
    TOOLS,
    _build_tool_map,
    _fetch_day_transactions,
    _fetch_message,
    _prepare_email,
    _prepare_transaction,
    build_initial_message,
    process_emails,
)

logger = logging.getLogger(__name__)

GMAIL_MESSAGES_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages"

# Lotes pequeños, como el modo en tiempo real (un push suele traer 1 a 3 correos).
BATCH_SIZE = 5
MAX_DAYS = 120
# Fuera del modo en tiempo real quedan los correos que Daniel envió o redactó.
GMAIL_BASE_QUERY = "-in:sent -in:drafts -in:chats"

# Precio de DeepSeek para deepseek-chat (el modelo de producción: services/llm_factory.DEFAULT_MODELS
# ["deepseek"], proveedor "deepseek"), en USD por millón de tokens. Fuente: tabla de precios de
# api-docs.deepseek.com (deepseek-chat, vigente desde sep-2025). Si cambia, actualizar aquí.
DEEPSEEK_INPUT_USD_PER_M = 0.28            # entrada, cache miss
DEEPSEEK_INPUT_CACHE_HIT_USD_PER_M = 0.028  # entrada, cache hit
DEEPSEEK_OUTPUT_USD_PER_M = 0.42           # salida

# Heurística para estimar el costo ANTES de llamar al LLM (para --max-cost-usd y --no-llm).
# Cada iteración del agente reenvía el system prompt, las herramientas y la conversación;
# un lote de n correos suele tomar ~2 iteraciones por correo financiero más una de cierre,
# y cada resultado de herramienta (categorías, hints) agrega contexto.
CHARS_PER_TOKEN = 3.5
EST_ITERATIONS_PER_EMAIL = 2
EST_TOOL_CONTEXT_TOKENS_PER_ITERATION = 1500
EST_OUTPUT_TOKENS_PER_ITERATION = 150
MAX_AGENT_ITERATIONS = 10  # igual que process_emails


def token_cost_usd(prompt_tokens: int, completion_tokens: int, cache_hit_tokens: int = 0) -> float:
    miss = max(prompt_tokens - cache_hit_tokens, 0)
    return (
        miss * DEEPSEEK_INPUT_USD_PER_M
        + cache_hit_tokens * DEEPSEEK_INPUT_CACHE_HIT_USD_PER_M
        + completion_tokens * DEEPSEEK_OUTPUT_USD_PER_M
    ) / 1_000_000


def estimate_batch_tokens(emails: list[dict]) -> tuple[int, int]:
    """(tokens de entrada, tokens de salida) estimados para un lote, sin llamar al LLM."""
    if not emails:
        return 0, 0
    base = len(SYSTEM_PROMPT) + len(json.dumps(TOOLS, ensure_ascii=False))
    message = len(build_initial_message(emails, []))
    iterations = min(1 + EST_ITERATIONS_PER_EMAIL * len(emails), MAX_AGENT_ITERATIONS)
    per_iteration = (base + message) / CHARS_PER_TOKEN
    prompt = int(iterations * per_iteration + EST_TOOL_CONTEXT_TOKENS_PER_ITERATION * iterations * (iterations - 1) / 2)
    return prompt, iterations * EST_OUTPUT_TOKENS_PER_ITERATION


def estimate_day_cost_usd(emails: list[dict]) -> float:
    total = 0.0
    for batch in _batches(emails, BATCH_SIZE):
        prompt, completion = estimate_batch_tokens(batch)
        total += token_cost_usd(prompt, completion)
    return total


def _batches(items: list, size: int) -> Iterable[list]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def daterange(since: date, until: date) -> list[date]:
    return [since + timedelta(days=i) for i in range((until - since).days + 1)]


def month_key(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def _sid(row: dict) -> str | None:
    attrs = row.get("attributes", row)
    return attrs.get("source_event_id") or (attrs.get("metadata") or {}).get("source_event_id")


# ── Gmail (solo lectura) ──────────────────────────────────────────────────────

def day_query(day: date) -> str:
    """Consulta de messages.list para un día de Colombia (UTC-5), con límites en epoch."""
    start = datetime.combine(day, time(0), COLOMBIA_TZ)
    end = start + timedelta(days=1)
    return f"after:{int(start.timestamp())} before:{int(end.timestamp())} {GMAIL_BASE_QUERY}"


def list_message_ids_for_day(access_token: str, day: date, extra_query: str = "") -> list[str]:
    """Ids de los correos del día, del más antiguo al más reciente. GET messages.list paginado.
    Lanza httpx.HTTPStatusError si Gmail rechaza el token (401/403)."""
    headers = {"Authorization": f"Bearer {access_token}"}
    query = f"{day_query(day)} {extra_query}".strip()
    ids: list[str] = []
    page_token = None
    while True:
        params = {"q": query, "maxResults": 100}
        if page_token:
            params["pageToken"] = page_token
        resp = httpx.get(GMAIL_MESSAGES_URL, headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        body = resp.json()
        ids.extend(m["id"] for m in body.get("messages", []) if m.get("id"))
        page_token = body.get("nextPageToken")
        if not page_token:
            break
    seen: set[str] = set()
    unique = [i for i in ids if not (i in seen or seen.add(i))]
    return list(reversed(unique))  # Gmail lista primero el más reciente


# ── API de Rails (lecturas) ───────────────────────────────────────────────────

def fetch_month_rows(account_id: int, year: int, month: int, source: str | None = None) -> list[dict]:
    """Todas las transacciones del mes (paginadas de 100 en 100). Solo lectura."""
    rows: list[dict] = []
    page = 1
    while True:
        params = {"month": month, "year": year, "page": page, "per_page": 100,
                  "sort_by": "date", "sort_dir": "desc"}
        if source:
            params["source"] = source
        resp = httpx.get(f"{API_BASE_URL}/api/v1/transactions", headers=build_auth_headers(str(account_id)),
                         params=params, timeout=15)
        resp.raise_for_status()
        body = resp.json()
        chunk = body.get("data", [])
        rows.extend(chunk)
        if not (body.get("meta") or {}).get("has_next_page") or not chunk or page >= 50:
            break
        page += 1
    return rows


def last_gmail_transaction_date(account_id: int, today: date, months_back: int = 13,
                                fetch_rows: Callable[..., list[dict]] | None = None) -> date | None:
    """Fecha del último movimiento con source=gmail ANTERIOR a hoy (los de hoy pueden venir del push
    recién reconectado y no marcan el fin del hueco). None si no hay en los últimos meses."""
    fetch_rows = fetch_rows or (lambda y, m: fetch_month_rows(account_id, y, m, source="gmail"))
    y, m = today.year, today.month
    for _ in range(months_back):
        dates = []
        for row in fetch_rows(y, m):
            raw = (row.get("attributes", row).get("date") or "")[:10]
            try:
                d = date.fromisoformat(raw)
            except ValueError:
                continue
            if d < today:
                dates.append(d)
        if dates:
            return max(dates)
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return None


def default_period(last_gmail: date | None, today: date) -> tuple[date, date, bool]:
    """(since, until, recortado). Desde el día siguiente al último movimiento gmail hasta hoy,
    máximo MAX_DAYS días (incluyendo hoy)."""
    floor = today - timedelta(days=MAX_DAYS - 1)
    since = last_gmail + timedelta(days=1) if last_gmail else floor
    capped = since < floor
    return max(since, floor), today, capped


# ── Corrida ───────────────────────────────────────────────────────────────────

STAT_KEYS = (
    "emails_reviewed",     # correos listados por Gmail en el periodo (sin excluidos)
    "excluded",            # dejados fuera por --exclude-dates / --exclude-ids-file
    "already_registered",  # su id ya era source_event_id de un movimiento: no van al LLM
    "unreadable",          # no se pudo leer o sin texto
    "guard_email",         # bloqueados por guarda antes del LLM (pago de TC, transferencia propia)
    "force_pending",       # marcados "Force pending: sí"
    "sent_to_llm",
    "financial",           # correos con al menos un create_transaction
    "created",             # creadas (o que crearía, en --dry-run)
    "pending",             # de las creadas, cuántas quedan pending
    "guard_tool",          # create_transaction bloqueado por guarda
    "duplicate_id",        # create_transaction de un id ya registrado (409 o visto en la corrida)
    "no_movement",         # enviados al LLM sin crear nada: no financieros o duplicado semántico
    "goal_link_stripped",  # payloads a los que se les quitó savings_goal_id / sinking_fund_id
    "errors",              # errores de herramienta o del LLM (por correo)
    "skipped_by_cost",     # correos de días que no entraron por --max-cost-usd
)


def _empty_stats() -> dict:
    return {k: 0 for k in STAT_KEYS}


@dataclass
class BackfillDeps:
    """Lo que la corrida necesita del mundo. Los tests pasan dobles."""
    list_message_ids: Callable[[date], list[str]]
    fetch_message: Callable[[str], dict | None]
    fetch_month_rows: Callable[[int, int], list[dict]]
    build_tool_map: Callable[[], dict]
    provider: object
    model: str | None = None
    send_summary: Callable[[dict], None] | None = None


@dataclass
class BackfillResult:
    since: date
    until: date
    dry_run: bool
    no_llm: bool
    months: dict = field(default_factory=lambda: defaultdict(_empty_stats))
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_hit_tokens: int = 0
    tokens_are_estimate: bool = False
    estimated_cost_usd: float = 0.0       # estimación previa (heurística) de lo seleccionado
    cost_reached_date: date | None = None  # día más antiguo procesado cuando aplica --max-cost-usd
    stopped_by_cost_at: date | None = None
    created_ids: list[str] = field(default_factory=list)
    goal_linked_by_api: int | None = None  # creadas que la API ligó a una meta por su cuenta

    @property
    def cost_usd(self) -> float:
        return token_cost_usd(self.prompt_tokens, self.completion_tokens, self.cache_hit_tokens)

    def totals(self) -> dict:
        total = _empty_stats()
        for stats in self.months.values():
            for k, v in stats.items():
                total[k] += v
        return total


def _usage_snapshot(provider) -> tuple[int, int, int] | None:
    usage = getattr(provider, "usage_totals", None)
    if not isinstance(usage, dict):
        return None
    return (int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0)),
            int(usage.get("prompt_cache_hit_tokens", 0)))


def _make_dry_run_recorder(counter: list[int]) -> Callable[[dict], dict]:
    """create_transaction de --dry-run: mismas guardas y force pending, ninguna escritura."""

    def create_transaction(inp: dict) -> dict:
        payload, _sid_, guard_reason = _prepare_transaction(inp)
        if guard_reason:
            return {"ok": True, "created": False, "ignored": True, "reason": guard_reason}
        counter[0] += 1
        return {"ok": True, "created": True, "id": f"dry-run-{counter[0]}",
                "concept": payload.get("concept"), "amount": payload.get("amount"),
                "status": payload.get("status"), "subcategory_code": payload.get("subcategory_code"),
                "payment_source": payload.get("payment_source")}

    return create_transaction


def guarded_create(inp: dict, *, inner_create: Callable[[dict], dict], known_ids: set[str], stats: dict,
                   day: date, day_rows: list[dict], created_ids: list[str], dry_run: bool) -> dict:
    """Envoltura de create_transaction común al backfill con LLM y al de la oficina:
    quita savings_goal_id / sinking_fund_id, salta ids ya registrados (known_ids), delega en
    inner_create (la herramienta real de _build_tool_map o el registrador de --dry-run, que aplican
    las guardas y el force pending de _prepare_transaction) y cuenta el resultado en stats.
    Lo creado se añade a day_rows (contexto anti-duplicados del día) y a known_ids."""
    inp = dict(inp)
    for key in ("savings_goal_id", "sinking_fund_id"):
        if inp.pop(key, None) is not None:
            stats["goal_link_stripped"] += 1
    sid = (inp.get("metadata") or {}).get("source_event_id")
    if sid and sid in known_ids:
        stats["duplicate_id"] += 1
        return {"ok": True, "created": False, "already_existed": True,
                "detail": "source_event_id ya registrado"}
    res = inner_create(inp)
    if res.get("created"):
        stats["created"] += 1
        if res.get("status") == "pending":
            stats["pending"] += 1
        if sid:
            known_ids.add(sid)
        if not dry_run and res.get("id"):
            created_ids.append(str(res["id"]))
        day_rows.append({"id": res.get("id"), "attributes": {
            "date": (inp.get("date") or day.isoformat())[:10], "concept": res.get("concept"),
            "amount": res.get("amount"), "transaction_type": inp.get("transaction_type"),
            "status": res.get("status"), "source": "gmail"}})
    elif res.get("ignored"):
        stats["guard_tool"] += 1
    elif res.get("already_existed"):
        stats["duplicate_id"] += 1
        if sid:
            known_ids.add(sid)
    elif not res.get("ok"):
        stats["errors"] += 1
    return res


def run_backfill(
    account_id: int,
    days: list[date],
    deps: BackfillDeps,
    *,
    dry_run: bool = False,
    no_llm: bool = False,
    excluded_ids: set[str] | None = None,
    excluded_dates: set[date] | None = None,
    max_cost_usd: float | None = None,
) -> BackfillResult:
    days = sorted(days)
    excluded_ids = excluded_ids or set()
    excluded_dates = excluded_dates or set()
    result = BackfillResult(since=days[0], until=days[-1], dry_run=dry_run, no_llm=no_llm)

    # 1. Movimientos ya registrados en los meses del periodo: contexto por día e ids conocidos.
    month_rows: dict[tuple[int, int], list[dict]] = {}
    for d in days:
        key = (d.year, d.month)
        if key not in month_rows:
            month_rows[key] = list(deps.fetch_month_rows(d.year, d.month))
    known_ids = {sid for rows in month_rows.values() for row in rows if (sid := _sid(row))}

    # 2. Gmail (lectura) y filtros previos al LLM, día por día.
    pending_by_day: dict[date, list[dict]] = {}
    for d in days:
        stats = result.months[month_key(d)]
        ids = deps.list_message_ids(d)
        if d in excluded_dates:
            stats["excluded"] += len(ids)
            continue
        queue: list[dict] = []
        for mid in ids:
            if mid in excluded_ids:
                stats["excluded"] += 1
                continue
            stats["emails_reviewed"] += 1
            if mid in known_ids:
                stats["already_registered"] += 1
                continue
            msg = deps.fetch_message(mid)
            if not msg:
                stats["unreadable"] += 1
                continue
            email, _reason = _prepare_email(msg)
            if email is None:
                stats["guard_email"] += 1
                continue
            if email.get("force_pending"):
                stats["force_pending"] += 1
            email["day"] = d.isoformat()
            queue.append(email)
        if queue:
            pending_by_day[d] = queue

    # 3. Tope de costo: del día más reciente hacia atrás hasta llenar el tope (estimación previa).
    selected = sorted(pending_by_day)
    estimates = {d: estimate_day_cost_usd(pending_by_day[d]) for d in selected}
    if max_cost_usd is not None:
        chosen: list[date] = []
        acc = 0.0
        for d in reversed(selected):
            if acc + estimates[d] > max_cost_usd:
                break
            acc += estimates[d]
            chosen.append(d)
        for d in selected:
            if d not in chosen:
                result.months[month_key(d)]["skipped_by_cost"] += len(pending_by_day[d])
        selected = sorted(chosen)
        result.cost_reached_date = selected[0] if selected else None
    result.estimated_cost_usd = sum(estimates[d] for d in selected)

    if no_llm:
        for d in selected:
            for batch in _batches(pending_by_day[d], BATCH_SIZE):
                p, c = estimate_batch_tokens(batch)
                result.prompt_tokens += p
                result.completion_tokens += c
        result.tokens_are_estimate = True
        return result

    # 4. Agente, día por día en orden cronológico, en lotes pequeños.
    provider = deps.provider
    base_usage = _usage_snapshot(provider)
    if base_usage is None:
        result.tokens_are_estimate = True
    dry_counter = [0]
    base_tools = deps.build_tool_map()
    inner_create = _make_dry_run_recorder(dry_counter) if dry_run else base_tools["create_transaction"]
    run_rows: dict[date, list[dict]] = defaultdict(list)  # creadas en esta corrida, por día

    stopped = False
    for d in selected:
        stats = result.months[month_key(d)]
        for batch in _batches(pending_by_day[d], BATCH_SIZE):
            if stopped or (max_cost_usd is not None and result.cost_usd >= max_cost_usd):
                # Tope duro con el costo real: lo que falta no se procesa.
                if not stopped:
                    result.stopped_by_cost_at = d
                    stopped = True
                stats["skipped_by_cost"] += len(batch)
                continue
            batch_ids = {e["id"] for e in batch}
            touched: set[str] = set()

            def create_transaction(inp: dict, _d=d, _stats=stats, _batch_ids=batch_ids, _touched=touched) -> dict:
                sid = (inp.get("metadata") or {}).get("source_event_id")
                if sid in _batch_ids:
                    _touched.add(sid)
                return guarded_create(inp, inner_create=inner_create, known_ids=known_ids, stats=_stats,
                                      day=_d, day_rows=run_rows[_d], created_ids=result.created_ids,
                                      dry_run=dry_run)

            tool_map = {**base_tools, "create_transaction": create_transaction}
            day_rows = month_rows.get((d.year, d.month), []) + run_rows[d]
            day_txns = _fetch_day_transactions(account_id, d, month_rows=day_rows)
            stats["sent_to_llm"] += len(batch)
            if base_usage is None:
                p, c = estimate_batch_tokens(batch)
                result.prompt_tokens += p
                result.completion_tokens += c
            try:
                process_emails(account_id, batch, day_txns, notify=False, tool_map=tool_map,
                               provider=provider, model=deps.model)
                failed = False
            except Exception as exc:  # el LLM o la red fallaron para este lote
                logger.error("[GmailBackfill] lote fallido: %s", type(exc).__name__)
                failed = True
            stats["financial"] += len(touched & batch_ids)
            untouched = len(batch_ids - touched)
            stats["errors" if failed else "no_movement"] += untouched
            if base_usage is not None:
                now = _usage_snapshot(provider)
                result.prompt_tokens = now[0] - base_usage[0]
                result.completion_tokens = now[1] - base_usage[1]
                result.cache_hit_tokens = now[2] - base_usage[2]

    if not dry_run and deps.send_summary:
        deps.send_summary(summary_payload(result))
    return result


def summary_payload(result: BackfillResult) -> dict:
    """Único aviso al final del backfill: solo conteos, nunca montos ni conceptos."""
    t = result.totals()
    body = (f"Recuperé {t['created']} movimientos de tus correos entre {result.since.isoformat()} "
            f"y {result.until.isoformat()}.")
    if t["pending"]:
        body += f" {t['pending']} quedaron pendientes para que los revises."
    return {"title": "Movimientos recuperados desde Gmail", "body": body, "tone": "success"}


def default_send_summary(account_id: int) -> Callable[[dict], None]:
    def send(payload: dict) -> None:
        try:
            httpx.post(f"{API_BASE_URL}/api/v1/agent_events", headers=build_auth_headers(str(account_id)),
                       json={"event_type": "show_card", "payload": payload}, timeout=15).raise_for_status()
        except Exception as exc:
            logger.error("[GmailBackfill] resumen no enviado: %s", type(exc).__name__)
    return send


def count_goal_linked(account_id: int, created_ids: list[str], months: Iterable[tuple[int, int]],
                      fetch_rows: Callable[[int, int], list[dict]] | None = None) -> int:
    """Cuántas de las creadas quedaron con savings_goal_id (la API puede derivarlo de una obligación
    o de un match estructural). Solo para reportarlo; no se edita nada."""
    fetch_rows = fetch_rows or (lambda y, m: fetch_month_rows(account_id, y, m))
    wanted = set(created_ids)
    linked = 0
    for y, m in months:
        for row in fetch_rows(y, m):
            if str(row.get("id")) in wanted and row.get("attributes", row).get("savings_goal_id"):
                linked += 1
    return linked


def default_deps(account_id: int, access_token: str, *, extra_query: str = "", llm: bool = True) -> BackfillDeps:
    """llm=False (modos de la oficina): no construye el proveedor de LLM."""
    from services.llm_factory import build_llm_provider, resolve_llm_model

    return BackfillDeps(
        list_message_ids=lambda d: list_message_ids_for_day(access_token, d, extra_query),
        fetch_message=lambda mid: _fetch_message(access_token, mid),
        fetch_month_rows=lambda y, m: fetch_month_rows(account_id, y, m),
        build_tool_map=lambda: _build_tool_map(account_id, notify=False),
        provider=build_llm_provider() if llm else None,
        model=resolve_llm_model() if llm else None,
        send_summary=default_send_summary(account_id),
    )
