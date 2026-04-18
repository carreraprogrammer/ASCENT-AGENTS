"""
adapters/rails_http.py — Implementación concreta del RailsApiPort usando httpx.

Esta es la única clase que conoce la URL de Rails y el token JWT.
"""

import os
import logging
import httpx

from ports.rails_api import RailsApiPort

logger = logging.getLogger(__name__)

BASE_URL = os.environ.get("DANIEL15K_API_URL", "https://daniel15k-api-production.up.railway.app")
API_TOKEN = os.environ.get("DANIEL15K_API_TOKEN", "")
SERVICE_TOKEN = os.environ.get("DANIEL15K_SERVICE_TOKEN", "")
ACCOUNT_ID = os.environ.get("DANIEL15K_ACCOUNT_ID", "")
AGENT_TYPE = os.environ.get("DANIEL15K_AGENT_TYPE", "finance_coach")
TIMEOUT = 15


def build_auth_headers() -> dict:
    if SERVICE_TOKEN and ACCOUNT_ID:
        return {
            "Authorization": f"Bearer {SERVICE_TOKEN}",
            "X-Account-Id": str(ACCOUNT_ID),
            "X-Agent-Type": AGENT_TYPE,
            "Content-Type": "application/json",
        }

    return {
        "Authorization": f"Bearer {API_TOKEN}",
        "Content-Type": "application/json",
    }


def _headers() -> dict:
    return build_auth_headers()


class RailsHttpAdapter(RailsApiPort):

    def _get(self, path: str, params: dict | None = None) -> dict | list:
        url = f"{BASE_URL}{path}"
        resp = httpx.get(url, headers=_headers(), params=params, timeout=TIMEOUT)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, body: dict | list) -> dict | list:
        url = f"{BASE_URL}{path}"
        payload = body
        if isinstance(body, dict) and "metadata" in body and body["metadata"] is not None:
            payload = {**body, "metadata": dict(body["metadata"])}

        resp = httpx.post(url, headers=_headers(), json=payload, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        return data.get("data", data) if isinstance(data, dict) and "data" in data else data

    def _patch(self, path: str, body: dict) -> dict:
        url = f"{BASE_URL}{path}"
        resp = httpx.patch(url, headers=_headers(), json=body, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        return data.get("data", data) if isinstance(data, dict) and "data" in data else data

    # --- summary ---

    def get_summary(self, month: int, year: int) -> dict:
        return self._get("/api/v1/summary", {"month": month, "year": year})

    # --- transactions ---

    def get_transactions(self, month: int, year: int) -> list[dict]:
        data = self._get("/api/v1/transactions", {"month": month, "year": year, "page": 1, "per_page": 100})
        return data if isinstance(data, list) else data.get("data", [])

    def get_pending_transactions(self) -> list[dict]:
        data = self._get("/api/v1/transactions/pending")
        return data if isinstance(data, list) else data.get("data", [])

    def get_balance(self) -> dict:
        return self._get("/api/v1/transactions/balance")

    def update_transaction(self, txn_id: int | str, **attrs) -> dict:
        return self._patch(f"/api/v1/transactions/{txn_id}", attrs)

    # --- pending actions ---

    def get_active_pending_action(self) -> dict | None:
        try:
            data = self._get("/api/v1/pending_actions/active")
            return data.get("data") or None
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise

    def create_pending_action(
        self,
        action_type: str,
        total_steps: int,
        context: dict,
        expires_at: str | None = None,
    ) -> dict:
        body = {
            "action_type": action_type,
            "total_steps": total_steps,
            "context": context,
            "status": "in_progress",
        }
        if expires_at:
            body["expires_at"] = expires_at
        return self._post("/api/v1/pending_actions", body)

    def update_pending_action(self, action_id: int | str, **attrs) -> dict:
        return self._patch(f"/api/v1/pending_actions/{action_id}", attrs)

    # --- financial context ---

    def get_financial_context(self) -> dict | None:
        try:
            data = self._get("/api/v1/financial_context")
            # _get no unwrapea — Rails devuelve {"data": ...}
            return data.get("data") or None
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise

    def update_financial_context(self, **attrs) -> dict:
        return self._patch("/api/v1/financial_context", attrs)

    # --- budgets ---

    def get_budgets(self, month: int, year: int) -> list[dict]:
        data = self._get("/api/v1/budgets", {"month": month, "year": year})
        return data if isinstance(data, list) else data.get("data", [])

    def create_budgets_bulk(self, budgets: list[dict]) -> list[dict]:
        data = self._post("/api/v1/budgets", {"budgets": budgets})
        return data if isinstance(data, list) else data.get("data", [])

    # --- monthly plans ---

    def get_current_monthly_plan(self, month: int, year: int) -> dict | None:
        data = self._get("/api/v1/monthly_plans/current", {"month": month, "year": year})
        return data.get("data") if isinstance(data, dict) else None

    def generate_monthly_plan(self, month: int, year: int, mode: str = "conservative") -> dict:
        return self._post("/api/v1/monthly_plans/generate", {"month": month, "year": year, "mode": mode})

    def confirm_monthly_plan(self, plan_id: int | str, *, budgets: list[dict] | None = None, **attrs) -> dict:
        body = {**attrs}
        if budgets is not None:
          body["budgets"] = budgets
        return self._post(f"/api/v1/monthly_plans/{plan_id}/confirm", body)

    def get_completeness(self, month: int, year: int) -> dict:
        data = self._get("/api/v1/completeness", {"month": month, "year": year})
        return data.get("data", data) if isinstance(data, dict) else {}

    def preflight_agent(self, *, intent: str, month: int, year: int) -> dict:
        return self._post("/api/v1/agents/preflight", {"intent": intent, "month": month, "year": year})

    # --- debts ---

    def get_debts(self) -> list[dict]:
        data = self._get("/api/v1/debts")
        return data if isinstance(data, list) else data.get("data", [])

    # --- categories ---

    def get_categories(self) -> list[dict]:
        data = self._get("/api/v1/categories")
        return data if isinstance(data, list) else data.get("data", [])

    # --- income sources ---

    def get_income_sources(self) -> list[dict]:
        data = self._get("/api/v1/income_sources")
        return data if isinstance(data, list) else data.get("data", [])

    def create_income_source(
        self,
        *,
        name: str,
        expected_amount: int,
        expected_day_from: int,
        expected_day_to: int,
        classification: str = "base",
        cadence: str = "monthly",
        reliability_score: int = 100,
        schedules: list[dict] | None = None,
        notes: str | None = None,
        is_variable: bool = False,
    ) -> dict:
        payload = {
            "name": name,
            "expected_amount": expected_amount,
            "expected_day_from": expected_day_from,
            "expected_day_to": expected_day_to,
            "classification": classification,
            "cadence": cadence,
            "reliability_score": reliability_score,
            "is_variable": is_variable,
        }
        if schedules is not None:
            payload["schedules"] = schedules
        if notes:
            payload["notes"] = notes
        result = self._post("/api/v1/income_sources", payload)
        return result.get("data", result) if isinstance(result, dict) else result

    # --- recurring obligations ---

    def get_recurring_obligations(self) -> list[dict]:
        data = self._get("/api/v1/recurring_obligations")
        return data if isinstance(data, list) else data.get("data", [])
