"""ASC-3: modos de la oficina (sin LLM) del backfill de Gmail. Correos de ejemplo y dobles; nada sale a la red."""
import io
import json
from datetime import date

import pytest

import agents.gmail_backfill as gb
import agents.gmail_office as go
import agents.gmail_push as gp
from scripts import gmail_backfill as cli
from tests.gmail_fakes import (CARD_PAYMENT, INBOUND, NEWSLETTER, PURCHASE, PURCHASE_ECHO, FakeWorld,
                               ScriptedProvider)

D1, D2 = date(2026, 9, 17), date(2026, 9, 18)
ACCOUNT = "7"
NAMED_ADDR = {  # nombre visible que trae una dirección completa
    "id": "m-nombre-con-correo",
    "from": '"notificaciones@banco.test" <notificaciones@banco.test>',
    "subject": "Aviso " + "x" * 200,
    "body": "Tu clave fue cambiada.",
}


@pytest.fixture
def world(monkeypatch):
    w = FakeWorld()
    monkeypatch.setattr(gp.httpx, "get", w.get)
    monkeypatch.setattr(gp.httpx, "post", w.post)
    monkeypatch.setattr(gp.httpx, "patch", w.forbid)
    monkeypatch.setattr(gp.httpx, "put", w.forbid)
    monkeypatch.setattr(gp.httpx, "delete", w.forbid)
    return w


def _set_emails(world: FakeWorld, by_day: dict) -> None:
    world.emails_by_day = by_day
    for emails in by_day.values():
        for e in emails:
            world.add_message(e)


class FakeApi:
    def get_demo_account_id(self):
        return "99"


def _cli(args, capsys, stdin=None, token="tok-secreto"):
    code = cli.main(args, api_factory=FakeApi, token_getter=lambda a: (token, []),
                    stdin=io.StringIO(json.dumps(stdin)) if stdin is not None else None)
    out = capsys.readouterr()
    assert out.err == ""  # todo va por stdout
    lines = [json.loads(line) for line in out.out.splitlines() if line.strip()]
    return code, lines, out.out


def _apply(decisions, capsys, *extra, account=ACCOUNT):
    code, lines, raw = _cli(["--apply-decisions", "--account-id", account, *extra], capsys, stdin=decisions)
    assert code == 0, raw
    assert len(lines) == 1
    return lines[0]["apply"], raw


def _create(email, day=D1, **txn):
    t = {"date": day.isoformat(), "concept": "Compra en comercio", "amount": 45000,
         "transaction_type": "expense", "status": "confirmed", "subcategory_code": "supermercado"}
    t.update(txn)
    return {"email_id": email["id"], "day": day.isoformat(), "decision": "create", "transaction": t}


# ── Modo 1: encabezados ───────────────────────────────────────────────────────

def test_headers_states_and_summary(world, capsys):
    world.add_transaction(date_str="2026-09-17", amount=80000, ttype="income", sid=INBOUND["id"])
    _set_emails(world, {D1: [PURCHASE, INBOUND, CARD_PAYMENT, NAMED_ADDR], D2: [NEWSLETTER]})
    world.messages.pop(NEWSLETTER["id"])  # Gmail no la devuelve: ilegible
    code, lines, raw = _cli(["--extract-headers", "--account-id", ACCOUNT, "--since", "2026-09-17",
                             "--until", "2026-09-18"], capsys)
    assert code == 0
    by_id = {line["id"]: line for line in lines if "id" in line}
    assert by_id[PURCHASE["id"]]["state"] == "new"
    assert by_id[PURCHASE["id"]]["day"] == "2026-09-17"
    assert by_id[INBOUND["id"]]["state"] == "known"
    assert by_id[CARD_PAYMENT["id"]]["state"] == "guard"
    assert by_id[CARD_PAYMENT["id"]]["reason"]
    assert by_id[NEWSLETTER["id"]]["state"] == "unreadable"
    assert len(by_id[NAMED_ADDR["id"]]["subject"]) == 120
    assert lines[-1] == {"summary": {"2026-09": {"new": 2, "known": 1, "guard": 1, "unreadable": 1}}}
    # Sin parte local en el remitente
    assert by_id[PURCHASE["id"]]["from"] == "Banco Ficticio @banco.test"
    assert by_id[NAMED_ADDR["id"]]["from"] == "@banco.test"
    for local in ("alertas@", "notificaciones@", "no-reply@", "promo@"):
        assert local not in raw
    assert world.post_calls == []


