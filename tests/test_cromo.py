"""TCW-1 fase 4: Ascent como cromo. Un emisor simulado local (llave y JWKS de prueba) hace de cerebro.

Cubre: el manifiesto; el MCP rechaza todo token que no sea del cerebro para Ascent (el del propio cerebro
incluido: sin passthrough); los scopes de lectura y escritura; el consolidado solo lleva conclusiones."""
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from starlette.testclient import TestClient

from cromo import consolidado
from cromo.manifest import MANIFEST
from cromo.server import make_server
from cromo.tokens import BrainTokenVerifier, Keys

ISSUER = "https://brain.test"
RESOURCE = "https://ascent.test/mcp"
KEY = ec.generate_private_key(ec.SECP256R1())
OTHER = ec.generate_private_key(ec.SECP256R1())
JWKS = {"keys": [jwt.algorithms.ECAlgorithm.to_jwk(KEY.public_key(), as_dict=True) | {"kid": "k1", "alg": "ES256"}]}
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
SECRET = "CONCEPTO-SECRETO"


def token(aud=RESOURCE, scope="ascent:read", iss=ISSUER, exp=300, key=KEY, kid="k1") -> str:
    now = int(time.time())
    return jwt.encode({"iss": iss, "sub": "u_owner", "aud": aud, "scope": scope, "iat": now, "exp": now + exp,
                       "act": {"sub": ISSUER}}, key, algorithm="ES256", headers={"kid": kid})


class FakeApi:
    created = []

    def get_summary(self, m, y):
        return {"balance": {"income_confirmed": 5_000_000, "expense_confirmed": 3_200_000, "expense_pending": 100_000,
                            "net_balance": 987_654_321, "balance_confirmed": 123_456_789},
                "month_execution": {"income": {"remaining_expected_total": 1_000_000}},
                "cash_flow_runway": {"commitment_gap": -250_000}}

    def get_transactions(self, m, y):
        return [{"id": "1", "attributes": {"date": "2026-11-10", "concept": SECRET, "amount": 900_000,
                                           "transaction_type": "expense", "status": "confirmed"},
                 "relationships": {"category": {"data": {"id": "7"}}}},
                {"id": "2", "attributes": {"date": "2026-11-12", "concept": f"{SECRET}-2", "amount": 300_000,
                                           "transaction_type": "expense", "status": "confirmed"},
                 "relationships": {"category": {"data": {"id": "8"}}}}]

    def get_categories(self):
        return [{"id": "7", "attributes": {"name": "Mercado"}}, {"id": "8", "attributes": {"name": "Transporte"}}]

    def get_budgets(self, m, y):
        return [{"category_id": 7, "category_name": "Mercado", "amount_limit": 600_000}]

    def get_recurring_obligations(self):
        return [{"attributes": {"name": "Arriendo", "amount": 1_500_000, "due_day": 1, "active": True}}]

    def get_pending_transactions(self):
        return [{"id": "9"}]

    def create_transaction(self, payload):
        self.created.append(payload)
        return {"ok": True, "id": "99"}


@pytest.fixture()
def client():
    verifier = BrainTokenVerifier(Keys("https://brain.test/jwks", fetch=lambda url: JWKS), ISSUER, RESOURCE)
    app = make_server(FakeApi, verifier, ISSUER, RESOURCE).streamable_http_app(stateless_http=True, json_response=True,
                                                                              host="0.0.0.0")
    with TestClient(app, base_url="https://ascent.test") as c:
        yield c


def call(client, bearer, method="tools/list", params=None):
    return client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
                       headers=HEADERS | {"Authorization": f"Bearer {bearer}"})


def test_the_manifest_is_a_valid_cromo_manifest():
    assert MANIFEST["slug"] == "ascent" and MANIFEST["space"] == "ascent" and MANIFEST["status"] == "available"
    assert MANIFEST["resource"].startswith("https://") and MANIFEST["mcp_url"] == MANIFEST["resource"]
    assert all(s.startswith("ascent:") for s in MANIFEST["scopes"])
    assert MANIFEST["name"]["es"] and MANIFEST["description"]["en"]


