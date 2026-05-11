"""
agents/insight.py — Daily insight generator with drift guard.

Flow:
  1. GET /api/v1/summary  — current financial state
  2. GET /api/v1/agent_insights/current  — last persisted insight
  3. Drift check (Python, $0)
  4. If stable → skip
  5. If should_refresh AND last_insight exists → Haiku validity check (~$0.001)
  6. If still_valid → skip
  7. Sonnet structured generation (~$0.01-0.02)
  8. POST /api/v1/agent_insights
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone, timedelta

import anthropic
import httpx

from adapters.rails_http import BASE_URL, build_auth_headers

logger = logging.getLogger(__name__)

COLOMBIA_TZ = timezone(timedelta(hours=-5))

HAIKU_MODEL  = "claude-haiku-4-5-20251001"
SONNET_MODEL = "claude-sonnet-4-6"

BALANCE_DRIFT_THRESHOLD = 1_000_000
DEPLOY_DRIFT_THRESHOLD  =   500_000


# ── Drift checker (Python mirror of InsightDriftChecker interactor) ───────────

def _should_refresh(current: dict, last_insight: dict | None, today: datetime) -> tuple[bool, str]:
    if last_insight is None:
        return True, "initial"

    snap = last_insight.get("key_metrics_snapshot", {})

    if snap.get("period_month") != today.month or snap.get("period_year") != today.year:
        return True, "month_change"

    if abs(current.get("confirmed_balance", 0) - snap.get("confirmed_balance", 0)) > BALANCE_DRIFT_THRESHOLD:
        return True, "balance_drift"

    prev_on_track = sorted(snap.get("categories_on_track", []))
    curr_on_track = sorted(current.get("categories_on_track", []))
    if prev_on_track != curr_on_track:
        return True, "track_change"

    if abs(current.get("deployable_overflow", 0) - snap.get("deployable_overflow", 0)) > DEPLOY_DRIFT_THRESHOLD:
        return True, "deploy_drift"

    # New milestone since last insight → always refresh
    last_generated_at = last_insight.get("generated_at", "")
    last_milestone_at = current.get("last_milestone_at", "")
    if last_milestone_at and last_milestone_at > last_generated_at:
        return True, "new_milestone"

    return False, "stable"


def _extract_current_state(summary: dict, milestones: list[dict]) -> dict:
    liquidity  = summary.get("liquidity") or {}
    balance    = summary.get("balance", {})
    burn_rate  = summary.get("burn_rate") or {}
    categories = burn_rate.get("categories", [])
    now_col    = datetime.now(COLOMBIA_TZ)

    confirmed_balance = (
        balance.get("income_confirmed", 0) - balance.get("expense_confirmed", 0)
    )
    deployable_overflow = (summary.get("overflow_status") or {}).get("deployable_overflow", 0)
    categories_on_track = [
        c["category"] for c in categories if c.get("on_track")
    ]

    last_milestone = milestones[0] if milestones else None

    return {
        "confirmed_balance":    confirmed_balance,
        "deployable_overflow":  deployable_overflow,
        "categories_on_track":  categories_on_track,
        "period_month":         now_col.month,
        "period_year":          now_col.year,
        "last_milestone_code":  last_milestone["code"] if last_milestone else None,
        "last_milestone_at":    last_milestone["achieved_at"] if last_milestone else None,
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


def _get_current_insight(month: int, year: int) -> dict | None:
    r = httpx.get(
        f"{BASE_URL}/api/v1/agent_insights/current",
        headers=build_auth_headers(),
        params={"month": month, "year": year},
        timeout=15,
    )
    r.raise_for_status()
    return r.json().get("data")


def _post_insight(payload: dict) -> dict:
    r = httpx.post(
        f"{BASE_URL}/api/v1/agent_insights",
        headers=build_auth_headers(),
        json=payload,
        timeout=20,
    )
    r.raise_for_status()
    return r.json()


# ── LLM calls ─────────────────────────────────────────────────────────────────

def _build_client() -> anthropic.Anthropic:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise ValueError("ANTHROPIC_API_KEY not set — cannot generate insights.")
    return anthropic.Anthropic(api_key=key)


def _haiku_still_valid(client: anthropic.Anthropic, last_insight: dict, summary: dict) -> bool:
    """Ask Haiku if the previous insight is still actionable given the current state."""
    prev_recs        = json.dumps(last_insight.get("recommendations", {}), ensure_ascii=False)
    deployable_now   = (summary.get("overflow_status") or {}).get("deployable_overflow", 0)
    overflow_status  = (summary.get("overflow_status") or {}).get("status", "waiting")
    ctx              = summary.get("financial_context") or {}

    prompt = f"""Previous insight recommendations:
{prev_recs}

