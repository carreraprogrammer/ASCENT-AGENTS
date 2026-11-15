"""El consolidado diario de Ascent, al cerebro de The Cyborg Way (TCW-1 fase 4).

Cada mañana los agentes escriben una nota en projects/ascent/episodic/runs/YYYY-MM-DD-consolidado.md del cerebro,
con un cliente de máquina de Ascent (client_credentials, scope brain:write, solo el space ascent).

Solo conclusiones: totales del mes, desviaciones contra el presupuesto (en porcentaje), alertas y decisiones
sugeridas. Nunca movimientos, conceptos, saldos de cuenta ni números de cuenta: build() solo lee agregados, y
un test lo comprueba con movimientos y saldos marcados.

El envío por correo queda detrás de CONSOLIDADO_POR_CORREO (apagado). Hoy no existe un envío por correo del
consolidado: el flag deja constancia de la decisión y no activa nada.

Variables: BRAIN_ISSUER (https://thecyborgway.dev), BRAIN_CLIENT_ID, BRAIN_CLIENT_SECRET.
"""
import asyncio
import json
import logging
import os
from collections import defaultdict
from datetime import date

import httpx

from cromo.server import category_id, category_names, today
from cromo.tokens import BRAIN_ISSUER

log = logging.getLogger(__name__)
POR_CORREO = os.environ.get("CONSOLIDADO_POR_CORREO", "false").lower() == "true"   # apagado: TCW-1


def miles(amount) -> str:
    """Un total redondeado a miles de pesos, sin centavos: $1.234k."""
    return f"${round(int(amount or 0) / 1000):,}k".replace(",", ".")


def build(api, day: date | None = None) -> dict:
    """{"path", "frontmatter", "body"} de la nota del día. Solo agregados del mes en curso."""
    day = day or today()
    m, y = day.month, day.year
    summary = api.get_summary(m, y)
    balance = summary.get("balance") or {}
    runway = summary.get("cash_flow_runway") or {}
    spent = defaultdict(int)
    for txn in api.get_transactions(m, y):
        attrs = txn.get("attributes", txn)
        if attrs.get("transaction_type") == "expense" and attrs.get("status") == "confirmed":
            spent[category_id(txn)] += int(attrs.get("amount") or 0)
    names = category_names(api)
    desviaciones = []
    for budget in api.get_budgets(m, y):
        limit = int(budget.get("amount_limit") or 0)
        used = spent.get(str(budget.get("category_id")), 0)
        if limit > 0 and used > limit:
            desviaciones.append((round((used - limit) * 100 / limit), budget.get("category_name")
                                 or names.get(str(budget.get("category_id"))) or "una categoría"))
    desviaciones.sort(reverse=True)
    pendientes = len(api.get_pending_transactions())
    proximas = [o for o in api.get_recurring_obligations() if _vence_en(o, day, 3)]

    alertas = []
    if (runway.get("commitment_gap") or 0) < 0:
        alertas.append("El margen libre está en negativo: los compromisos del mes superan lo disponible.")
    if proximas:
        alertas.append(f"{len(proximas)} obligación(es) recurrente(s) vencen en los próximos 3 días.")
    if pendientes:
        alertas.append(f"{pendientes} movimiento(s) pendientes de confirmar.")

    decisiones = []
    for exceso, nombre in desviaciones[:3]:
        decisiones.append(f"Revisar {nombre}: va {exceso} % por encima de su presupuesto.")
    if (runway.get("commitment_gap") or 0) < 0:
        decisiones.append("Aplazar gastos no esenciales hasta el próximo ingreso.")
    if pendientes:
        decisiones.append("Confirmar o descartar los movimientos pendientes.")
    if not decisiones:
        decisiones.append("Sin cambios: el mes va dentro de lo previsto.")

    lines = [f"# Consolidado de Ascent — {day.isoformat()}", "",
             "Conclusiones del día, sin movimientos ni datos de cuentas.", "", "## Totales del mes", "",
             f"- Ingresos confirmados: {miles(balance.get('income_confirmed'))}",
             f"- Gastos confirmados: {miles(balance.get('expense_confirmed'))}",
             f"- Gastos pendientes: {miles(balance.get('expense_pending'))}", "", "## Desviaciones", ""]
    lines += [f"- {nombre}: +{exceso} % sobre su presupuesto" for exceso, nombre in desviaciones] or ["- Ninguna categoría pasa su presupuesto."]
    lines += ["", "## Alertas", ""] + ([f"- {a}" for a in alertas] or ["- Ninguna."])
    lines += ["", "## Decisiones sugeridas", ""] + [f"- {d}" for d in decisiones]
    meta = {"project": "ascent", "type": "run", "date": day.isoformat(), "source": "cromo:ascent"}
    return {"path": f"projects/ascent/episodic/runs/{day.isoformat()}-consolidado.md", "frontmatter": meta,
            "body": "\n".join(lines)}


def _vence_en(obligation: dict, day: date, dias: int) -> bool:
    attrs = obligation.get("attributes", obligation)
    if not attrs.get("active", True) or not attrs.get("due_day"):
        return False
    due_day = int(attrs["due_day"])
    return 0 <= due_day - day.day <= dias


def machine_token(issuer: str = BRAIN_ISSUER, http=None) -> str:
    """Un token del cerebro para el cliente de máquina de Ascent (client_credentials, brain:write)."""
    http = http or httpx
    answer = http.post(f"{issuer}/token", data={"grant_type": "client_credentials", "scope": "brain:write"},
                       auth=(os.environ["BRAIN_CLIENT_ID"], os.environ["BRAIN_CLIENT_SECRET"]), timeout=20)
    answer.raise_for_status()
    return answer.json()["access_token"]


async def write_note(note: dict, token: str, issuer: str = BRAIN_ISSUER) -> dict:
    """create_note en el MCP del cerebro, con el token de máquina. Nunca se registra el token."""
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    async with httpx2.AsyncClient(timeout=60, headers={"Authorization": f"Bearer {token}"}) as http:
        async with streamable_http_client(f"{issuer}/mcp", http_client=http) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                answer = await session.call_tool("create_note", note)
                text = answer.content[0].text if answer.content else ""
                return {"error": text} if answer.is_error else json.loads(text)


async def run(api) -> dict:
    """El trabajo diario del scheduler: arma el consolidado (en un hilo: la API de Rails es síncrona) y lo escribe
    en el cerebro."""
    if not os.environ.get("BRAIN_CLIENT_ID"):
        return {"skipped": "sin BRAIN_CLIENT_ID"}
    loop = asyncio.get_running_loop()
    note = await loop.run_in_executor(None, build, api)
    token = await loop.run_in_executor(None, machine_token)
    result = await write_note(note, token)
    log.info(json.dumps({"event": "consolidado", "path": note["path"], "ok": "error" not in result}))
    return result
