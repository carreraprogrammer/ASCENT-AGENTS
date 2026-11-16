"""ASC-3: modo backfill de Gmail. Solo correos de ejemplo y dobles; nada sale a la red."""
from datetime import date

import pytest

import agents.gmail_backfill as gb
import agents.gmail_push as gp
from scripts import gmail_backfill as cli
from tests.gmail_fakes import (CARD_PAYMENT, INBOUND, NEWSLETTER, PURCHASE, PURCHASE_ECHO, FakeWorld,
                               ScriptedProvider)

D1, D2, D3 = date(2026, 9, 10), date(2026, 9, 11), date(2026, 10, 2)
ACCOUNT = 7


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


def _deps(provider, send_summary=None) -> gb.BackfillDeps:
    """Las mismas dependencias que default_deps, con el LLM doble; Gmail y API pasan por httpx (falso)."""
    return gb.BackfillDeps(
        list_message_ids=lambda d: gb.list_message_ids_for_day("tok", d),
        fetch_message=lambda mid: gp._fetch_message("tok", mid),
        fetch_month_rows=lambda y, m: gb.fetch_month_rows(ACCOUNT, y, m),
        build_tool_map=lambda: gp._build_tool_map(ACCOUNT, notify=False),
        provider=provider,
        model="deepseek-chat",
        send_summary=send_summary,
    )


def _run(world, days, provider=None, **kw):
    provider = provider or ScriptedProvider()
    result = gb.run_backfill(ACCOUNT, days, _deps(provider, kw.pop("send_summary", None)), **kw)
    return result, provider


# ── Idempotencia ──────────────────────────────────────────────────────────────

def test_second_run_creates_nothing_and_calls_no_llm(world):
    _set_emails(world, {D1: [PURCHASE, INBOUND], D2: [NEWSLETTER]})
    first, _ = _run(world, [D1, D2])
    assert first.totals()["created"] == 2
    created = world.created_rows()
    assert sorted(c["metadata"]["source_event_id"] for c in created) == ["m-compra", "m-entrante"]
    assert all(c["source"] == "gmail" for c in created)

    world.post_calls.clear()
    second, provider2 = _run(world, [D1, D2], provider=ScriptedProvider())
    t = second.totals()
    assert t["created"] == 0
    assert t["already_registered"] == 2
    assert world.created_rows() == []
    # El boletín no quedó registrado, así que vuelve al LLM; los dos ya registrados no.
    assert len(provider2.calls) == 1
    assert "m-compra" not in provider2.calls[0]["initial_message"]
    assert "m-entrante" not in provider2.calls[0]["initial_message"]


def test_known_ids_come_from_the_api_rows_source_event_id(world):
    world.add_transaction(date_str=D1.isoformat(), amount=45000, sid="m-compra")
    _set_emails(world, {D1: [PURCHASE]})
    result, provider = _run(world, [D1])
    assert provider.calls == []
    assert result.totals()["already_registered"] == 1


# ── Deduplicación por día ─────────────────────────────────────────────────────

def test_day_context_lists_that_days_transactions_only(world):
    world.add_transaction(date_str=D1.isoformat(), amount=45000, source="telegram", concept="Tienda a mano")
    world.add_transaction(date_str=D2.isoformat(), amount=99000, source="manual", concept="Otro dia")
    _set_emails(world, {D1: [PURCHASE], D2: [INBOUND]})
    result, provider = _run(world, [D1, D2])
    msg_d1, msg_d2 = (c["initial_message"] for c in provider.calls)
    assert "TRANSACCIONES YA REGISTRADAS HOY" in msg_d1 and "$45000" in msg_d1 and "$99000" not in msg_d1
    assert "$99000" in msg_d2 and "$45000" not in msg_d2
    assert "Fecha: 2026-09-10" in msg_d1
    # El pago de D1 ya estaba registrado a mano: el agente no lo duplica.
    assert [c["metadata"]["source_event_id"] for c in world.created_rows()] == ["m-entrante"]
    assert result.months["2026-09"]["no_movement"] == 1


def test_same_payment_in_two_emails_of_a_day_is_created_once_across_batches(world, monkeypatch):
    monkeypatch.setattr(gb, "BATCH_SIZE", 1)  # cada correo en su lote: el contexto del 2.º trae lo creado
    _set_emails(world, {D1: [PURCHASE, PURCHASE_ECHO]})
    result, provider = _run(world, [D1])
    assert len(provider.calls) == 2
    assert "$45000" in provider.calls[1]["initial_message"].split("REGISTRADAS HOY", 1)[1]
    assert len(world.created_rows()) == 1
    assert result.totals()["created"] == 1


