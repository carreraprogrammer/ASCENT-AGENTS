"""El servidor MCP de Ascent, primer cromo de The Cyborg Way (TCW-1 fase 4).

Se monta en /mcp. Solo responde a tokens del cerebro emitidos para Ascent (cromo.tokens): el SDK de MCP contesta
401 con WWW-Authenticate y publica /.well-known/oauth-protected-resource/mcp, que nombra al cerebro como servidor de
autorización. Herramientas:

  lectura (scope ascent:read)    resumen_mes, gasto_por_categoria, obligaciones_proximas, ultimos_movimientos
  escritura (scope ascent:write) registrar_gasto

Los datos salen de la API de Rails con la cuenta del dueño (MVP de un solo usuario: DANIEL15K_ACCOUNT_ID).
Nada de lo que devuelven se escribe en logs.

Modo demo (ASC-4): si el token del cerebro trae demo: true, todas las herramientas leen la cuenta demo
(RailsHttpAdapter(account_id=<id demo>), con el id que da GET /api/v1/agent/accounts/demo, guardado 5 minutos) y
nunca la del dueño: si el id no se puede resolver, la herramienta falla en vez de caer a la cuenta del dueño.
registrar_gasto responde "modo demo: solo lectura".
"""
import os
import threading
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from cromo.tokens import ASCENT_RESOURCE, BRAIN_ISSUER, BrainTokenVerifier

READ, WRITE = "ascent:read", "ascent:write"
COLOMBIA = timezone(timedelta(hours=-5))
DEMO_READ_ONLY = "modo demo: solo lectura"
DEMO_UNAVAILABLE = "modo demo: la cuenta demo no está disponible"
DEMO_ACCOUNT_TTL = 300


class DemoAccount:
    """El id de la cuenta demo, resuelto por la API y guardado un rato. resolve() lo pide (por defecto
    RailsHttpAdapter().get_demo_account_id()). Un fallo no se guarda y nunca se reemplaza por otra cuenta."""

    def __init__(self, resolve=None, ttl: float = DEMO_ACCOUNT_TTL):
        self.resolve, self.ttl = resolve, ttl
        self.value, self.read_at = "", 0.0
        self.lock = threading.Lock()

    def _resolve(self) -> str:
        if self.resolve:
            return self.resolve()
        from adapters.rails_http import RailsHttpAdapter
        return RailsHttpAdapter().get_demo_account_id()

    def get(self) -> str:
        """El id de la cuenta demo, o "" si la API no lo da."""
        with self.lock:
            if self.value and time.time() - self.read_at < self.ttl:
                return self.value
            try:
                value = str(self._resolve() or "")
            except Exception:
                value = ""
            if value:
                self.value, self.read_at = value, time.time()
            return value

    def forget(self) -> None:
        """Tras un reset de la demo: el próximo uso vuelve a preguntar el id."""
        with self.lock:
            self.value, self.read_at = "", 0.0


DEMO_ACCOUNT = DemoAccount()


def in_demo() -> bool:
    token = get_access_token()
    return bool(getattr(token, "demo", False))


def today() -> date:
    return datetime.now(COLOMBIA).date()


def need(scope: str) -> None:
    token = get_access_token()
    if token is None or scope not in token.scopes:
        raise ToolError(f"este token no tiene el scope {scope}")


def category_id(txn: dict) -> str | None:
    data = ((txn.get("relationships") or {}).get("category") or {}).get("data") or {}
    return str(data["id"]) if data.get("id") else None


def category_names(api) -> dict:
    names = {}
    for c in api.get_categories():
        attrs = c.get("attributes", c)
        names[str(c.get("id"))] = attrs.get("name")
    return names


