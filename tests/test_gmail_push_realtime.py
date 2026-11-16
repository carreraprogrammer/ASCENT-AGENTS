"""ASC-3: el modo en tiempo real de gmail_push no cambia con la refactorización para el backfill.

Fija lo que el agente le manda al LLM (prompt, herramientas, mensaje inicial con el contexto de hoy),
lo que filtra antes (guardas, force pending) y lo que hace al crear (POST + eventos de UI)."""
from datetime import datetime

import pytest

import agents.gmail_push as gp
from tests.gmail_fakes import (CARD_PAYMENT, COLOMBIA_TZ, INBOUND, NEWSLETTER, PURCHASE, FakeWorld,
                               ScriptedProvider)


@pytest.fixture
def world(monkeypatch):
    w = FakeWorld()
    monkeypatch.setattr(gp.httpx, "get", w.get)
    monkeypatch.setattr(gp.httpx, "post", w.post)
    return w


def _today() -> str:
    return datetime.now(COLOMBIA_TZ).strftime("%Y-%m-%d")


def _expected_initial_message(emails: list[dict], today_rows: list[str]) -> str:
    # Formato literal del modo en tiempo real antes de ASC-3.
    emails_text = "\n\n---\n\n".join(
        f"[ID: {e['id']}]\nDe: {e['from']}\nAsunto: {e['subject']}\n"
        f"Force pending: {'sí' if e.get('force_pending') else 'no'}\n\n{e['body']}"
        for e in emails
    )
    msg = f"Procesa estos {len(emails)} correo(s) de Gmail:\n\n{emails_text}"
    if today_rows:
        msg += (
            "\n\n═══ TRANSACCIONES YA REGISTRADAS HOY (no las dupliques) ═══\n"
            + "\n".join(today_rows) + "\n\n"
            "Un mismo pago suele llegar en DOS correos distintos (ej: notificación del banco + "
            "confirmación del operador/PSE, o débito de cuenta + comprobante) con remitentes y "
            "textos diferentes pero el MISMO monto. Si el movimiento de un correo ya está en la "
            "lista de arriba (mismo monto y misma naturaleza, aunque el concepto difiera), NO lo "
            "registres de nuevo. Lo mismo aplica entre los correos de este lote: si dos describen "
            "el mismo pago, registralo una sola vez."
        )
    return msg


def test_realtime_sends_same_prompt_tools_and_message(world, monkeypatch):
    today = _today()
    world.add_transaction(date_str=today, amount=12000, concept="Cafe ficticio", sid="otro")
    world.add_transaction(date_str="2000-01-01", amount=1, concept="viejo")  # otro mes: no entra
    for e in (PURCHASE, CARD_PAYMENT, INBOUND, NEWSLETTER):
        world.add_message(e)
    world.history_ids = [PURCHASE["id"], CARD_PAYMENT["id"], INBOUND["id"], NEWSLETTER["id"]]
    provider = ScriptedProvider(txn_date=today)
    monkeypatch.setattr(gp, "build_llm_provider", lambda: provider)
    monkeypatch.setattr(gp, "resolve_llm_model", lambda **_kw: "modelo-x")

    gp.run_gmail_push(7, "123")

    assert len(provider.calls) == 1
    call = provider.calls[0]
    assert call["system_prompt"] is gp.SYSTEM_PROMPT
    assert call["tools"] is gp.TOOLS
    assert call["max_iterations"] == 10
    assert call["model"] == "modelo-x"
    assert set(call["tool_map"]) == {"get_categories", "get_classification_hints", "create_transaction"}
    # La guarda saca el pago de TC antes del LLM; el entrante va con force pending.
    expected = _expected_initial_message(
        [PURCHASE, {**INBOUND, "force_pending": True}, NEWSLETTER],
        [f"- id={world.transactions[0]['id']} expense $12000 Cafe ficticio"],
    )
    assert call["initial_message"] == expected


def test_realtime_creates_and_notifies_ui_per_movement(world, monkeypatch):
    today = _today()
    world.add_message(PURCHASE)
    world.add_message(INBOUND)
    world.history_ids = [PURCHASE["id"], INBOUND["id"]]
    provider = ScriptedProvider(txn_date=today)
    monkeypatch.setattr(gp, "build_llm_provider", lambda: provider)
    monkeypatch.setattr(gp, "resolve_llm_model", lambda **_kw: "m")

    gp.run_gmail_push(7, "123")

    created = world.created_rows()
    assert [c["metadata"]["source_event_id"] for c in created] == ["m-compra", "m-entrante"]
    assert all(c["source"] == "gmail" for c in created)
    by_sid = {c["metadata"]["source_event_id"]: c for c in created}
    assert by_sid["m-entrante"]["status"] == "pending"  # force pending en create_transaction
    kinds = [e["event_type"] for e in world.ui_events()]
    # Por cada movimiento: data_changed + tarjeta (confirmada) o botones (pendiente).
    assert kinds == ["data_changed", "show_card", "data_changed", "show_quick_replies"]


def test_realtime_without_new_messages_does_nothing(world, monkeypatch):
    provider = ScriptedProvider()
    monkeypatch.setattr(gp, "build_llm_provider", lambda: provider)
    gp.run_gmail_push(7, "123")
    assert provider.calls == []
    assert world.post_calls == []


def test_create_transaction_guard_blocks_card_payment(world):
    tool = gp._build_tool_map(7)["create_transaction"]
    result = tool({"date": "2026-01-01", "concept": "Pago Tarjeta de Crédito", "amount": 300000,
                   "transaction_type": "expense", "status": "confirmed",
                   "metadata": {"source_event_id": "x"}})
    assert result == {"ok": True, "created": False, "ignored": True, "reason": "credit_card_payment"}
    assert world.post_calls == []


def test_create_transaction_duplicate_source_event_id_returns_already_existed(world):
    world.add_transaction(date_str="2026-01-01", amount=5000, sid="dup")
    tool = gp._build_tool_map(7)["create_transaction"]
    result = tool({"date": "2026-01-01", "concept": "Compra", "amount": 5000, "transaction_type": "expense",
                   "status": "confirmed", "metadata": {"source_event_id": "dup"}})
    assert result["already_existed"] is True and result["created"] is False
    assert world.ui_events() == []


def test_openai_compatible_provider_accumulates_usage_without_changing_result(monkeypatch):
    import httpx

    from adapters.openai_compatible_llm import OpenAICompatibleLlmProvider

    provider = OpenAICompatibleLlmProvider("k", provider_name="deepseek", base_url="http://llm.test",
                                           default_model="deepseek-chat")
    body = {"choices": [{"message": {"content": "listo"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 30, "prompt_cache_hit_tokens": 100}}
    monkeypatch.setattr(provider, "_post_chat",
                        lambda payload: httpx.Response(200, json=body, request=httpx.Request("POST", "http://llm.test")))
    out = provider.run_agent(system_prompt="s", tools=[], tool_map={}, initial_message="hola")
    provider.run_agent(system_prompt="s", tools=[], tool_map={}, initial_message="hola")
    assert out == "listo"
    assert provider.usage_totals == {"prompt_tokens": 240, "completion_tokens": 60, "prompt_cache_hit_tokens": 200}