def test_same_amount_on_another_day_is_not_a_duplicate(world):
    world.add_transaction(date_str=D2.isoformat(), amount=45000, source="manual")
    _set_emails(world, {D1: [PURCHASE]})
    _run(world, [D1, D2])
    assert len(world.created_rows()) == 1


def test_days_are_processed_in_chronological_order_and_in_small_batches(world):
    many = [{**NEWSLETTER, "id": f"m-n{i}"} for i in range(12)]
    _set_emails(world, {D3: [PURCHASE], D1: many})
    _result, provider = _run(world, [D3, D1])
    sizes = [c["initial_message"].count("[ID: ") for c in provider.calls]
    assert sizes == [5, 5, 2, 1]
    assert "m-n0]" in provider.calls[0]["initial_message"]  # el más antiguo primero
    assert "m-compra" in provider.calls[-1]["initial_message"]


# ── Guardas y force pending ───────────────────────────────────────────────────

def test_guard_blocks_card_payment_before_llm(world):
    _set_emails(world, {D1: [CARD_PAYMENT]})
    result, provider = _run(world, [D1])
    assert provider.calls == []
    assert result.totals()["guard_email"] == 1
    assert world.created_rows() == []


def test_guard_in_create_tool_counts_blocked(world):
    _set_emails(world, {D1: [NEWSLETTER]})

    class Provider(ScriptedProvider):
        def run_agent(self, *, tool_map, **kw):
            self.calls.append(kw)
            return tool_map["create_transaction"]({
                "date": D1.isoformat(), "concept": "Pago Tarjeta de Crédito", "amount": 1,
                "transaction_type": "expense", "status": "confirmed",
                "metadata": {"source_event_id": NEWSLETTER["id"]}})

    result, _ = _run(world, [D1], provider=Provider())
    assert result.totals()["guard_tool"] == 1
    assert world.created_rows() == []


def test_force_pending_inbound_transfer(world):
    _set_emails(world, {D1: [INBOUND]})
    result, provider = _run(world, [D1])
    assert "Force pending: sí" in provider.calls[0]["initial_message"]
    (created,) = world.created_rows()
    assert created["status"] == "pending"
    t = result.totals()
    assert t["force_pending"] == 1 and t["pending"] == 1 and t["created"] == 1


def test_backfill_uses_realtime_prompt_and_tools(world):
    _set_emails(world, {D1: [PURCHASE]})
    _result, provider = _run(world, [D1])
    call = provider.calls[0]
    assert call["system_prompt"] is gp.SYSTEM_PROMPT
    assert call["tools"] is gp.TOOLS
    assert call["max_iterations"] == 10
    assert set(call["tool_map"]) == {"get_categories", "get_classification_hints", "create_transaction"}


def test_goal_and_fund_links_are_stripped(world):
    _set_emails(world, {D1: [INBOUND]})

    class Provider(ScriptedProvider):
        def run_agent(self, *, tool_map, **kw):
            self.calls.append(kw)
            return tool_map["create_transaction"]({
                "date": D1.isoformat(), "concept": "Retiro fondo", "amount": 80000, "transaction_type": "income",
                "status": "confirmed", "savings_goal_id": 5, "sinking_fund_id": 6,
                "metadata": {"source_event_id": INBOUND["id"]}})

    result, _ = _run(world, [D1], provider=Provider())
    (created,) = world.created_rows()
    assert "savings_goal_id" not in created and "sinking_fund_id" not in created
    assert result.totals()["goal_link_stripped"] == 2


# ── Dry-run ───────────────────────────────────────────────────────────────────

def test_dry_run_writes_nothing_but_counts(world):
    _set_emails(world, {D1: [PURCHASE, PURCHASE_ECHO, INBOUND, CARD_PAYMENT]})
    summaries = []
    result, provider = _run(world, [D1], dry_run=True, send_summary=summaries.append)
    assert world.post_calls == []
    assert summaries == []
    t = result.totals()
    assert t["created"] == 2 and t["pending"] == 1 and t["guard_email"] == 1
    assert t["financial"] == 2 and t["no_movement"] == 1  # el eco del mismo pago no se registra
    assert all("gmail.googleapis.com" not in u or "/messages" in u for u in world.gmail_calls())


