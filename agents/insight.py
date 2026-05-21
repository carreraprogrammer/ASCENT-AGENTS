"""
agents/insight.py — Daily insight generator with drift guard.

Flow:
  1. GET /api/v1/summary  — current financial state
  2. GET /api/v1/agent_insights/latest  — last persisted insight
  3. Drift check (Python, $0)
  4. If stable → skip
  5. If should_refresh AND last_insight exists → Haiku validity check (~$0.001)
  6. If still_valid → skip
  7. Sonnet structured generation (~$0.01-0.02)
  8. POST /api/v1/night_analyses  — stores NightAnalysis + AgentInsight atomically
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta

import httpx

from adapters.rails_http import BASE_URL, build_auth_headers
from services.llm_factory import build_llm_provider

logger = logging.getLogger(__name__)

COLOMBIA_TZ = timezone(timedelta(hours=-5))

BALANCE_DRIFT_THRESHOLD        = 1_000_000
COMMITMENT_GAP_DRIFT_THRESHOLD = 500_000


# ── Drift checker (Python mirror of InsightDriftChecker interactor) ───────────

def _should_refresh(current: dict, last_insight: dict | None, today: datetime) -> tuple[bool, str]:
    if last_insight is None:
        return True, "initial"

    # Refresh if insight is more than 20 hours old (new day effectively)
    generated_at = last_insight.get("generated_at", "")
    if generated_at:
        try:
            gen_dt = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
            age_hours = (datetime.now(gen_dt.tzinfo) - gen_dt).total_seconds() / 3600
            if age_hours > 20:
                return True, "age"
        except Exception:
            pass

    if abs(current.get("confirmed_balance", 0) - current.get("prev_confirmed_balance", 0)) > BALANCE_DRIFT_THRESHOLD:
        return True, "balance_drift"

    if abs(current.get("commitment_gap", 0) - current.get("prev_commitment_gap", 0)) > COMMITMENT_GAP_DRIFT_THRESHOLD:
        return True, "deploy_drift"

    # New milestone since last insight → always refresh
    last_generated_at = last_insight.get("generated_at", "")
    last_milestone_at = current.get("last_milestone_at", "")
    if last_milestone_at and last_milestone_at > last_generated_at:
        return True, "new_milestone"

    return False, "stable"


def _extract_current_state(summary: dict, milestones: list[dict]) -> dict:
    runway    = summary.get("cash_flow_runway") or {}
    balance   = summary.get("balance", {})
    burn_rate = summary.get("burn_rate") or {}
    categories = burn_rate.get("categories", [])
    now_col    = datetime.now(COLOMBIA_TZ)

    confirmed_balance = (
        balance.get("income_confirmed", 0) - balance.get("expense_confirmed", 0)
    )
    commitment_gap = runway.get("commitment_gap", 0) or 0
    categories_on_track = [
        c["category"] for c in categories if c.get("on_track")
    ]

    last_milestone = milestones[0] if milestones else None

    return {
        "confirmed_balance":   confirmed_balance,
        "commitment_gap":      commitment_gap,
        "categories_on_track": categories_on_track,
        "period_month":        now_col.month,
        "period_year":         now_col.year,
        "last_milestone_code": last_milestone["code"] if last_milestone else None,
        "last_milestone_at":   last_milestone["achieved_at"] if last_milestone else None,
    }


# ── API helpers ───────────────────────────────────────────────────────────────

def _get_summary(month: int, year: int) -> dict:
    r = httpx.get(
        f"{BASE_URL}/api/v1/summary",
        headers=build_auth_headers(),
        params={"month": month, "year": year},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()


def _get_milestones(limit: int = 5) -> list[dict]:
    try:
        r = httpx.get(
            f"{BASE_URL}/api/v1/milestones",
            headers=build_auth_headers(),
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        items = data if isinstance(data, list) else data.get("data", [])
        return items[:limit]
    except Exception as exc:
        logger.warning("[insight] milestones fetch failed: %s", exc)
        return []


def _get_latest_insight() -> dict | None:
    r = httpx.get(
        f"{BASE_URL}/api/v1/agent_insights/latest",
        headers=build_auth_headers(),
        timeout=15,
    )
    r.raise_for_status()
    return r.json().get("data")


def _post_night_analysis(payload: dict) -> dict:
    r = httpx.post(
        f"{BASE_URL}/api/v1/night_analyses",
        headers=build_auth_headers(),
        json=payload,
        timeout=20,
    )
    r.raise_for_status()
    return r.json()


# ── LLM calls ─────────────────────────────────────────────────────────────────

def _haiku_still_valid(last_insight: dict, summary: dict) -> bool:
    """Ask the fast model if the previous insight is still actionable given the current state."""
    prev_body      = last_insight.get("body", "")
    runway         = summary.get("cash_flow_runway") or {}
    health_status  = runway.get("health_status", "unknown")
    commitment_gap = runway.get("commitment_gap", 0) or 0
    days_to_next   = runway.get("days_to_next_income")

    prompt = f"""Previous insight body:
