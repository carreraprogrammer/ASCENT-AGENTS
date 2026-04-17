"""Tool schemas y wiring del agente conversacional financiero."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import httpx

from adapters.rails_http import BASE_URL as API_BASE_URL, build_auth_headers
from ports.messenger import MessengerPort
from ports.rails_api import RailsApiPort
from services.chat_context import flatten_transaction, normalize_telegram_html, parse_api_date

logger = logging.getLogger(__name__)
API_URL = API_BASE_URL


def build_tools() -> list[dict[str, Any]]:
    return [
        {
            "name": "get_summary",
            "description": "Resumen financiero del mes: balance, burn rate, deudas, monthly_plan, overflow_status y contexto.",
            "input_schema": {
                "type": "object",
                "properties": {"month": {"type": "integer"}, "year": {"type": "integer"}},
                "required": ["month", "year"],
            },
        },
        {
            "name": "get_transactions",
            "description": "Transacciones del mes.",
            "input_schema": {
                "type": "object",
                "properties": {"month": {"type": "integer"}, "year": {"type": "integer"}},
                "required": ["month", "year"],
            },
        },
        {
            "name": "get_recent_transactions",
            "description": "Transacciones recientes. Úsalo para corregir o borrar 'ese gasto'.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "minimum": 1, "maximum": 31},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                },
                "required": [],
            },
        },
        {
            "name": "get_categories",
            "description": "Categorías y subcategorías disponibles.",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "get_budgets",
            "description": "Presupuestos del mes.",
            "input_schema": {
                "type": "object",
                "properties": {"month": {"type": "integer"}, "year": {"type": "integer"}},
                "required": ["month", "year"],
            },
        },
        {
            "name": "get_debts",
            "description": "Deudas activas.",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "get_balance",
            "description": "Saldo disponible actual.",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "get_financial_context",
            "description": "Contexto financiero actual.",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "get_income_sources",
            "description": "Fuentes de ingreso.",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "get_recurring_obligations",
            "description": "Gastos recurrentes.",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "create_transaction",
            "description": (
                "Crea una transacción. Para Telegram usa source=telegram. "
                "No inventes source_event_id: el sistema lo inyecta automáticamente. "
                "La API espera date en DD/MM/YYYY o DD/MM."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "date": {"type": "string"},
                    "concept": {"type": "string"},
                    "product": {"type": "string"},
                    "amount": {"type": "integer"},
                    "transaction_type": {"type": "string", "enum": ["expense", "income"]},
                    "status": {"type": "string", "enum": ["confirmed", "pending", "projected"]},
                    "category_id": {"type": "integer"},
                    "subcategory_id": {"type": "integer"},
                    "category_code": {"type": "string"},
                    "subcategory_code": {"type": "string"},
                    "source": {"type": "string", "enum": ["telegram", "gmail", "manual"]},
                    "metadata": {"type": "object"},
                },
                "required": ["date", "concept", "amount", "transaction_type", "status"],
            },
        },
        {
            "name": "update_transaction",
            "description": "Corrige una transacción existente por ID.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "date": {"type": "string"},
                    "concept": {"type": "string"},
                    "product": {"type": "string"},
                    "amount": {"type": "integer"},
                    "status": {"type": "string", "enum": ["confirmed", "pending", "projected"]},
                    "category_id": {"type": "integer"},
                    "subcategory_id": {"type": "integer"},
                    "category_code": {"type": "string"},
                    "subcategory_code": {"type": "string"},
                    "clarification_resolved_at": {"type": "string"},
                    "metadata": {"type": "object"},
                },
                "required": ["id"],
            },
        },
        {
            "name": "delete_transaction",
            "description": "Elimina una transacción por ID.",
            "input_schema": {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        },
        {
            "name": "update_financial_context",
            "description": "Actualiza el contexto financiero.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "phase": {
                        "type": "string",
                        "enum": ["debt_payoff", "emergency_fund", "investing", "wealth_building"],
                    },
                    "strategy": {"type": "string", "enum": ["snowball", "avalanche"]},
                    "reward_pct": {"type": "integer", "minimum": 1, "maximum": 100},
                    "notes": {"type": "string"},
                },
                "required": [],
            },
        },
        {
            "name": "trigger_financial_context_wizard",
            "description": "Activa el wizard guiado de contexto financiero en Telegram.",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "update_debt",
            "description": "Actualiza una deuda.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "current_balance": {"type": "integer"},
                    "monthly_payment": {"type": "integer"},
                    "interest_rate": {"type": "number"},
                    "status": {"type": "string", "enum": ["active", "paid_off", "paused", "disputed"]},
                    "notes": {"type": "string"},
                },
                "required": ["id"],
            },
        },
        {
            "name": "delete_debt",
            "description": "Elimina una deuda por ID.",
            "input_schema": {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        },
        {
            "name": "create_recurring_obligation",
            "description": "Crea un gasto fijo recurrente.",
            "input_schema": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "amount": {"type": "integer"}, "notes": {"type": "string"}},
                "required": ["name", "amount"],
            },
        },
        {
            "name": "update_recurring_obligation",
            "description": "Actualiza un gasto fijo recurrente.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                    "amount": {"type": "integer"},
                    "active": {"type": "boolean"},
                    "notes": {"type": "string"},
                },
                "required": ["id"],
            },
        },
        {
            "name": "delete_recurring_obligation",
            "description": "Desactiva un gasto fijo recurrente.",
            "input_schema": {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        },
        {
            "name": "send_telegram",
            "description": (
                "Envía la respuesta final. Soporta inline_keyboard. "
                "Para callbacks rápidos usa chat:... como callback_data."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "message": {"type": "string"},
                    "mensaje": {"type": "string"},
                    "inline_keyboard": {
                        "type": "array",
                        "items": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "text": {"type": "string"},
                                    "callback_data": {"type": "string"},
                                },
                                "required": ["text", "callback_data"],
                            },
                        },
                    },
                },
                "required": [],
            },
        },
    ]


def _send_telegram(messenger: MessengerPort, payload: dict) -> dict:
    message = payload.get("message") or payload.get("mensaje") or ""
    normalized = normalize_telegram_html(message)

    if payload.get("inline_keyboard"):
        messenger.send_with_buttons(normalized, payload["inline_keyboard"])
    else:
        messenger.send_message(normalized)

    return {"ok": True}


def build_tool_map(
    api: RailsApiPort,
    messenger: MessengerPort,
    now: datetime,
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    month, year = now.month, now.year
    today = now.date()
    state = state or {"responded": False, "mutated": False, "source_event_id": None}

    def _patch(path: str, body: dict) -> dict:
        response = httpx.patch(
            f"{API_URL}{path}",
            headers=build_auth_headers(),
            json=body,
            timeout=15,
        )
        response.raise_for_status()
        state["mutated"] = True
        return response.json().get("data", {})

    def _delete(path: str) -> dict:
        response = httpx.delete(
            f"{API_URL}{path}",
            headers=build_auth_headers(),
            timeout=15,
        )
        response.raise_for_status()
        state["mutated"] = True
        return {"ok": True}

    def _post(path: str, body: dict) -> dict:
        response = httpx.post(
            f"{API_URL}{path}",
            headers=build_auth_headers(),
            json=body,
            timeout=15,
        )
        response.raise_for_status()
        state["mutated"] = True
        return response.json().get("data", {})

    def _normalize_categories() -> list[dict]:
        categories: list[dict] = []
        for raw in api.get_categories():
            attributes = raw.get("attributes", raw)
            subcategories = raw.get("relationships", {}).get("subcategories", {}).get("data", [])
            categories.append(
                {
                    "id": raw.get("id"),
                    "name": attributes.get("name"),
                    "code": attributes.get("code"),
                    "category_type": attributes.get("category_type"),
                    "subcategories": [
                        {
                            "id": sub.get("id"),
                            "name": sub.get("attributes", {}).get("name"),
                            "code": sub.get("attributes", {}).get("code"),
                        }
                        for sub in subcategories
                    ],
                }
            )
        return categories

    def _get_recent_transactions(input_data: dict) -> dict:
        days = int(input_data.get("days", 7))
        limit = int(input_data.get("limit", 10))
        cutoff = today - timedelta(days=max(days - 1, 0))
        month_keys = {(today.year, today.month), (cutoff.year, cutoff.month)}

        rows: list[dict] = []
        for tx_year, tx_month in month_keys:
            try:
                rows.extend(api.get_transactions(tx_month, tx_year))
            except Exception as exc:
                logger.warning(
                    "[chat_agent] recent transactions fetch failed for %s-%s: %s",
                    tx_year,
                    tx_month,
                    exc,
                )

        flattened: list[dict] = []
        for row in rows:
            flat = flatten_transaction(row)
            tx_date = parse_api_date(flat.get("date"), fallback_year=flat.get("year"))
            if not tx_date or tx_date < cutoff:
                continue

            flat["_sort_key"] = (tx_date.isoformat(), flat.get("id") or "")
            flattened.append(flat)

        flattened.sort(key=lambda item: item["_sort_key"], reverse=True)
        for item in flattened:
            item.pop("_sort_key", None)

        recent = flattened[:limit]
        return {"transactions": recent, "total": len(recent), "days": days}

    def _create_transaction(input_data: dict) -> dict:
        payload = dict(input_data)
        metadata = dict(payload.get("metadata") or {})

        if payload.get("source") == "telegram" and state.get("source_event_id"):
            metadata["source_event_id"] = state["source_event_id"]

        if metadata:
            payload["metadata"] = metadata

        return _post("/api/v1/transactions", payload)

    def _trigger_fc_wizard(_: dict) -> dict:
        from flows import financial_context_wizard

        financial_context_wizard.trigger(api, messenger)
        return {"ok": True, "message": "Wizard iniciado en Telegram."}

    return {
        "get_summary": lambda p: api.get_summary(p.get("month", month), p.get("year", year)),
        "get_transactions": lambda p: api.get_transactions(p.get("month", month), p.get("year", year)),
        "get_recent_transactions": _get_recent_transactions,
        "get_categories": lambda _: {"categories": _normalize_categories()},
        "get_budgets": lambda p: api.get_budgets(p.get("month", month), p.get("year", year)),
        "get_debts": lambda _: api.get_debts(),
        "get_balance": lambda _: api.get_balance(),
        "get_financial_context": lambda _: api.get_financial_context(),
        "get_income_sources": lambda _: api.get_income_sources(),
        "get_recurring_obligations": lambda _: api.get_recurring_obligations(),
        "create_transaction": _create_transaction,
        "update_transaction": lambda p: _patch(f"/api/v1/transactions/{p.pop('id')}", p),
        "delete_transaction": lambda p: _delete(f"/api/v1/transactions/{p['id']}"),
        "update_financial_context": lambda p: api.update_financial_context(**p),
        "trigger_financial_context_wizard": _trigger_fc_wizard,
        "update_debt": lambda p: _patch(f"/api/v1/debts/{p.pop('id')}", p),
        "delete_debt": lambda p: _delete(f"/api/v1/debts/{p['id']}"),
        "create_recurring_obligation": lambda p: _post("/api/v1/recurring_obligations", p),
        "update_recurring_obligation": lambda p: _patch(f"/api/v1/recurring_obligations/{p.pop('id')}", p),
        "delete_recurring_obligation": lambda p: _delete(f"/api/v1/recurring_obligations/{p['id']}"),
        "send_telegram": lambda p: state.update({"responded": True}) or _send_telegram(messenger, p),
    }