Current state:
- overflow_status: {overflow_status}
- deployable_overflow: {deployable_now:,} COP
- recommended_action: {ctx.get('recommended_action', 'none')}
- buffer_status: {(summary.get('liquidity') or {}).get('buffer_status', 'unknown')}

Answer ONLY with a JSON object: {{"still_valid": true}} or {{"still_valid": false}}
The insight is NOT still valid if deployable_overflow changed significantly or the
recommended action contradicts current liquidity."""

    resp = client.messages.create(
        model=HAIKU_MODEL,
        max_tokens=64,
        messages=[{"role": "user", "content": prompt}],
    )
    try:
        data = json.loads(resp.content[0].text)
        return bool(data.get("still_valid", False))
    except Exception:
        return False


def _sonnet_generate(
    client: anthropic.Anthropic,
    summary: dict,
    last_insight: dict | None,
    trigger_reason: str,
    milestones: list[dict] | None = None,
) -> dict:
    """Ask Sonnet to generate a structured financial insight."""
    liquidity  = summary.get("liquidity") or {}
    ctx        = summary.get("financial_context") or {}
    burn_rate  = summary.get("burn_rate") or {}
    debts      = summary.get("debts") or {}

    prev_block = ""
    if last_insight:
        prev_block = f"\nPrevious insight (now outdated — trigger: {trigger_reason}):\n{json.dumps(last_insight.get('recommendations', {}), ensure_ascii=False)}\n"

    milestones_block = ""
    if milestones:
        lines = []
        for m in milestones[:5]:
            date_str = (m.get("achieved_at") or "")[:10]
            meta = m.get("metadata") or {}
            meta_str = f" ({', '.join(f'{k}={v}' for k, v in meta.items())})" if meta else ""
            lines.append(f"- {m['code']} on {date_str}{meta_str}")
        milestones_block = "\nRECENT MILESTONES (most recent first):\n" + "\n".join(lines) + "\n"

    overflow          = summary.get("overflow_status") or {}
    deployable_now    = overflow.get("deployable_overflow", 0)
    realized_overflow = overflow.get("realized_overflow", 0)
    deployable_cycle  = liquidity.get("deployable_this_cycle", 0)
    overflow_status   = overflow.get("status", "waiting")
    confirmed_balance = liquidity.get("confirmed_balance", 0)

    system = """You are a responsible personal finance advisor for a Colombian user paid in two quincenas per month.

CRITICAL INCOME TIMING CONTEXT:
- Base income (EMAPTA) arrives in TWO tranches: ~50% around day 5, ~50% around day 20.
- The system only tracks monthly aggregates, NOT intra-month timing.
- This means: even if the monthly total looks sufficient, the user may be cash-flow negative between day 1-5 (before first quincena) and day 5-20 (between quincenas).
- NEVER recommend deploying money if it would leave the user unable to pay rent or other obligations that fall due before the next quincena arrives.
- If confirmed_balance is less than the user's largest single monthly obligation (likely rent ~$2-3M COP), DO NOT recommend any deployment — the user needs that cash to bridge the gap until the next quincena.

STRICT GUARDRAILS:
1. deployable_overflow = income already arrived above the base plan, capped by confirmed_balance and next-cycle coverage.
2. realized_overflow = gross income above the base plan — this may be higher than deployable_overflow because the month's expenses already consumed part of it.
3. CRITICAL — if deployable_overflow < realized_overflow: you MUST explain this in your rationale. The cap is the confirmed_balance (net of all month expenses). Say: "overflow of X arrived but after month expenses the net balance is Y — that's the actual ceiling for deployment."
4. deployable_this_cycle = conditional projection only — never present as available today.
5. safe_to_deploy is an internal guardrail — never mention it to the user.
6. If confirmed_balance < 2_000_000 COP: primary_action must be about preserving cash for the quincena gap, NOT debt deployment.
7. PERSONAL NOTES / GOALS: Pay close attention to "user personal notes / goals". If there's an overarching life goal or preference (e.g., lose weight, move to a new apartment), align your recommendations and rationale to support it implicitly.
Priority order: cash flow timing > quality of life > debt payoff > savings goals.
When recent milestones are present, reference them in signals with type "ok".
Respond ONLY with a valid JSON object — no prose, no markdown."""

    user = f"""Financial state for {datetime.now(COLOMBIA_TZ).strftime('%B %Y')}:

