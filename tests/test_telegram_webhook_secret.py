"""/webhook/telegram solo acepta updates con el secreto que Telegram reenvía."""

import importlib

import pytest
from fastapi import HTTPException


class _Request:
    def __init__(self, headers: dict[str, str]):
        self.headers = headers


@pytest.fixture
def webhook(monkeypatch):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "test-secret")
    import routers.webhook as module

    return importlib.reload(module)


def test_accepts_matching_secret(webhook):
    webhook._verify_telegram_secret(_Request({"X-Telegram-Bot-Api-Secret-Token": "test-secret"}))


@pytest.mark.parametrize("headers", [{}, {"X-Telegram-Bot-Api-Secret-Token": "wrong"}])
def test_rejects_missing_or_wrong_secret(webhook, headers):
    with pytest.raises(HTTPException) as error:
        webhook._verify_telegram_secret(_Request(headers))
    assert error.value.status_code == 403


def test_rejects_everything_when_secret_is_not_configured(monkeypatch):
    monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET", raising=False)
    import routers.webhook as module

    module = importlib.reload(module)
    with pytest.raises(HTTPException) as error:
        module._verify_telegram_secret(_Request({"X-Telegram-Bot-Api-Secret-Token": ""}))
    assert error.value.status_code == 403