def test_headers_only_new(world, capsys):
    _set_emails(world, {D1: [PURCHASE, CARD_PAYMENT]})
    _code, lines, _raw = _cli(["--extract-headers", "--only-new", "--account-id", ACCOUNT,
                               "--since", "2026-09-17", "--until", "2026-09-17"], capsys)
    assert [line.get("id") for line in lines[:-1]] == [PURCHASE["id"]]
    assert lines[-1]["summary"]["2026-09"]["guard"] == 1


@pytest.mark.parametrize("raw,label", [
    ("Banco <alertas@banco.test>", "Banco @banco.test"),
    ("alertas@banco.test", "@banco.test"),
    ("Pedro (pedro@x.test) <pedro@x.test>", "Pedro (@x.test) @x.test"),
    ("", ""),
])
def test_sender_label_never_has_local_part(raw, label):
    assert go.sender_label(raw) == label


# ── Modo 2: cuerpos, contexto del día, categorías y hints ────────────────────

def test_bodies_give_the_same_text_as_the_llm_path(world, capsys):
    _set_emails(world, {D1: [PURCHASE, INBOUND]})
    code, lines, raw = _cli(["--extract-bodies", "--account-id", ACCOUNT,
                             "--ids", f"{PURCHASE['id']},{INBOUND['id']},no-existe"], capsys)
    assert code == 0
    by_id = {line["id"]: line for line in lines}
    llm_email = gp._fetch_message("tok", PURCHASE["id"])
    assert by_id[PURCHASE["id"]]["text"] == llm_email["body"]
    assert by_id[PURCHASE["id"]]["text"] in gp.build_initial_message([llm_email], [])
    assert by_id[PURCHASE["id"]]["day"] == "2026-09-17"
    assert by_id[PURCHASE["id"]]["force_pending"] is False
    assert by_id[INBOUND["id"]]["force_pending"] is True
    assert by_id["no-existe"] == {"id": "no-existe", "state": "unreadable"}
    assert "alertas@" not in raw and world.post_calls == []


def test_bodies_truncate_and_cap_ids(world, capsys):
    _set_emails(world, {D1: [PURCHASE]})
    _code, lines, _raw = _cli(["--extract-bodies", "--account-id", ACCOUNT, "--ids", PURCHASE["id"],
                               "--max-chars", "10"], capsys)
    assert lines[0]["text"] == gp._fetch_message("tok", PURCHASE["id"])["body"][:10]
    code, lines, _raw = _cli(["--extract-bodies", "--account-id", ACCOUNT,
                              "--ids", ",".join(f"i{n}" for n in range(26))], capsys)
    assert code == cli.EXIT_BAD_ARGS and "error" in lines[0]


def test_day_context_and_categories(world, capsys):
    world.add_transaction(date_str="2026-09-17", amount=45000, sid="otro", concept="Compra previa")
    world.add_transaction(date_str="2026-09-18", amount=1, sid="otro-dia")
    code, lines, _raw = _cli(["--extract-bodies", "--account-id", ACCOUNT, "--day-context", "2026-09-17",
                              "--categories"], capsys, token=None)  # no necesita Gmail
    assert code == 0
    assert lines[0] == {"day": "2026-09-17", "registered": [
        {"concept": "Compra previa", "amount": 45000, "transaction_type": "expense", "status": "confirmed",
         "source": "gmail"}]}
    assert lines[1]["categories"]["ok"] is True
    assert lines[1]["categories"]["categories"][0]["code"] == "mercado"


