"""ASC-3: dobles para probar gmail_push y gmail_backfill sin red.

Todos los correos, montos y conceptos son ficticios."""
from __future__ import annotations

import base64
import json
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse

COLOMBIA_TZ = timezone(timedelta(hours=-5))

# Correos de ejemplo (ficticios)
PURCHASE = {
    "id": "m-compra",
    "from": "Banco Ficticio <alertas@banco.test>",
    "subject": "Compra aprobada",
    "body": "Compra aprobada por $45.000 en TIENDA FICTICIA 123 con tu tarjeta debito.",
}
PURCHASE_ECHO = {  # el mismo pago, notificado por el operador con otro texto
    "id": "m-compra-eco",
    "from": "Pasarela <no-reply@pasarela.test>",
    "subject": "Comprobante de pago",
    "body": "Tu pago por $45.000 a TIENDA FICTICIA fue exitoso.",
}
CARD_PAYMENT = {
    "id": "m-pago-tc",
    "from": "Banco Ficticio <alertas@banco.test>",
    "subject": "Pago recibido",
    "body": "Pago Tarjeta de Crédito por $300.000 aplicado.",
}
INBOUND = {
    "id": "m-entrante",
    "from": "Banco Ficticio <alertas@banco.test>",
    "subject": "Recibiste dinero",
    "body": "Recibiste una transferencia por Bre-B de PERSONA FICTICIA por $80.000",
}
NEWSLETTER = {
    "id": "m-boletin",
    "from": "Tienda <promo@tienda.test>",
    "subject": "Ofertas de la semana",
    "body": "Descuentos en toda la tienda. Visítanos.",
}
AMOUNTS = {"m-compra": 45000, "m-compra-eco": 45000, "m-entrante": 80000}


def gmail_message_json(email: dict) -> dict:
    data = base64.urlsafe_b64encode(email["body"].encode()).decode().rstrip("=")
    return {
        "id": email["id"],
        "snippet": email["body"][:40],
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": email["from"]},
                {"name": "Subject", "value": email["subject"]},
            ],
            "body": {"data": data},
        },
    }


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = json.dumps(self._payload)

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self):
        return self._payload

    def raise_for_status(self):
        if not self.is_success:
            import httpx

            request = httpx.Request("GET", "https://fake.test")
            raise httpx.HTTPStatusError("error", request=request, response=httpx.Response(self.status_code))


