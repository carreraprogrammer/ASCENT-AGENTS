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
from services.web_search import web_search as _web_search_http

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
            "description": "Gastos recurrentes. Incluye source_type/source_id cuando la obligación viene de una deuda o inversión estructural.",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "get_planned_expenses",
            "description": "Gastos futuros previsibles que todavía no son transacciones reales ni obligaciones mensuales.",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "create_transactions",
            "description": (
                "Crea múltiples transacciones en una sola llamada. "
                "Usá este tool cuando el mensaje mencione 2 o más gastos o ingresos con montos distintos. "
                "Más eficiente que llamar create_transaction varias veces. "
                "Incluí payment_source ('credit_card', 'debit', 'cash') si el usuario lo menciona o si se infiere del contexto."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "transactions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "date": {"type": "string"},
                                "concept": {"type": "string"},
                                "product": {"type": "string"},
                                "amount": {"type": "integer"},
                                "transaction_type": {"type": "string", "enum": ["expense", "income"]},
                                "status": {"type": "string", "enum": ["confirmed", "pending"]},
                                "category_id": {"type": "integer"},
                                "subcategory_id": {"type": "integer"},
                                "category_code": {"type": "string"},
                                "subcategory_code": {"type": "string"},
                                "source": {"type": "string", "enum": ["telegram", "gmail", "manual"]},
                                "payment_source": {"type": "string", "enum": ["credit_card", "debit", "cash"]},
                            },
                            "required": ["date", "concept", "amount", "transaction_type", "status"],
                        },
                    }
                },
                "required": ["transactions"],
            },
        },
        {
            "name": "create_transaction",
            "description": (
                "Crea una transacción. Para Telegram usa source=telegram. "
                "No inventes source_event_id: el sistema lo inyecta automáticamente. "
                "La API espera date en DD/MM/YYYY o DD/MM. "
                "Si el usuario menciona con qué pagó (tarjeta, Nequi, efectivo, débito), incluí payment_source: "
                "'credit_card' para cualquier tarjeta de crédito, 'debit' para débito/Nequi/transferencia, 'cash' para efectivo. "
                "Las compras con credit_card quedan como pendientes de pagar hasta que llegue el abono al banco."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "date": {"type": "string"},
                    "concept": {"type": "string"},
                    "product": {"type": "string"},
                    "amount": {"type": "integer"},
                    "transaction_type": {"type": "string", "enum": ["expense", "income"]},
                    "status": {"type": "string", "enum": ["confirmed", "pending"]},
                    "category_id": {"type": "integer"},
                    "subcategory_id": {"type": "integer"},
                    "category_code": {"type": "string"},
                    "subcategory_code": {"type": "string"},
                    "source": {"type": "string", "enum": ["telegram", "gmail", "manual"]},
                    "metadata": {"type": "object"},
                    "payment_source": {"type": "string", "enum": ["credit_card", "debit", "cash"]},
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
                    "status": {"type": "string", "enum": ["confirmed", "pending"]},
                    "category_id": {"type": "integer"},
                    "subcategory_id": {"type": "integer"},
                    "category_code": {"type": "string"},
                    "subcategory_code": {"type": "string"},
                    "payment_source": {"type": "string", "enum": ["credit_card", "debit", "cash"]},
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
            "description": (
                "Actualiza el contexto financiero. "
                "Si el usuario confirma que no tiene deudas, pasá debts_confirmed_at con la fecha de hoy (ISO 8601). "
                "Eso distingue 'sin deudas confirmadas' de 'no registró sus deudas'."
            ),
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
                    "debts_confirmed_at": {
                        "type": "string",
                        "description": "ISO 8601. Setear cuando el usuario confirma explícitamente que no tiene deudas.",
                    },
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
            "description": (
                "Crea un gasto fijo recurrente. "
                "Siempre intentá asignar category_id y subcategory_id usando get_categories primero. "
                "due_day es el día del mes en que vence (1-31). "
                "Si la obligación corresponde a una deuda ya existente, podés pasar source_type=Debt y source_id."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "amount": {"type": "integer"},
                    "due_day": {"type": "integer", "minimum": 1, "maximum": 31},
                    "category_id": {"type": "integer"},
                    "subcategory_id": {"type": "integer"},
                    "source_type": {"type": "string", "enum": ["Debt", "Investment"]},
                    "source_id": {"type": "integer"},
                    "active": {"type": "boolean"},
                    "notes": {"type": "string"},
                },
                "required": ["name", "amount"],
            },
        },
        {
            "name": "update_recurring_obligation",
            "description": "Actualiza un gasto fijo recurrente. Puede corregir monto, nombre, categoría, subcategoría, vínculo estructural o estado activo. Para desvincular una deuda, envía source_type=null y source_id=null.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                    "amount": {"type": "integer"},
                    "due_day": {"type": "integer", "minimum": 1, "maximum": 31},
                    "category_id": {"type": "integer"},
                    "subcategory_id": {"type": "integer"},
                    "source_type": {"type": ["string", "null"], "enum": ["Debt", "Investment", None]},
                    "source_id": {"type": ["integer", "null"]},
                    "active": {"type": "boolean"},
                    "notes": {"type": "string"},
                },
                "required": ["id"],
            },
        },
        {
            "name": "create_milestone",
            "description": (
                "Registra un hito del usuario (logro o setback). "
                "Úsalo SIEMPRE después de marcar una deuda como paid_off, "
                "o cuando el usuario reporte un evento financiero significativo."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "enum": [
                            "debt_paid_off", "first_debt_paid_off", "debt_free",
                            "emergency_fund_reached", "first_monthly_plan",
                            "three_months_planned", "investment_started",
                            "month_positive_balance", "discretionary_under_budget",
                            "overflow_deployed", "new_debt_acquired", "payment_missed",
                            "plan_not_confirmed",
                        ],
                        "description": "Código del hito.",
                    },
                    "metadata": {
                        "type": "object",
                        "description": "Contexto del hito. Para debt_paid_off incluir debt_name y amount.",
                    },
                },
                "required": ["code"],
            },
        },
        {
            "name": "create_planned_expense",
            "description": (
                "Crea un gasto futuro previsible que todavía no es transacción real ni obligación mensual. "
                "Úsalo para SOAT, tecnomecánica, viajes, ropa o compras planeadas."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "amount_estimated": {"type": "integer"},
                    "target_date": {"type": "string", "description": "Fecha ISO 8601 YYYY-MM-DD."},
                    "planning_type": {
                        "type": "string",
                        "enum": ["mandatory_one_off", "irregular_maintenance", "wish", "planned_purchase"],
                    },
                    "status": {"type": "string", "enum": ["planned", "executed", "cancelled"]},
                    "category_id": {"type": "integer"},
                    "subcategory_id": {"type": "integer"},
                    "notes": {"type": "string"},
                },
                "required": ["name", "amount_estimated", "target_date", "planning_type", "category_id", "subcategory_id"],
            },
        },
        {
            "name": "update_planned_expense",
            "description": "Actualiza un gasto planeado. Sirve para corregir monto/fecha o marcarlo como ejecutado o cancelado.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                    "amount_estimated": {"type": "integer"},
                    "target_date": {"type": "string", "description": "Fecha ISO 8601 YYYY-MM-DD."},
                    "planning_type": {
                        "type": "string",
                        "enum": ["mandatory_one_off", "irregular_maintenance", "wish", "planned_purchase"],
                    },
                    "status": {"type": "string", "enum": ["planned", "executed", "cancelled"]},
                    "category_id": {"type": "integer"},
                    "subcategory_id": {"type": "integer"},
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
            "name": "create_income_source",
            "description": (
                "Crea una fuente de ingreso recurrente. "
                "classification: 'base' para ingresos fijos confiables, 'variable' para ingresos inconsistentes, "
                "'seasonal' para ingresos estacionales. "
                "expected_day_from / expected_day_to definen la ventana del mes en que suele llegar (1-31)."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "expected_amount": {"type": "integer"},
                    "classification": {
                        "type": "string",
                        "enum": ["base", "variable", "seasonal"],
                    },
                    "expected_day_from": {"type": "integer", "minimum": 1, "maximum": 31},
                    "expected_day_to": {"type": "integer", "minimum": 1, "maximum": 31},
                    "active": {"type": "boolean"},
                    "notes": {"type": "string"},
                },
                "required": ["name", "expected_amount", "classification"],
            },
        },
        {
            "name": "update_income_source",
            "description": "Actualiza una fuente de ingreso existente. Puede corregir monto, nombre, clasificación o estado.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                    "expected_amount": {"type": "integer"},
                    "classification": {
                        "type": "string",
                        "enum": ["base", "variable", "seasonal"],
                    },
                    "expected_day_from": {"type": "integer", "minimum": 1, "maximum": 31},
                    "expected_day_to": {"type": "integer", "minimum": 1, "maximum": 31},
                    "active": {"type": "boolean"},
                    "notes": {"type": "string"},
                },
                "required": ["id"],
            },
        },
        {
            "name": "delete_income_source",
            "description": "Desactiva una fuente de ingreso.",
            "input_schema": {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        },
        {
            "name": "navigate_to",
            "description": (
                "Navega al usuario web a una página específica de la aplicación. "
                "Úsalo al final de un flujo para llevar al usuario al resultado. "
                "Ejemplos: /budgets tras confirmar el plan, /debts tras registrar una deuda. "
                "Solo disponible en modo web."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "route": {
                        "type": "string",
                        "description": "Ruta destino. Ej: /budgets, /debts, /dashboard, /recurring",
                    },
                },
                "required": ["route"],
            },
        },
        {
            "name": "emit_ui_event",
            "description": (
                "Emite un evento estructurado al front-end web (PWA). "
                "El front hace polling y renderiza el componente correspondiente al event_type. "
                "Úsalo para proponer un plan, mostrar una tarjeta informativa, "
                "solicitar confirmación o abrir un formulario dinámico. "
                "NO lo uses en flujos puramente de Telegram."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "event_type": {
                        "type": "string",
                        "enum": [
                            "show_plan_proposal",
                            "show_card",
                            "show_form",
                            "show_category_selector",
                            "show_amount_editor",
                            "request_confirmation",
                        ],
                    },
                    "payload": {
                        "type": "object",
                        "description": (
                            "Datos del evento. "
                            "show_plan_proposal: { draft: MonthlyPlanDraft, warnings: string[] }. "
                            "show_card: { title, body, tone: info|warning|success }. "
                            "show_form: { fields: DynamicField[], prefilled: object }. "
                            "show_category_selector: { categories: [{code, name, category_type, selected}], title, subtitle }. "
                            "show_amount_editor: { items: [{code, name, amount, editable}], title, subtitle }. "
                            "request_confirmation: { question, context }."
                        ),
                    },
                    "session_id": {
                        "type": "string",
                        "description": "Identificador de sesión opcional para filtrar eventos en el front.",
                    },
                },
                "required": ["event_type", "payload"],
            },
        },
        {
            "name": "web_search",
            "description": (
                "Busca en internet. Úsalo para investigar precios, tarifas, costos de "
                "servicios externos (SOAT, tecnomecánica, impuestos, seguros) o cualquier "
                "información que no esté disponible en la API. Antes de llamarlo en Telegram "
                "podés enviar un mensaje de espera con send_telegram."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Término de búsqueda. Sé específico: incluí país, año y modelo si aplica.",
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "send_telegram",
            "description": (
                "Envía la respuesta final. Soporta inline_keyboard. "
                "Formatos de callback_data: "
                "'cat:{id}:{subcat_code}' | 'pay:{id}:{payment_source}' (credit_card|debit|cash) | 'confirm:{id}' | 'skip:{id}'."
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


def _send_telegram(messenger: MessengerPort, payload: dict, state: dict | None = None) -> dict:
    message = payload.get("message") or payload.get("mensaje") or ""
    normalized = normalize_telegram_html(message)
    button_rows = payload.get("inline_keyboard") or []

    logger.info(
        "[chat_agent] send_telegram message_len=%d button_rows=%d",
        len(normalized),
        len(button_rows),
    )

    if button_rows:
        messenger.send_with_buttons(normalized, button_rows)
    else:
        messenger.send_message(normalized)

    if state is not None and normalized:
        state["last_response"] = normalized

    return {"ok": True}


def build_tool_map(
    api: RailsApiPort,
    messenger: MessengerPort,
    now: datetime,
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    month, year = now.month, now.year
    today = now.date()
    state = state or {"responded": False, "mutated": False, "source_event_id": None, "transaction_index": 0}

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

    def _inject_source_event_id(payload: dict) -> dict:
        """Inyecta source_event_id indexado para idempotencia por posición dentro del mensaje."""
        metadata = dict(payload.get("metadata") or {})
        if payload.get("source") == "telegram" and state.get("source_event_id"):
            idx = state.get("transaction_index", 0)
            metadata["source_event_id"] = f"{state['source_event_id']}:{idx}"
            state["transaction_index"] = idx + 1
        if metadata:
            payload = {**payload, "metadata": metadata}
        return payload

    def _create_transaction(input_data: dict) -> dict:
        payload = _inject_source_event_id(dict(input_data))
        source_event_id = (payload.get("metadata") or {}).get("source_event_id")

        logger.info(
            "[chat_agent] create_transaction amount=%s concept=%s date=%s source=%s event_id=%s",
            payload.get("amount"),
            payload.get("concept"),
            payload.get("date"),
            payload.get("source"),
            source_event_id,
        )

        try:
            result = _post("/api/v1/transactions", payload)
            logger.info("[chat_agent] create_transaction OK id=%s", result.get("id") if isinstance(result, dict) else "?")
            return result
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 409:
                raise

            try:
                data = exc.response.json()
            except ValueError:
                data = {}

            duplicate_result = {
                "ok": True,
                "duplicate": True,
                "already_recorded": True,
                "existing_id": data.get("existing_id"),
                "detail": ((data.get("errors") or [{}])[0]).get("detail") or "Duplicate transaction",
            }
            logger.info(
                "[chat_agent] create_transaction DUPLICATE existing_id=%s",
                duplicate_result["existing_id"],
            )
            return duplicate_result

    def _create_transactions(input_data: dict) -> dict:
        transactions = input_data.get("transactions", [])
        logger.info("[chat_agent] create_transactions batch_size=%d", len(transactions))

        prepared = []
        for txn in transactions:
            prepared.append(_inject_source_event_id(dict(txn)))

        for i, txn in enumerate(prepared):
            logger.info(
                "[chat_agent]   [%d/%d] amount=%s concept=%s date=%s event_id=%s",
                i + 1, len(prepared),
                txn.get("amount"), txn.get("concept"), txn.get("date"),
                (txn.get("metadata") or {}).get("source_event_id"),
            )

        response = httpx.post(
            f"{API_URL}/api/v1/transactions/batch",
            headers=build_auth_headers(),
            json={"transactions": prepared},
            timeout=30,
        )
        response.raise_for_status()
        state["mutated"] = True
        result = response.json()

        created = result.get("data", [])
        errors  = result.get("errors", [])
        logger.info(
            "[chat_agent] create_transactions DONE created=%d errors=%d",
            len(created), len(errors),
        )
        if errors:
            for err in errors:
                logger.warning("[chat_agent]   error index=%s detail=%s", err.get("index"), err.get("detail"))

        return {"created": len(created), "errors": len(errors), "transactions": created}

    def _web_search_with_notice(input_data: dict) -> dict:
        query = input_data.get("query", "")
        logger.info("[chat_agent] web_search query=%r", query)
        messenger.send_message("🔍 Regalame un momento, estoy investigando...")
        try:
            result = _web_search_http(query)
            return result
        except Exception as exc:
            logger.error("[chat_agent] web_search failed: %s", exc)
            return {"error": str(exc), "results": []}

    def _trigger_fc_wizard(_: dict) -> dict:
        from flows import financial_context_wizard

        financial_context_wizard.trigger(api, messenger)
        return {"ok": True, "message": "Wizard iniciado en Telegram."}

    def _navigate_to(input_data: dict) -> dict:
        return _emit_ui_event({"event_type": "navigate", "payload": {"route": input_data["route"]}})

    def _emit_ui_event(input_data: dict) -> dict:
        body = {
            "event_type": input_data["event_type"],
            "payload": input_data.get("payload", {}),
        }
        session_id = input_data.get("session_id") or state.get("session_id")
        if session_id:
            body["session_id"] = session_id

        response = httpx.post(
            f"{API_URL}/api/v1/agent_events",
            headers=build_auth_headers(),
            json=body,
            timeout=15,
        )
        response.raise_for_status()
        return response.json().get("data", {})

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
        "get_planned_expenses": lambda _: api.get_planned_expenses(),
        "create_transactions": _create_transactions,
        "create_transaction": _create_transaction,
        "update_transaction": lambda p: _patch(f"/api/v1/transactions/{p.pop('id')}", p),
        "delete_transaction": lambda p: _delete(f"/api/v1/transactions/{p['id']}"),
        "update_financial_context": lambda p: api.update_financial_context(**p),
        "trigger_financial_context_wizard": _trigger_fc_wizard,
        "navigate_to": _navigate_to,
        "emit_ui_event": _emit_ui_event,
        "update_debt": lambda p: _patch(f"/api/v1/debts/{p.pop('id')}", p),
        "delete_debt": lambda p: _delete(f"/api/v1/debts/{p['id']}"),
        "create_recurring_obligation": lambda p: _post("/api/v1/recurring_obligations", p),
        "update_recurring_obligation": lambda p: _patch(f"/api/v1/recurring_obligations/{p.pop('id')}", p),
        "create_milestone": lambda p: api.create_milestone(p["code"], p.get("metadata", {})),
        "delete_recurring_obligation": lambda p: _delete(f"/api/v1/recurring_obligations/{p['id']}"),
        "create_planned_expense": lambda p: _post("/api/v1/planned_expenses", p),
        "update_planned_expense": lambda p: _patch(f"/api/v1/planned_expenses/{p.pop('id')}", p),
        "create_income_source": lambda p: _post("/api/v1/income_sources", p),
        "update_income_source": lambda p: _patch(f"/api/v1/income_sources/{p.pop('id')}", p),
        "delete_income_source": lambda p: _delete(f"/api/v1/income_sources/{p['id']}"),
        "web_search": _web_search_with_notice,
        "send_telegram": lambda p: state.update({"responded": True}) or _send_telegram(messenger, p, state),
    }