"{prev_body}"

Current state:
- health_status: {health_status}
- commitment_gap: {commitment_gap:,} COP  (positive = safe, negative = critical)
- days_to_next_income: {days_to_next}

Answer ONLY with a JSON object: {{"still_valid": true}} or {{"still_valid": false}}
The insight is NOT still valid if health_status changed or commitment_gap changed significantly (>500,000 COP)."""

    llm = build_llm_provider()
    raw = llm.simple_complete(
        [{"role": "user", "content": prompt}],
        max_tokens=64,
    )
    try:
        data = json.loads(raw)
        return bool(data.get("still_valid", False))
    except Exception:
        return False


def _sonnet_generate(
    summary: dict,
    last_insight: dict | None,
    trigger_reason: str,
    milestones: list[dict] | None = None,
) -> dict:
    """Ask Sonnet to generate a title+body coaching insight."""
    runway    = summary.get("cash_flow_runway") or {}
    ctx       = summary.get("financial_context") or {}
    burn_rate = summary.get("burn_rate") or {}
    debts     = summary.get("debts") or {}
    overflow  = summary.get("overflow_status") or {}

    prev_block = ""
    if last_insight:
        prev_block = f"\nPrevious insight (outdated — trigger: {trigger_reason}):\n\"{last_insight.get('body', '')}\"\n"

    milestones_block = ""
    if milestones:
        lines = []
        for m in milestones[:5]:
            date_str = (m.get("achieved_at") or "")[:10]
            meta = m.get("metadata") or {}
            meta_str = f" ({', '.join(f'{k}={v}' for k, v in meta.items())})" if meta else ""
            lines.append(f"- {m['code']} on {date_str}{meta_str}")
        milestones_block = "\nRECENT MILESTONES (most recent first):\n" + "\n".join(lines) + "\n"

    confirmed_balance = runway.get("confirmed_balance", 0) or 0
    commitment_gap    = runway.get("commitment_gap") or 0
    health_status     = runway.get("health_status", "unknown")
    daily_burn        = runway.get("daily_necessary_burn", 0) or 0
    days_to_income    = runway.get("days_to_next_income")
    next_income_day   = runway.get("next_income_day")
    buffer_days       = runway.get("buffer_days")
    committed_obls    = runway.get("committed_obligations", [])
    realized_overflow = overflow.get("realized_overflow", 0)
    overflow_status   = overflow.get("status", "waiting")

    system = """Eres el coach financiero personal de un usuario colombiano que cobra por quincenas.
Tu trabajo es generar una tarjeta de insight para su dashboard.

REGLAS DE COMUNICACIÓN — MUY IMPORTANTE:
- Escribí siempre en español, tuteo (vos/te).
- NUNCA repitas nombres de campos técnicos: nada de "commitment_gap", "buffer_days", "daily_burn", "colchón de días".
- NUNCA pongas números crudos como "368346". Formateá así: $368K, $1.2M, $45K.
- Si mencionás dinero, siempre con el símbolo y abreviado. Si mencionás días, decí "hasta el 20" o "hasta fin de quincena", no "22 días".
- El mensaje tiene que sonar como lo que le diría un amigo que sabe de finanzas, no como un reporte de sistema.
- 1-2 oraciones máximo. Directo, sin relleno motivacional.

SELECCIÓN DE KIND:
- "congratulation" — estado cómodo con buen comportamiento o hito positivo
- "alert" — margen ajustado o crítico
- "achievement" — logro reciente en la lista de hitos
- "proposal" — recomendación accionable concreta cuando hay excedente
- "tip" — observación de coaching general (por defecto)

GUARDARRAÍLES POR ESTADO:
- comfortable: podés sugerir mover plata si hay excedente real
- warning: mencioná el margen ajustado primero, sin sugerir deploys
- critical: NO recomiendes mover ninguna plata

