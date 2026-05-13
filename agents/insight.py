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

    system = """You are a responsible personal finance coach for a Colombian user paid in two quincenas per month.
Generate a short coaching insight card for their dashboard.

HEALTH STATUS GUARDRAILS:
- comfortable: safe to suggest moving money (commitment_gap >= 0, buffer_days >= 2)
- warning: mention thin margin first — no deployment suggestions
- critical: do NOT recommend moving any money

KIND SELECTION:
- "congratulation" — positive milestone or comfortable state with good behavior
- "alert" — health_status is warning or critical
- "achievement" — recent milestone in the milestones list
- "proposal" — specific actionable recommendation when comfortable
- "tip" — general coaching observation (default)

Respond ONLY with a valid JSON object — no prose, no markdown."""

    user = f"""Financial state for {datetime.now(COLOMBIA_TZ).strftime('%B %Y')}:

CASH FLOW RUNWAY:
- confirmed_balance: {confirmed_balance:,} COP
- health_status: {health_status}
- commitment_gap: {commitment_gap:,} COP
- daily_necessary_burn: {daily_burn:,} COP/day
- days_to_next_income: {days_to_income} (next income day {next_income_day})
- buffer_days: {buffer_days}
- committed_obligations:
{json.dumps(committed_obls, ensure_ascii=False, indent=2)}

OVERFLOW: status={overflow_status}, realized={realized_overflow:,} COP

FINANCIAL CONTEXT:
- phase: {ctx.get('phase', 'unknown')}
- strategy: {ctx.get('strategy', 'unknown')}
- goals: {ctx.get('notes', 'none')}

BURN RATE:
{json.dumps(burn_rate.get('categories', []), ensure_ascii=False, indent=2)}

DEBTS: balance={debts.get('total_balance', 0):,} COP, monthly={debts.get('monthly_payments', 0):,} COP
{milestones_block}{prev_block}
Generate a JSON coaching card:
{{
  "kind": "tip|congratulation|alert|proposal|achievement",
  "title": "Short title (max 50 chars, Spanish)",
  "body": "1-2 concrete coaching sentences grounded in the numbers (Spanish). Direct, no fluff.",
  "reasoning": "Internal reasoning — honest, specific."
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