def test_no_llm_mode_only_reads_and_estimates(world):
    _set_emails(world, {D1: [PURCHASE, INBOUND]})
    result, provider = _run(world, [D1], no_llm=True)
    assert provider.calls == [] and world.post_calls == []
    assert result.tokens_are_estimate and result.prompt_tokens > 0 and result.estimated_cost_usd > 0


# ── Notificaciones ────────────────────────────────────────────────────────────

def test_no_ui_event_per_movement_and_one_summary_at_the_end(world):
    _set_emails(world, {D1: [PURCHASE, INBOUND], D2: [PURCHASE_ECHO]})
    result, _ = _run(world, [D1, D2], send_summary=gb.default_send_summary(ACCOUNT))
    assert result.totals()["created"] == 3
    events = world.ui_events()
    assert len(events) == 1
    assert events[0]["event_type"] == "show_card"
    body = events[0]["payload"]["body"]
    assert "3 movimientos" in body and "45000" not in body and "45.000" not in body and "TIENDA" not in body


# ── Tope de costo ─────────────────────────────────────────────────────────────

def test_cost_cap_keeps_most_recent_days(world):
    days = {D1: [PURCHASE], D2: [INBOUND], D3: [{**NEWSLETTER, "id": "m-n1"}]}
    _set_emails(world, days)
    cost = {d: gb.estimate_day_cost_usd([{**e, "day": d.isoformat()} for e in es]) for d, es in days.items()}
    cap = cost[D3] + cost[D2] + cost[D1] / 2  # alcanza para D3 y D2, no para D1
    result, provider = _run(world, [D1, D2, D3], max_cost_usd=cap)
    assert result.cost_reached_date == D2
    assert result.months["2026-09"]["skipped_by_cost"] == 1
    processed = " ".join(c["initial_message"] for c in provider.calls)
    assert "m-compra" not in processed and "m-entrante" in processed and "m-n1" in processed
    assert provider.calls[0]["initial_message"].count("m-entrante")  # cronológico: D2 antes que D3


def test_cost_cap_hard_stop_with_real_usage(world):
    _set_emails(world, {D1: [PURCHASE], D2: [INBOUND]})
    expensive = ScriptedProvider(usage_per_call=(10_000_000, 0))  # ~2.8 USD por llamada
    result, provider = _run(world, [D1, D2], provider=expensive, max_cost_usd=1000.0)
    assert len(provider.calls) == 2 and result.stopped_by_cost_at is None
    world.post_calls.clear()
    world.transactions.clear()
    result, provider = _run(world, [D1, D2], provider=ScriptedProvider(usage_per_call=(10_000_000, 0)),
                            max_cost_usd=1.0)
    assert len(provider.calls) == 1
    assert result.stopped_by_cost_at == D2
    assert result.months["2026-09"]["skipped_by_cost"] == 1


def test_token_cost_uses_deepseek_prices():
    assert gb.token_cost_usd(1_000_000, 0) == pytest.approx(gb.DEEPSEEK_INPUT_USD_PER_M)
    assert gb.token_cost_usd(1_000_000, 1_000_000, cache_hit_tokens=1_000_000) == pytest.approx(
        gb.DEEPSEEK_INPUT_CACHE_HIT_USD_PER_M + gb.DEEPSEEK_OUTPUT_USD_PER_M)


# ── Exclusiones ───────────────────────────────────────────────────────────────

def test_excluded_dates_and_ids_are_skipped(world):
    _set_emails(world, {D1: [PURCHASE, INBOUND], D2: [PURCHASE_ECHO]})
    result, provider = _run(world, [D1, D2], excluded_dates={D2}, excluded_ids={"m-entrante"})
    t = result.totals()
    assert t["excluded"] == 2
    processed = " ".join(c["initial_message"] for c in provider.calls)
    assert "m-compra]" in processed and "m-entrante" not in processed and "m-compra-eco" not in processed
    fetched = [u for u in world.gmail_calls() if "/messages/" in u]
    assert not any(u.endswith("m-entrante") or u.endswith("m-compra-eco") for u in fetched)
    assert [c["metadata"]["source_event_id"] for c in world.created_rows()] == ["m-compra"]


def test_cli_parses_exclusions(tmp_path):
    f = tmp_path / "ids.txt"
    f.write_text("abc\n# comentario\n def # otro\n\n")
    assert cli.read_exclude_ids(str(f)) == {"abc", "def"}
    assert cli.parse_exclude_dates(["2026-09-01,2026-09-03..2026-09-04", "2026-10-01"]) == {
        date(2026, 9, 1), date(2026, 9, 3), date(2026, 9, 4), date(2026, 10, 1)}