def test_hints_mode(world, capsys):
    world.hints["BOLD CAMILO"] = {"merchant": "BOLD CAMILO", "samples": 3, "candidates": [
        {"subcategory_code": "almuerzos", "count": 3, "share": 1.0}],
        "dominant": {"subcategory_code": "almuerzos", "count": 3, "share": 1.0}}
    code, lines, _raw = _cli(["--hints", "--account-id", ACCOUNT, "--merchants", "BOLD CAMILO|OTRO"], capsys,
                             token=None)
    assert code == 0
    assert [line["merchant"] for line in lines] == ["BOLD CAMILO", "OTRO"]
    assert lines[0]["hints"]["dominant"]["subcategory_code"] == "almuerzos"
    assert lines[1]["hints"]["samples"] == 0


# ── Modo 3: aplicar decisiones ────────────────────────────────────────────────

def test_apply_creates_through_the_tool_with_source_event_id(world, capsys):
    _set_emails(world, {D1: [PURCHASE]})
    stats, _raw = _apply([_create(PURCHASE)], capsys)
    assert stats["created"] == 1
    [row] = world.created_rows()
    assert row["source"] == "gmail"
    assert row["metadata"]["source_event_id"] == PURCHASE["id"]
    assert row["metadata"]["subject"] == PURCHASE["subject"]
    assert row["payment_source"] == "debit"  # normalizado como en el camino del LLM
    assert world.ui_events() == []  # notify=False: sin eventos de UI por movimiento


def test_apply_overrides_source_event_id_and_source(world, capsys):
    _set_emails(world, {D1: [PURCHASE]})
    d = _create(PURCHASE, source="gmail", metadata={"source_event_id": "otro", "raw_text": "texto"})
    stats, _raw = _apply([d], capsys)
    assert stats["created"] == 1
    assert world.created_rows()[0]["metadata"] == {"source_event_id": PURCHASE["id"], "raw_text": "texto",
                                                   "subject": PURCHASE["subject"]}


def test_apply_guards_block_email_and_tool(world, capsys):
    _set_emails(world, {D1: [CARD_PAYMENT, PURCHASE]})
    stats, _raw = _apply([
        _create(CARD_PAYMENT, amount=300000),                     # guarda del correo
        _create(PURCHASE, concept="Pago tarjeta de crédito"),     # guarda de create_transaction
    ], capsys)
    assert stats["guard_email"] == 1 and stats["guard_tool"] == 1
    assert stats["created"] == 0 and world.created_rows() == []


def test_apply_force_pending(world, capsys):
    _set_emails(world, {D1: [INBOUND]})
    stats, _raw = _apply([_create(INBOUND, amount=80000, transaction_type="income", status="confirmed")], capsys)
    assert stats["created"] == 1 and stats["pending"] == 1 and stats["force_pending"] == 1
    assert world.created_rows()[0]["status"] == "pending"


def test_apply_twice_creates_nothing_the_second_time(world, capsys):
    _set_emails(world, {D1: [PURCHASE, INBOUND]})
    decisions = [_create(PURCHASE), _create(INBOUND, amount=80000, transaction_type="income")]
    first, _raw = _apply(decisions, capsys)
    assert first["created"] == 2
    second, _raw = _apply(decisions, capsys)
    assert second["created"] == 0 and second["duplicate_id"] == 2
    assert len(world.created_rows()) == 2


def test_apply_same_email_twice_in_one_input(world, capsys):
    _set_emails(world, {D1: [PURCHASE]})
    stats, _raw = _apply([_create(PURCHASE), _create(PURCHASE, amount=1)], capsys)
    assert stats["created"] == 1 and stats["duplicate_id"] == 1