def make_server(api_factory, verifier: BrainTokenVerifier | None = None, issuer: str = BRAIN_ISSUER,
                resource: str = ASCENT_RESOURCE, demo_account: DemoAccount | None = None) -> MCPServer:
    """api_factory() da el adaptador de Rails de la cuenta del dueño, y api_factory(account_id=<id>) el de otra
    cuenta (RailsHttpAdapter en producción, un doble en los tests). demo_account resuelve la cuenta demo."""
    demo_account = demo_account or DEMO_ACCOUNT

    def account_api():
        """El adaptador de la cuenta que toca: la demo si el token trae demo: true, si no la del dueño."""
        if not in_demo():
            return api_factory()
        demo_id = demo_account.get()
        if not demo_id:                                    # nunca caer a la cuenta del dueño
            raise ToolError(DEMO_UNAVAILABLE)
        return api_factory(account_id=demo_id)

    auth = AuthSettings(issuer_url=issuer, resource_server_url=resource, required_scopes=[READ],
                        validate_token_resource=True)
    mcp = MCPServer("ascent", instructions="Ascent: finanzas personales del dueño. Montos en pesos colombianos.",
                    auth=auth, token_verifier=verifier or BrainTokenVerifier(issuer=issuer, resource=resource))

    def periodo(month: int | None, year: int | None) -> tuple[int, int]:
        now = today()
        return int(month or now.month), int(year or now.year)

    def resumen_mes(month: int | None = None, year: int | None = None) -> dict:
        """Resumen del mes: ingresos y gastos (confirmados y pendientes), lo que queda por recibir y el margen
        libre. month y year opcionales (por defecto, el mes en curso en Colombia)."""
        need(READ)
        m, y = periodo(month, year)
        summary = account_api().get_summary(m, y)
        balance = summary.get("balance") or {}
        income = (summary.get("month_execution") or {}).get("income") or {}
        runway = summary.get("cash_flow_runway") or {}
        return {"mes": f"{y}-{m:02d}",
                "ingresos_confirmados": balance.get("income_confirmed"),
                "ingresos_pendientes": balance.get("income_pending"),
                "gastos_confirmados": balance.get("expense_confirmed"),
                "gastos_pendientes": balance.get("expense_pending"),
                "ingreso_esperado_por_recibir": income.get("remaining_expected_total"),
                "margen_libre": runway.get("commitment_gap")}

    def gasto_por_categoria(month: int | None = None, year: int | None = None) -> list[dict]:
        """Gasto del mes por categoría, de mayor a menor: total y número de movimientos."""
        need(READ)
        m, y = periodo(month, year)
        api = account_api()
        totals, counts = defaultdict(int), defaultdict(int)
        names = category_names(api)
        for txn in api.get_transactions(m, y):
            attrs = txn.get("attributes", txn)
            if attrs.get("transaction_type") != "expense":
                continue
            category = names.get(category_id(txn), "sin categoría")
            totals[category] += int(attrs.get("amount") or 0)
            counts[category] += 1
        return [{"categoria": c, "total": totals[c], "movimientos": counts[c]}
                for c in sorted(totals, key=totals.get, reverse=True)]

    def obligaciones_proximas(dias: int = 15) -> list[dict]:
        """Obligaciones recurrentes activas que vencen en los próximos `dias` días (por defecto 15)."""
        need(READ)
        now, upcoming = today(), []
        for o in account_api().get_recurring_obligations():
            attrs = o.get("attributes", o)
            if not attrs.get("active", True) or not attrs.get("due_day"):
                continue
            day = int(attrs["due_day"])
            for month_offset in (0, 1):
                year, month = divmod(now.month - 1 + month_offset, 12)
                try:
                    due = date(now.year + year, month + 1, day)
                except ValueError:                         # día 31 en un mes de 30
                    continue
                if now <= due <= now + timedelta(days=int(dias)):
                    upcoming.append({"nombre": attrs.get("name"), "monto": attrs.get("amount"), "vence": due.isoformat()})
                    break
        return sorted(upcoming, key=lambda x: x["vence"])

    def ultimos_movimientos(cantidad: int = 10) -> list[dict]:
        """Los últimos movimientos del mes en curso (fecha, concepto, categoría, monto, tipo), máximo 50."""
        need(READ)
        m, y = periodo(None, None)
        api = account_api()
        names = category_names(api)
        rows = sorted(api.get_transactions(m, y), key=lambda t: str(t.get("attributes", t).get("date") or ""), reverse=True)
        return [{"fecha": a.get("date"), "concepto": a.get("concept"), "categoria": names.get(category_id(t)),
                 "monto": a.get("amount"), "tipo": a.get("transaction_type")}
                for t in rows[:max(1, min(int(cantidad), 50))] for a in [t.get("attributes", t)]]

    def registrar_gasto(monto: int, concepto: str, fecha: str | None = None, categoria_code: str | None = None) -> dict:
        """Registra un gasto confirmado (scope ascent:write). monto en pesos, concepto, fecha YYYY-MM-DD (hoy por
        defecto) y, si se conoce, el código de la categoría."""
        if in_demo():
            raise ToolError(DEMO_READ_ONLY)
        need(WRITE)
        if int(monto) <= 0:
            raise ToolError("el monto debe ser positivo")
        payload = {"date": fecha or today().isoformat(), "concept": concepto.strip()[:200], "amount": int(monto),
                   "transaction_type": "expense", "status": "confirmed", "source": "manual"}
        if categoria_code:
            payload["category_code"] = categoria_code
        created = account_api().create_transaction(payload)
        return {"registrado": bool(created.get("ok", True)), "id": created.get("id")}

    for tool in (resumen_mes, gasto_por_categoria, obligaciones_proximas, ultimos_movimientos, registrar_gasto):
        mcp.add_tool(tool)
    return mcp