Respondé SOLO con un JSON válido, sin prose ni markdown."""

    # Formatear datos financieros en lenguaje legible antes de pasarlos al modelo
    def _fmt(n: int | float) -> str:
        n = int(n)
        if abs(n) >= 1_000_000:
            return f"${n/1_000_000:.1f}M"
        if abs(n) >= 1_000:
            return f"${n//1_000}K"
        return f"${n}"

    burn_summary = []
    for cat in burn_rate.get("categories", []):
        burn_summary.append({
            "categoria": cat.get("category", cat.get("category_type", "")),
            "presupuesto": _fmt(cat.get("budget", 0)),
            "gastado": _fmt(cat.get("spent", 0)),
            "porcentaje_usado": f"{cat.get('pct', 0):.0f}%",
            "alerta": cat.get("alert") or ("sobre presupuesto" if cat.get("pct", 0) > 100 else "ok"),
        })

    obligations_summary = [
        {"nombre": o.get("name", ""), "monto": _fmt(o.get("remaining", o.get("expected", 0)))}
        for o in committed_obls
    ]

    user = f"""Estado financiero — {datetime.now(COLOMBIA_TZ).strftime('%B %Y')}:

SALUD DEL FLUJO:
- estado: {health_status} (comfortable=tranquilo, warning=justo, critical=en rojo)
- margen hasta próximo ingreso: {_fmt(commitment_gap)}
- gasto diario necesario: {_fmt(daily_burn)}/día
- días hasta próximo ingreso: {days_to_income} (día {next_income_day} del mes)
- días de colchón real: {buffer_days}
- obligaciones pendientes antes del próximo ingreso:
{json.dumps(obligations_summary, ensure_ascii=False, indent=2)}

EXCEDENTE: estado={overflow_status}, monto real={_fmt(realized_overflow)}

CONTEXTO FINANCIERO:
- fase: {ctx.get('phase', 'desconocida')}
- estrategia: {ctx.get('strategy', 'desconocida')}
- notas/objetivos: {ctx.get('notes', 'ninguno')}

GASTO POR CATEGORÍA:
{json.dumps(burn_summary, ensure_ascii=False, indent=2)}

DEUDAS: saldo total={_fmt(debts.get('total_balance', 0))}, pago mensual={_fmt(debts.get('monthly_payments', 0))}
{milestones_block}{prev_block}
Generá la tarjeta de coaching:
{{
  "kind": "tip|congratulation|alert|proposal|achievement",
  "title": "Título corto (máx 50 chars, español, concreto)",
  "body": "1-2 oraciones de coaching en español. Formateá montos como $XXK o $X.XM. Soná como un amigo que sabe de finanzas, no como un sistema.",
  "reasoning": "Razonamiento interno — honesto y específico. Siempre en español."
}}"""

    llm = build_llm_provider()
    raw = llm.simple_complete(
        [{"role": "user", "content": user}],
        system=system,
        max_tokens=512,
    ).strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw)


# ── Main entry point ──────────────────────────────────────────────────────────

def run_insight_refresh(*, trigger: str = "scheduled") -> None:
    now_col = datetime.now(COLOMBIA_TZ)
    month, year = now_col.month, now_col.year

    logger.info("[insight] starting — %s/%s trigger=%s", month, year, trigger)

    summary      = _get_summary(month, year)
    last_insight = _get_latest_insight()
    milestones   = _get_milestones()
    current      = _extract_current_state(summary, milestones)

    should, reason = _should_refresh(current, last_insight, now_col)

    if not should and trigger == "scheduled":
        logger.info("[insight] stable — skipping generation.")
        return

    if trigger != "scheduled":
        reason = trigger  # manual / on_demand

    logger.info("[insight] refresh needed — reason=%s", reason)

    # Fast validity gate (only if previous insight exists)
    if last_insight and trigger == "scheduled":
        still_valid = _haiku_still_valid(last_insight, summary)
        if still_valid:
            logger.info("[insight] fast model confirmed previous insight still valid — skipping.")
            return

    result = _sonnet_generate(summary, last_insight, reason, milestones)

    runway         = summary.get("cash_flow_runway") or {}
    commitment_gap = runway.get("commitment_gap", 0) or 0
    health_status  = runway.get("health_status", "unknown")

    payload = {
        "date":            now_col.date().isoformat(),
        "metrics": {
            "health_status":      health_status,
            "commitment_gap":     int(commitment_gap),
            "daily_burn":         int(runway.get("daily_necessary_burn", 0) or 0),
            "days_to_next_income": runway.get("days_to_next_income"),
            "category_alerts":    [],
            "transactions_context": {"matched": [], "unmatched": []},
        },
        "agent_reasoning": result.get("reasoning", ""),
        "insight": {
            "kind":  result.get("kind", "tip"),
            "title": result.get("title", ""),
            "body":  result.get("body", ""),
        },
    }

    _post_night_analysis(payload)
    logger.info("[insight] analysis persisted — trigger=%s commitment_gap=%s", reason, commitment_gap)