def test_apply_day_context_dedup(world, capsys):
    world.add_transaction(date_str="2026-09-18", amount=45000, sid="ya-estaba")
    _set_emails(world, {D1: [PURCHASE, PURCHASE_ECHO], D2: [INBOUND]})
    stats, _raw = _apply([
        _create(PURCHASE_ECHO),                 # el mismo pago en dos correos del mismo día
        _create(PURCHASE),
        _create(INBOUND, day=D2, amount=45000, transaction_type="income"),  # otro día ya tiene 45000 pero de gasto: el ingreso no es duplicado
    ], capsys)
    assert stats["created"] == 2 and stats["duplicate_day"] == 1
    # La oficina puede afirmar que son movimientos distintos con el mismo monto
    _set_emails(world, {D2: [PURCHASE_ECHO, NEWSLETTER]})
    world.add_message(NEWSLETTER)
    d = _create(NEWSLETTER, day=D2)
    d["not_duplicate"] = True
    stats, _raw = _apply([d], capsys)
    assert stats["created"] == 1 and stats["duplicate_day"] == 0


def test_apply_hints_paths(world, capsys):
    world.hints["DOMINANTE"] = {"samples": 4, "candidates": [{"subcategory_code": "almuerzos"}],
                                "dominant": {"subcategory_code": "almuerzos"}}
    world.hints["AMBIGUO"] = {"samples": 4, "dominant": None, "candidates": [
        {"subcategory_code": "cafe"}, {"subcategory_code": "almuerzos"}, {"subcategory_code": "mercado"}]}
    e1, e2, e3 = ({**PURCHASE, "id": f"m-{n}"} for n in (1, 2, 3))
    _set_emails(world, {D1: [e1], D2: [e2, e3]})
    decisions = []
    for email, merchant, day, amount in ((e1, "DOMINANTE", D1, 1000), (e2, "AMBIGUO", D2, 2000),
                                         (e3, "NUEVO", D2, 3000)):
        d = _create(email, day=day, amount=amount, status="pending")
        del d["transaction"]["subcategory_code"]
        d["hints_for"] = merchant
        decisions.append(d)
    stats, _raw = _apply(decisions, capsys)
    assert (stats["hints_dominant"], stats["hints_ambiguous"], stats["hints_undetermined"]) == (1, 1, 1)
    rows = {r["metadata"]["source_event_id"]: r for r in world.created_rows()}
    assert rows["m-1"]["subcategory_code"] == "almuerzos" and rows["m-1"]["status"] == "confirmed"
    assert rows["m-1"]["metadata"]["classification_source"] == "pattern"
    assert rows["m-2"]["subcategory_code"] == "cafe" and rows["m-2"]["status"] == "pending"
    assert rows["m-2"]["metadata"]["suggested_subcategories"] == ["cafe", "almuerzos"]
    assert rows["m-2"]["metadata"]["classification_source"] == "agent"
    assert "subcategory_code" not in rows["m-3"] or rows["m-3"]["subcategory_code"] is None
    assert rows["m-3"]["status"] == "pending" and rows["m-3"]["metadata"]["classification_source"] == "agent"
    hint_calls = [p["merchant"] for (u, p) in world.get_calls if u.endswith("/classification_hints")]
    assert hint_calls == ["DOMINANTE", "AMBIGUO", "NUEVO"]


def test_apply_hints_not_called_when_subcategory_given(world, capsys):
    _set_emails(world, {D1: [PURCHASE]})
    d = _create(PURCHASE)
    d["hints_for"] = "TIENDA"
    _apply([d], capsys)
    assert not [u for (u, _p) in world.get_calls if u.endswith("/classification_hints")]


def test_apply_dry_run_writes_nothing(world, capsys):
    _set_emails(world, {D1: [PURCHASE, INBOUND, CARD_PAYMENT]})
    stats, _raw = _apply([_create(PURCHASE), _create(INBOUND, amount=80000, transaction_type="income"),
                          _create(CARD_PAYMENT, amount=300000)], capsys, "--dry-run")
    assert stats["dry_run"] is True
    assert stats["created"] == 2 and stats["pending"] == 1 and stats["guard_email"] == 1
    assert world.post_calls == []


