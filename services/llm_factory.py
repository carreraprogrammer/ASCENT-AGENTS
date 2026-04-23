from __future__ import annotations

import os

from dotenv import load_dotenv

from adapters.anthropic_llm import AnthropicLlmProvider
from adapters.openai_compatible_llm import OpenAICompatibleLlmProvider
from ports.llm_provider import LlmProviderPort

load_dotenv()

DEFAULT_PROVIDER = "kimi"
DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-4-6",
    "openai": "gpt-4.1-mini",
    "kimi": "kimi-k2.5",
}
OPENAI_BASE_URL = "https://api.openai.com/v1"
KIMI_BASE_URL = "https://api.moonshot.ai/v1"


def _env_first(*names: str) -> str:
    for name in names:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return ""


def resolve_llm_provider_name() -> str:
    configured = _env_first("LLM_PROVIDER").lower()
    if configured:
        return configured

    if _env_first("KIMI_API_KEY", "KIMI_AI_API_KEY"):
        return "kimi"
    if _env_first("OPENAI_API_KEY", "OPEN_AI_API_KEY"):
        return "openai"
    if _env_first("ANTHROPIC_API_KEY"):
        return "anthropic"
    return DEFAULT_PROVIDER


def resolve_llm_model(*, env_var: str | None = None) -> str:
    provider = resolve_llm_provider_name()

    if env_var:
        env_value = _env_first(env_var)
        if env_value:
            return env_value

    configured = _env_first("LLM_MODEL")
    if configured:
        return configured

    if provider == "anthropic":
        return _env_first("CLAUDE_MODEL") or DEFAULT_MODELS["anthropic"]

    return DEFAULT_MODELS.get(provider, DEFAULT_MODELS[DEFAULT_PROVIDER])


def build_llm_provider() -> LlmProviderPort:
    provider = resolve_llm_provider_name()

    if provider == "anthropic":
        return AnthropicLlmProvider(
            _env_first("ANTHROPIC_API_KEY"),
            default_model=resolve_llm_model(env_var="CLAUDE_MODEL"),
        )

    if provider == "openai":
        return OpenAICompatibleLlmProvider(
            _env_first("OPENAI_API_KEY", "OPEN_AI_API_KEY"),
            provider_name="openai",
            base_url=_env_first("OPENAI_BASE_URL") or OPENAI_BASE_URL,
            default_model=resolve_llm_model(),
        )

    if provider == "kimi":
        return OpenAICompatibleLlmProvider(
            _env_first("KIMI_API_KEY", "KIMI_AI_API_KEY"),
            provider_name="kimi",
            base_url=_env_first("KIMI_BASE_URL") or KIMI_BASE_URL,
            default_model=resolve_llm_model(),
        )

    raise ValueError(f"Unsupported LLM_PROVIDER '{provider}'. Use anthropic, openai or kimi.")