def test_without_a_token_the_mcp_says_who_to_ask(client):
    answer = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=HEADERS)
    assert answer.status_code == 401 and "resource_metadata=" in answer.headers["www-authenticate"]
    prm = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert prm["resource"] == RESOURCE and prm["authorization_servers"] == [ISSUER]


def test_a_brain_token_for_ascent_lists_and_runs_the_tools(client):
    names = {t["name"] for t in call(client, token()).json()["result"]["tools"]}
    assert names == {"resumen_mes", "gasto_por_categoria", "obligaciones_proximas", "ultimos_movimientos", "registrar_gasto"}
    result = call(client, token(), "tools/call", {"name": "gasto_por_categoria", "arguments": {}}).json()["result"]
    assert not result.get("isError") and "Mercado" in str(result) and "900000" in str(result)


@pytest.mark.parametrize("bad", [
    token(aud=f"{ISSUER}/mcp"),               # the brain's own token: no passthrough
    token(iss="https://evil.test"),
    token(exp=-10),
    token(key=OTHER),                         # signed by someone else with the same kid
    token(kid="unknown"),
    jwt.encode({"iss": ISSUER, "sub": "x", "aud": RESOURCE, "iat": 0, "exp": 9999999999}, "s3cret", algorithm="HS256"),
    "not-a-jwt",
])
def test_everything_else_is_refused(client, bad):
    assert call(client, bad).status_code == 401


def test_writing_needs_ascent_write(client):
    FakeApi.created.clear()
    args = {"name": "registrar_gasto", "arguments": {"monto": 12000, "concepto": "Café"}}
    refused = call(client, token(scope="ascent:read"), "tools/call", args).json()["result"]
    assert refused.get("isError") and "ascent:write" in str(refused) and not FakeApi.created
    done = call(client, token(scope="ascent:read ascent:write"), "tools/call", args).json()["result"]
    assert not done.get("isError") and FakeApi.created[0]["amount"] == 12000
    assert FakeApi.created[0]["transaction_type"] == "expense"


def test_the_daily_summary_carries_conclusions_only():
    from datetime import date
    note = consolidado.build(FakeApi(), date(2026, 11, 15))
    assert note["path"] == "projects/ascent/episodic/runs/2026-11-15-consolidado.md"
    assert note["frontmatter"] == {"project": "ascent", "type": "run", "date": "2026-11-15", "source": "cromo:ascent"}
    body = note["body"]
    assert "## Totales del mes" in body and "## Desviaciones" in body and "## Alertas" in body
    assert "## Decisiones sugeridas" in body and "Mercado: +50 %" in body and "margen libre está en negativo" in body
    assert SECRET not in body                                 # no movement, no concept
    assert "987" not in body and "123.457" not in body and "123456789" not in body   # no account balance
    assert "$5.000k" in body                                  # totals, rounded to thousands


def test_the_summary_goes_to_the_brain_with_the_machine_client(monkeypatch):
    import asyncio
    monkeypatch.setenv("BRAIN_CLIENT_ID", "ascent-agents")
    monkeypatch.setenv("BRAIN_CLIENT_SECRET", "s")
    sent = {}
    monkeypatch.setattr(consolidado, "machine_token", lambda: "machine-token")

    async def fake_write(note, token_value):
        sent.update(note=note, token=token_value)
        return {"path": note["path"], "commit": "abc"}
    monkeypatch.setattr(consolidado, "write_note", fake_write)
    result = asyncio.run(consolidado.run(FakeApi()))
    assert result["commit"] == "abc" and sent["token"] == "machine-token"
    assert sent["note"]["frontmatter"]["source"] == "cromo:ascent"


def test_email_stays_off():
    assert consolidado.POR_CORREO is False
