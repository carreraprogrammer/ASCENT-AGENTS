"""
ports/rails_api.py — Contrato (interfaz) para el cliente de la API de Rails.

El Brain depende de esta abstracción, nunca de la implementación concreta.
Si mañana Rails se reemplaza por otra API, solo cambia el adapter.
"""

from abc import ABC, abstractmethod
from typing import Any


class RailsApiPort(ABC):

    @abstractmethod
    def get_summary(self, month: int, year: int) -> dict:
        """GET /api/v1/summary?month=&year="""
        ...

    @abstractmethod
    def get_transactions(self, month: int, year: int) -> list[dict]:
        """GET /api/v1/transactions?month=&year="""
        ...

    @abstractmethod
    def get_pending_transactions(self) -> list[dict]:
        """GET /api/v1/transactions/pending"""
        ...

    @abstractmethod
    def get_balance(self) -> dict:
        """GET /api/v1/transactions/balance"""
        ...

    @abstractmethod
    def update_transaction(self, txn_id: int | str, **attrs) -> dict:
        """PATCH /api/v1/transactions/:id"""
        ...

    @abstractmethod
    def get_active_pending_action(self) -> dict | None:
        """GET /api/v1/pending_actions/active — None si no hay ninguno activo"""
        ...

    @abstractmethod
    def create_pending_action(self, action_type: str, total_steps: int, context: dict, expires_at: str | None = None) -> dict:
        """POST /api/v1/pending_actions"""
        ...

    @abstractmethod
    def update_pending_action(self, action_id: int | str, **attrs) -> dict:
        """PATCH /api/v1/pending_actions/:id"""
        ...

    @abstractmethod
    def get_financial_context(self) -> dict | None:
        """GET /api/v1/financial_context"""
        ...

    @abstractmethod
    def update_financial_context(self, **attrs) -> dict:
        """PATCH /api/v1/financial_context"""
        ...

    @abstractmethod
    def get_budgets(self, month: int, year: int) -> list[dict]:
        """GET /api/v1/budgets?month=&year="""
        ...

    @abstractmethod
    def create_budgets_bulk(self, budgets: list[dict]) -> list[dict]:
        """POST /api/v1/budgets (acepta array)"""
        ...

    @abstractmethod
    def get_current_monthly_plan(self, month: int, year: int) -> dict | None:
        """GET /api/v1/monthly_plans/current?month=&year="""
        ...

    @abstractmethod
    def generate_monthly_plan(self, month: int, year: int, mode: str = "conservative") -> dict:
        """POST /api/v1/monthly_plans/generate"""
        ...

    @abstractmethod
    def confirm_monthly_plan(self, plan_id: int | str, *, budgets: list[dict] | None = None, **attrs) -> dict:
        """POST /api/v1/monthly_plans/:id/confirm"""
        ...

    @abstractmethod
    def get_completeness(self, month: int, year: int) -> dict:
        """GET /api/v1/completeness?month=&year="""
        ...

    @abstractmethod
    def preflight_agent(self, *, intent: str, month: int, year: int) -> dict:
        """POST /api/v1/agents/preflight"""
        ...

    @abstractmethod
    def get_debts(self) -> list[dict]:
        """GET /api/v1/debts"""
        ...

    @abstractmethod
    def get_categories(self) -> list[dict]:
        """GET /api/v1/categories"""
        ...

    @abstractmethod
    def get_income_sources(self) -> list[dict]:
        """GET /api/v1/income_sources"""
        ...

    @abstractmethod
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
        schedules: list[dict[str, Any]] | None = None,
        notes: str | None = None,
        is_variable: bool = False,
    ) -> dict:
        """POST /api/v1/income_sources"""
        ...

    @abstractmethod
    def get_recurring_obligations(self) -> list[dict]:
        """GET /api/v1/recurring_obligations"""
        ...

    @abstractmethod
    def get_planned_expenses(self) -> list[dict]:
        """GET /api/v1/planned_expenses"""
        ...

    @abstractmethod
    def create_planned_expense(
        self,
        *,
        name: str,
        amount_estimated: int,
        target_date: str,
        planning_type: str,
        status: str = "planned",
        category_id: int,
        subcategory_id: int,
        notes: str | None = None,
    ) -> dict:
        """POST /api/v1/planned_expenses"""
        ...

    @abstractmethod
    def update_planned_expense(self, planned_expense_id: int | str, **attrs) -> dict:
        """PATCH /api/v1/planned_expenses/:id"""
        ...

    @abstractmethod
    def get_milestones(self) -> list[dict]:
        """GET /api/v1/milestones"""
        ...

    @abstractmethod
    def create_milestone(self, code: str, metadata: dict) -> dict:
        """POST /api/v1/milestones"""
        ...