LIQUIDITY:
- confirmed_balance: {confirmed_balance:,} COP  ← actual net balance (income - expenses this month)
- pending_variable: {liquidity.get('pending_variable', 0):,} COP  (NOT arrived yet)
- projected_eom_balance: {liquidity.get('projected_eom_balance', 0):,} COP
- next_cycle_obligations (full budget): {liquidity.get('next_cycle_obligations', 0):,} COP
- buffer_status: {liquidity.get('buffer_status', 'unknown')}

OVERFLOW (what's actionable):
- overflow_status: {overflow_status}  (waiting = pending income hasn't arrived; available = surplus arrived and ready)
- realized_overflow: {realized_overflow:,} COP  ← gross income above base plan this month
- deployable_overflow: {deployable_now:,} COP  ← MAX deployable. Capped by confirmed_balance. If less than realized_overflow, it's because expenses consumed the difference.
- deployable_this_cycle: {deployable_cycle:,} COP  ← Projection only if pending income arrives. Do NOT present as available today.

FINANCIAL CONTEXT:
- phase: {ctx.get('phase', 'unknown')}
- strategy: {ctx.get('strategy', 'unknown')}
- current recommended_action: {ctx.get('recommended_action', 'none')}
- user personal notes / goals: {ctx.get('notes', 'none')}

BURN RATE:
{json.dumps(burn_rate.get('categories', []), ensure_ascii=False, indent=2)}

DEBTS:
- total_balance: {debts.get('total_balance', 0):,} COP
- monthly_payments: {debts.get('monthly_payments', 0):,} COP
{milestones_block}{prev_block}
Generate a JSON insight with this exact structure:
{{
  "still_valid": false,
  "recommendations": {{
    "primary_action": "One concrete sentence. If overflow_status='waiting': say what will be possible once income arrives, no amounts. If overflow_status='available': state the deployable_overflow amount AND briefly why it differs from realized_overflow if they differ significantly.",
    "safe_to_deploy_suggested": <integer COP, must be <= deployable_overflow>,
    "rationale": "2-3 sentences. If deployable_overflow < realized_overflow, EXPLAIN the balance cap: overflow arrived but expenses consumed part of it — the net balance is what's available."
  }},
  "signals": [
    {{"type": "warn|info|ok", "category": "category_name or milestone", "message": "short observation"}}
  ],
  "reasoning": "Full internal reasoning. Honest, specific, no fluff."
}}"""

    resp = client.messages.create(
        model=SONNET_MODEL,
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": user}],
    )

    raw = resp.content[0].text.strip()
    # Strip markdown code fences if present
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
    last_insight = _get_current_insight(month, year)
    milestones   = _get_milestones()
    current      = _extract_current_state(summary, milestones)

    should, reason = _should_refresh(current, last_insight, now_col)

    if not should and trigger == "scheduled":
        logger.info("[insight] stable — skipping generation.")
        return

    if trigger != "scheduled":
        reason = trigger  # manual / on_demand

    logger.info("[insight] refresh needed — reason=%s", reason)

    client = _build_client()

    # Haiku validity gate (only if previous insight exists)
    if last_insight and trigger == "scheduled":
        still_valid = _haiku_still_valid(client, last_insight, summary)
        if still_valid:
            logger.info("[insight] Haiku confirmed previous insight still valid — skipping.")
            return

    result = _sonnet_generate(client, summary, last_insight, reason, milestones)

    deployable_overflow = (summary.get("overflow_status") or {}).get("deployable_overflow", 0)

    payload = {
        "period_month":           month,
        "period_year":            year,
        "generated_at":           now_col.isoformat(),
        "key_metrics_snapshot":   {**current, "period_month": month, "period_year": year},
        "recommendations":        result.get("recommendations", {}),
        "reasoning":              result.get("reasoning", ""),
        "signals":                result.get("signals", []),
        "safe_to_deploy_amount":  deployable_overflow,
        "trigger_reason":         reason,
    }

    _post_insight(payload)
    logger.info("[insight] insight persisted — trigger=%s deployable_overflow=%s", reason, deployable_overflow)