class FakeWorld:
    """Simula Gmail (solo lectura) y la API de Rails en memoria. Registra cada llamada."""

    def __init__(self, emails_by_day: dict[date, list[dict]] | None = None, transactions: list[dict] | None = None):
        self.emails_by_day = emails_by_day or {}
        self.messages = {e["id"]: e for day in self.emails_by_day.values() for e in day}
        self.transactions: list[dict] = list(transactions or [])  # filas JSON:API
        self.history_ids: list[str] = []
        self.get_calls: list[tuple[str, dict]] = []
        self.post_calls: list[tuple[str, dict]] = []
        self.next_id = 1000
        self.hints: dict[str, dict] = {}  # comercio → data de classification_hints

    # -- helpers --
    def add_message(self, email: dict) -> None:
        self.messages[email["id"]] = email

    def add_transaction(self, *, date_str: str, amount: int, ttype: str = "expense", source: str = "gmail",
                        sid: str | None = None, concept: str = "Movimiento ficticio", status: str = "confirmed",
                        savings_goal_id=None) -> dict:
        self.next_id += 1
        row = {
            "id": str(self.next_id),
            "type": "transactions",
            "attributes": {
                "date": date_str, "concept": concept, "amount": amount, "transaction_type": ttype,
                "source": source, "source_event_id": sid, "status": status,
                "metadata": {"source_event_id": sid} if sid else {},
                "savings_goal_id": savings_goal_id,
                "year": int(date_str[:4]), "month": int(date_str[5:7]),
            },
            "relationships": {},
        }
        self.transactions.append(row)
        return row

    def created_rows(self) -> list[dict]:
        return [p for (u, p) in self.post_calls if u.endswith("/api/v1/transactions")]

    def ui_events(self) -> list[dict]:
        return [p for (u, p) in self.post_calls if u.endswith("/api/v1/agent_events")]

    def gmail_calls(self) -> list[str]:
        return [u for (u, _p) in self.get_calls if "gmail.googleapis.com" in u]

    # -- httpx.get --
    def get(self, url, headers=None, params=None, timeout=None, **_kw):
        params = dict(params or {})
        self.get_calls.append((url, params))
        path = urlparse(url).path
        if path.endswith("/history"):
            return FakeResponse(200, {"history": [{"messagesAdded": [{"message": {"id": mid}}]} for mid in self.history_ids]})
        if path.endswith("/users/me/messages"):
            q = params.get("q", "")
            after = int(q.split("after:")[1].split()[0])
            day = datetime.fromtimestamp(after, COLOMBIA_TZ).date()
            ids = [e["id"] for e in self.emails_by_day.get(day, [])]
            ids = list(reversed(ids))  # Gmail devuelve primero el más reciente
            token = params.get("pageToken")
            start = int(token) if token else 0
            page = ids[start:start + 2]  # páginas de 2 para ejercitar la paginación
            body = {"messages": [{"id": i} for i in page]}
            if start + 2 < len(ids):
                body["nextPageToken"] = str(start + 2)
            return FakeResponse(200, body)
        if "/users/me/messages/" in path:
            mid = path.rsplit("/", 1)[1]
            if mid not in self.messages:
                return FakeResponse(404, {})
            body = gmail_message_json(self.messages[mid])
            for day, emails in self.emails_by_day.items():
                if any(e["id"] == mid for e in emails):  # mediodía de Colombia de ese día, en ms
                    noon = datetime(day.year, day.month, day.day, 12, tzinfo=COLOMBIA_TZ)
                    body["internalDate"] = str(int(noon.timestamp() * 1000))
            return FakeResponse(200, body)
        if path.endswith("/api/v1/me/email_connection/token"):
            return FakeResponse(200, {"data": {"access_token": "tok-ficticio", "bank_senders": []}})
        if path.endswith("/api/v1/transactions"):
            month, year = int(params["month"]), int(params["year"])
            rows = [t for t in self.transactions
                    if t["attributes"]["month"] == month and t["attributes"]["year"] == year]
            if params.get("source"):
                rows = [t for t in rows if t["attributes"]["source"] == params["source"]]
            rows.sort(key=lambda t: t["attributes"]["date"], reverse=True)
            per_page = min(int(params.get("per_page", 20)), 100)
            page = int(params.get("page", 1))
            chunk = rows[(page - 1) * per_page: page * per_page]
            return FakeResponse(200, {"data": chunk, "meta": {"has_next_page": page * per_page < len(rows)}})
        if path.endswith("/api/v1/categories"):
            return FakeResponse(200, {"data": [
                {"id": "1", "attributes": {"code": "mercado", "name": "Mercado", "category_type": "expense",
                                           "subcategories": [{"code": "supermercado", "name": "Supermercado"}]}},
            ]})
        if path.endswith("/classification_hints"):
            default = {"merchant": params.get("merchant"), "candidates": [], "samples": 0, "dominant": None}
            return FakeResponse(200, {"data": self.hints.get(params.get("merchant"), default)})
        if path.endswith("/api/v1/agent/accounts/demo"):
            return FakeResponse(200, {"data": {"id": 99, "demo": True}})
        return FakeResponse(404, {})

    # -- httpx.post --
    def post(self, url, headers=None, json=None, timeout=None, **_kw):
        self.post_calls.append((url, json))
        path = urlparse(url).path
        if "gmail.googleapis.com" in url:
            raise AssertionError("Gmail es solo lectura")
        if path.endswith("/api/v1/transactions"):
            sid = (json.get("metadata") or {}).get("source_event_id")
            for t in self.transactions:
                if sid and t["attributes"]["source_event_id"] == sid and t["attributes"]["source"] == json.get("source"):
                    return FakeResponse(409, {"errors": [{"detail": "Duplicate"}], "existing_id": t["id"]})
            row = self.add_transaction(date_str=json["date"], amount=json["amount"], ttype=json["transaction_type"],
                                       source=json.get("source", "manual"), sid=sid, concept=json["concept"],
                                       status=json.get("status", "confirmed"))
            row["attributes"]["subcategory_code"] = json.get("subcategory_code")
            row["attributes"]["payment_source"] = json.get("payment_source")
            return FakeResponse(201, {"data": row})
        if path.endswith("/api/v1/agent_events"):
            return FakeResponse(201, {"data": {"id": "e1"}})
        return FakeResponse(404, {})

    def forbid(self, *_a, **_kw):
        raise AssertionError("método HTTP de escritura no permitido")


class ScriptedProvider:
    """LLM doble: por cada correo del mensaje decide como lo haría el modelo, según AMOUNTS.

    - correos sin monto conocido (boletines) → no llama herramientas;
    - si el monto ya está en el bloque "TRANSACCIONES YA REGISTRADAS HOY" o ya lo creó en esta corrida
      del mismo lote → no lo duplica (deduplicación semántica);
    - "Force pending: sí" → status pending.
    """

    def __init__(self, txn_date: str | None = None, usage_per_call=(1000, 100)):
        self.calls: list[dict] = []
        self.txn_date = txn_date
        self.usage_totals = {"prompt_tokens": 0, "completion_tokens": 0, "prompt_cache_hit_tokens": 0}
        self._usage = usage_per_call

    def run_agent(self, *, system_prompt, tools, tool_map, initial_message, max_iterations=20, model=None,
                  prior_messages=None):
        self.calls.append({"system_prompt": system_prompt, "tools": tools, "initial_message": initial_message,
                           "max_iterations": max_iterations, "model": model, "tool_map": tool_map})
        self.usage_totals["prompt_tokens"] += self._usage[0]
        self.usage_totals["completion_tokens"] += self._usage[1]
        head, _, registered = initial_message.partition("TRANSACCIONES YA REGISTRADAS HOY")
        seen_amounts = {f"${a}" for a in AMOUNTS.values() if f"${a} " in registered or registered.endswith(f"${a}")}
        blocks = head.split("[ID: ")[1:]
        for block in blocks:
            mid = block.split("]", 1)[0]
            amount = AMOUNTS.get(mid)
            if amount is None:
                continue
            if f"${amount}" in seen_amounts:
                continue
            seen_amounts.add(f"${amount}")
            day = self.txn_date
            if "Fecha: " in block:
                day = block.split("Fecha: ", 1)[1][:10]
            tool_map["get_categories"]({})
            tool_map["create_transaction"]({
                "date": day,
                "concept": "Movimiento " + mid,
                "amount": amount,
                "transaction_type": "income" if mid == "m-entrante" else "expense",
                "status": "confirmed",
                "subcategory_code": "supermercado",
                "source": "gmail",
                "metadata": {"source_event_id": mid, "raw_text": block[:200], "subject": "x"},
            })
        return "listo"

    def simple_complete(self, *a, **kw):
        return ""
