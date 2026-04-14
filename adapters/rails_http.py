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
TIMEOUT = 15


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {API_TOKEN}",
        "Content-Type": "application/json",
    }


class RailsHttpAdapter(RailsApiPort):

    def _get(self, path: str, params: dict | None = None) -> dict | list:
        url = f"{BASE_URL}{path}"
        resp = httpx.get(url, headers=_headers(), params=params, timeout=TIMEOUT)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, body: dict | list) -> dict | list:
        url = f"{BASE_URL}{path}"
        resp = httpx.post(url, headers=_headers(), json=body, timeout=TIMEOUT)
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
        data = self._get("/api/v1/transactions", {"month": month, "year": year})
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

    # --- debts ---

    def get_debts(self) -> list[dict]:
        data = self._get("/api/v1/debts")
        return data if isinstance(data, list) else data.get("data", [])

    # --- categories ---

    def get_categories(self) -> list[dict]:
        data = self._get("/api/v1/categories")
        return data if isinstance(data, list) else data.get("data", [])
