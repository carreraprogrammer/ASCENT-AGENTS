"""ASC-4 fase 6: el cromo en modo demo.

Con el claim demo: true del cerebro, cada herramienta ascent.* lee la cuenta demo (el id lo da la API) y nunca la
del dueño; las escrituras se rechazan; sin el claim todo sigue como antes. El reset nocturno está programado y el
consolidado nunca sale de la cuenta demo. Todo con dobles: ni datos ni tokens reales."""
import asyncio
import time

import jwt
import pytest
from starlette.testclient import TestClient

from cromo import consolidado
from cromo.manifest import MANIFEST
from cromo.server import DEMO_READ_ONLY, DEMO_UNAVAILABLE, DemoAccount, make_server
from cromo.tokens import BrainTokenVerifier, Keys
from tests.test_cromo import HEADERS, ISSUER, JWKS, KEY, RESOURCE, FakeApi

DEMO_ID, OWNER_ID = "demo-7", "owner-1"
READ_TOOLS = ("resumen_mes", "gasto_por_categoria", "obligaciones_proximas", "ultimos_movimientos")


def brain_token(scope="ascent:read ascent:write", **extra) -> str:
    now = int(time.time())
    return jwt.encode({"iss": ISSUER, "sub": "u_owner", "aud": RESOURCE, "scope": scope, "iat": now, "exp": now + 300,
                       "act": {"sub": ISSUER}} | extra, KEY, algorithm="ES256", headers={"kid": "k1"})


class AccountApi(FakeApi):
    """El doble del RailsHttpAdapter: recuerda con qué cuenta se creó cada instancia."""
    accounts: list = []

    def __init__(self, account_id=None):
        self.account_id = str(account_id) if account_id else OWNER_ID   # como RailsHttpAdapter: sin id, el dueño
        AccountApi.accounts.append(self.account_id)


def make_client(resolve=lambda: DEMO_ID):
    AccountApi.accounts, AccountApi.created = [], []
    verifier = BrainTokenVerifier(Keys("https://brain.test/jwks", fetch=lambda url: JWKS), ISSUER, RESOURCE)
    server = make_server(AccountApi, verifier, ISSUER, RESOURCE, demo_account=DemoAccount(resolve))
    return TestClient(server.streamable_http_app(stateless_http=True, json_response=True, host="0.0.0.0"),
                      base_url="https://ascent.test")


def call_tool(client, bearer, name, arguments=None) -> dict:
    answer = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                       "params": {"name": name, "arguments": arguments or {}}},
                         headers=HEADERS | {"Authorization": f"Bearer {bearer}"})
    assert answer.status_code == 200, answer.text
    return answer.json()["result"]


def test_the_demo_claim_selects_the_demo_account_and_never_the_owners():
    with make_client() as client:
        for name in READ_TOOLS:
            assert not call_tool(client, brain_token(demo=True), name).get("isError"), name
    assert AccountApi.accounts and set(AccountApi.accounts) == {DEMO_ID}


def test_without_the_claim_the_owner_account_is_used_as_today():
    with make_client() as client:
        for name in READ_TOOLS:
            assert not call_tool(client, brain_token(), name).get("isError"), name
        assert not call_tool(client, brain_token(demo="true"), "resumen_mes").get("isError")   # only a real boolean
    assert set(AccountApi.accounts) == {OWNER_ID}


def test_writes_are_refused_in_demo():
    args = {"monto": 12000, "concepto": "Café"}
    with make_client() as client:
        refused = call_tool(client, brain_token(demo=True), "registrar_gasto", args)
        assert refused.get("isError") and DEMO_READ_ONLY in str(refused)
        assert not AccountApi.created and not AccountApi.accounts
        done = call_tool(client, brain_token(), "registrar_gasto", args)            # the owner still writes
        assert not done.get("isError") and AccountApi.created[0]["amount"] == 12000
    assert AccountApi.accounts == [OWNER_ID]


def test_an_unresolvable_demo_account_fails_instead_of_reading_the_owners():
    def down():
        raise RuntimeError("api down")
    for resolve in (down, lambda: "", lambda: None):
        with make_client(resolve) as client:
            result = call_tool(client, brain_token(demo=True), "resumen_mes")
            assert result.get("isError") and DEMO_UNAVAILABLE in str(result)
        assert AccountApi.accounts == []


def test_the_demo_account_id_is_cached_briefly_and_forgotten_after_a_reset():
    calls = []
    demo = DemoAccount(lambda: calls.append(1) or DEMO_ID, ttl=60)
    assert demo.get() == DEMO_ID and demo.get() == DEMO_ID and len(calls) == 1
    demo.forget()
    assert demo.get() == DEMO_ID and len(calls) == 2
    demo.read_at -= 61
    assert demo.get() == DEMO_ID and len(calls) == 3


def test_the_verified_token_exposes_the_demo_flag():
    verifier = BrainTokenVerifier(Keys("https://brain.test/jwks", fetch=lambda url: JWKS), ISSUER, RESOURCE)
    assert asyncio.run(verifier.verify_token(brain_token(demo=True))).demo is True
    assert asyncio.run(verifier.verify_token(brain_token())).demo is False
    assert asyncio.run(verifier.verify_token(brain_token(demo="true"))).demo is False


def test_the_manifest_documents_the_demo_claim_as_the_brain_validates_it():
    demo = MANIFEST["demo"]
    assert demo["claim"] == "demo"                         # the brain refuses any other claim name (ADR-0015)
    assert demo["effect"]["es"] and demo["effect"]["en"] and set(demo) == {"claim", "effect"}


def test_the_nightly_demo_reset_is_registered_and_calls_the_api(monkeypatch):
    import scheduler
    from adapters import rails_http
    from apscheduler.triggers.cron import CronTrigger
    from cromo import server
    job = scheduler._make_scheduler().get_job("demo_reset")
    assert job is not None and job.func is scheduler._run_demo_reset and isinstance(job.trigger, CronTrigger)
    assert str(job.trigger.fields[job.trigger.FIELD_NAMES.index("hour")]) == "5"
    resets = []
    monkeypatch.setattr(rails_http.RailsHttpAdapter, "reset_demo", lambda self: resets.append(1) or {"ok": True})
    server.DEMO_ACCOUNT.value, server.DEMO_ACCOUNT.read_at = DEMO_ID, time.time()
    asyncio.run(scheduler._run_demo_reset())
    assert resets == [1] and server.DEMO_ACCOUNT.value == ""


def test_the_consolidado_uses_the_owner_account_never_the_demo(monkeypatch):
    import scheduler
    from adapters import rails_http
    from cromo import server
    monkeypatch.setenv("BRAIN_CLIENT_ID", "ascent-agents")
    monkeypatch.setattr(rails_http, "RailsHttpAdapter", AccountApi)
    monkeypatch.setattr(server.DEMO_ACCOUNT, "get", lambda: DEMO_ID)
    monkeypatch.setattr(consolidado, "machine_token", lambda: "machine-token")
    sent = []

    async def fake_write(note, token_value):
        sent.append(note)
        return {"path": note["path"], "commit": "abc"}
    monkeypatch.setattr(consolidado, "write_note", fake_write)
    AccountApi.accounts = []
    asyncio.run(scheduler._run_consolidado())
    assert AccountApi.accounts == [OWNER_ID] and DEMO_ID not in AccountApi.accounts and len(sent) == 1

    sent.clear()                                            # an api pointed at the demo account sends nothing
    assert asyncio.run(consolidado.run(AccountApi(DEMO_ID), demo_account_id=DEMO_ID)) == {"skipped": "cuenta demo"}
    assert sent == []