# ── Gmail solo lectura y consulta por día ─────────────────────────────────────

def test_gmail_list_is_paged_dated_and_read_only(world):
    emails = [{**NEWSLETTER, "id": f"m-{i}"} for i in range(5)]
    _set_emails(world, {D1: emails})
    ids = gb.list_message_ids_for_day("tok", D1)
    assert ids == [e["id"] for e in emails]  # cronológico, sin duplicados, 3 páginas
    queries = [p["q"] for (u, p) in world.get_calls if u.endswith("/users/me/messages")]
    assert len(queries) == 3
    start = int(queries[0].split("after:")[1].split()[0])
    end = int(queries[0].split("before:")[1].split()[0])
    assert end - start == 86400 and "-in:sent" in queries[0]
    assert world.post_calls == []


# ── Periodo por defecto ───────────────────────────────────────────────────────

def test_last_gmail_date_ignores_today_and_other_sources(world):
    today = date(2026, 11, 15)
    world.add_transaction(date_str="2026-11-15", amount=1, sid="hoy")  # del push recién reconectado
    world.add_transaction(date_str="2026-08-20", amount=1, sid="a")
    world.add_transaction(date_str="2026-08-25", amount=1, source="manual")
    assert gb.last_gmail_transaction_date(ACCOUNT, today) == date(2026, 8, 20)


def test_default_period_starts_after_last_gmail_and_caps_at_120_days():
    today = date(2026, 11, 15)
    assert gb.default_period(date(2026, 8, 20), today) == (date(2026, 8, 21), today, False)
    since, until, capped = gb.default_period(date(2026, 1, 1), today)
    assert capped and (until - since).days + 1 == 120
    assert gb.default_period(None, today)[0] == since


# ── CLI: cuenta demo y salida solo agregada ──────────────────────────────────

class FakeApi:
    def __init__(self, demo="99"):
        self.demo = demo

    def get_demo_account_id(self):
        return self.demo


def test_cli_refuses_demo_account(capsys):
    calls = []
    code = cli.main(["--account-id", "99", "--since", "2026-09-10", "--until", "2026-09-11"],
                    api_factory=FakeApi, token_getter=lambda a: calls.append(a) or ("t", []),
                    deps_factory=lambda *a, **k: pytest.fail("no debe construir nada"))
    assert code == cli.EXIT_DEMO
    assert calls == []
    assert "demo" in capsys.readouterr().err


def test_cli_refuses_when_demo_cannot_be_resolved():
    class Broken:
        def get_demo_account_id(self):
            raise RuntimeError("sin red")

    code = cli.main(["--account-id", "7", "--since", "2026-09-10", "--until", "2026-09-11"], api_factory=Broken,
                    token_getter=lambda a: pytest.fail("no debe pedir token"))
    assert code == cli.EXIT_DEMO


def test_cli_without_gmail_token_exits_with_gmail_error():
    code = cli.main(["--account-id", "7", "--since", "2026-09-10", "--until", "2026-09-11"], api_factory=FakeApi,
                    token_getter=lambda a: (None, []))
    assert code == cli.EXIT_GMAIL


def test_cli_dry_run_prints_only_aggregates(world, capsys):
    world.add_transaction(date_str="2026-09-01", amount=1, sid="viejo")  # último gmail antes del periodo
    _set_emails(world, {D1: [PURCHASE, INBOUND, CARD_PAYMENT], D3: [PURCHASE_ECHO]})
    provider = ScriptedProvider()
    code = cli.main(["--account-id", "7", "--dry-run", "--until", "2026-10-03"], api_factory=FakeApi,
                    token_getter=lambda a: ("tok-secreto", []),
                    deps_factory=lambda a, t, extra_query="": _deps(provider), today=date(2026, 10, 3))
    out = capsys.readouterr().out
    assert code == 0
    assert world.post_calls == []
    assert "desde 2026-09-02" in out
    assert "2026-09" in out and "2026-10" in out and "TOTAL" in out and "costo LLM" in out
    for secret in ("tok-secreto", "45000", "45.000", "80000", "80.000", "TIENDA", "Banco", "Compra aprobada",
                   "alertas@", "m-compra", "PERSONA"):
        assert secret not in out