def test_apply_skips_invalid_and_goal_links(world, capsys):
    _set_emails(world, {D1: [PURCHASE]})
    stats, _raw = _apply([
        {"email_id": "a", "day": "2026-09-17", "decision": "skip", "reason": "non_financial"},
        {"email_id": "b", "day": "2026-09-17", "decision": "skip", "reason": "transfer_own"},
        {"email_id": "c", "day": "2026-09-17", "decision": "skip", "reason": "inventada"},
        {"email_id": "d", "day": "ayer", "decision": "create", "transaction": {}},
        _create(PURCHASE, amount="45000"),
        _create(PURCHASE, category_id=3),
        _create(PURCHASE, savings_goal_id=5, sinking_fund_id=6),
    ], capsys)
    assert stats["skipped_non_financial"] == 1 and stats["skipped_transfer_own"] == 1
    assert stats["invalid"] == 4 and stats["decisions"] == 7
    assert stats["created"] == 1 and stats["goal_link_stripped"] == 2
    row = world.created_rows()[0]
    assert "savings_goal_id" not in row and "sinking_fund_id" not in row


def test_apply_refuses_demo_account(world, capsys):
    _set_emails(world, {D1: [PURCHASE]})
    code, lines, _raw = _cli(["--apply-decisions", "--account-id", "99"], capsys, stdin=[_create(PURCHASE)])
    assert code == cli.EXIT_DEMO and "error" in lines[0]
    assert world.post_calls == [] and world.gmail_calls() == []


def test_apply_output_has_no_concept_amount_or_email_content(world, capsys):
    _set_emails(world, {D1: [PURCHASE, INBOUND, CARD_PAYMENT]})
    decisions = [_create(PURCHASE, concept="CONCEPTO SECRETO", amount=45123),
                 _create(INBOUND, concept="OTRO SECRETO", amount=80321, transaction_type="income"),
                 _create(CARD_PAYMENT, amount=300777)]
    stats, raw = _apply(decisions, capsys)
    assert stats["created"] == 2
    for secret in ("SECRETO", "45123", "45.123", "80321", "300777", "TIENDA", "Banco", "Compra aprobada",
                   "alertas@", "m-compra", "m-entrante", "tok-secreto", "PERSONA"):
        assert secret not in raw
    assert set(stats) == set(go.APPLY_KEYS) | {"dry_run", "goal_linked_by_api"}


def test_apply_reports_goal_links_made_by_the_api(world, capsys, monkeypatch):
    _set_emails(world, {D1: [PURCHASE]})
    real_post = world.post

    def post(url, **kw):
        res = real_post(url, **kw)
        if url.endswith("/api/v1/transactions") and res.status_code == 201:
            world.transactions[-1]["attributes"]["savings_goal_id"] = 1
        return res

    monkeypatch.setattr(gp.httpx, "post", post)
    stats, _raw = _apply([_create(PURCHASE)], capsys)
    assert stats["goal_linked_by_api"] == 1


def test_apply_rejects_non_list_stdin(world, capsys):
    code, lines, _raw = _cli(["--apply-decisions", "--account-id", ACCOUNT], capsys, stdin={"a": 1})
    assert code == cli.EXIT_BAD_ARGS and "error" in lines[0]


def test_office_modes_do_not_change_the_llm_backfill(world):
    """La envoltura compartida (guarded_create) deja el backfill con LLM igual."""
    _set_emails(world, {D1: [PURCHASE, PURCHASE_ECHO, INBOUND]})
    deps = gb.BackfillDeps(
        list_message_ids=lambda d: gb.list_message_ids_for_day("tok", d),
        fetch_message=lambda mid: gp._fetch_message("tok", mid),
        fetch_month_rows=lambda y, m: gb.fetch_month_rows(int(ACCOUNT), y, m),
        build_tool_map=lambda: gp._build_tool_map(int(ACCOUNT), notify=False),
        provider=ScriptedProvider(), model="deepseek-chat")
    result = gb.run_backfill(int(ACCOUNT), [D1], deps)
    t = result.totals()
    assert t["created"] == 2 and t["pending"] == 1 and world.ui_events() == []
