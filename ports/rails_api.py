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
    def get_recurring_obligations(self) -> list[dict]:
        """GET /api/v1/recurring_obligations"""
        ...
