from __future__ import annotations

import json
import logging
from typing import Callable

import anthropic

from ports.llm_provider import LlmProviderPort

logger = logging.getLogger(__name__)
MAX_TOKENS = 8096
DEFAULT_MODEL = "claude-sonnet-4-6"


class AnthropicLlmProvider(LlmProviderPort):
    def __init__(self, api_key: str, *, default_model: str = DEFAULT_MODEL):
        if not api_key:
            raise ValueError("Missing API key for Anthropic provider. Set ANTHROPIC_API_KEY.")
        self._client = anthropic.Anthropic(api_key=api_key)
        self._default_model = default_model

    def run_agent(
        self,
        *,
        system_prompt: str,
        tools: list[dict],
        tool_map: dict[str, Callable],
        initial_message: str,
        max_iterations: int = 20,
        model: str | None = None,
        prior_messages: list[dict] | None = None,
    ) -> str:
        messages = [
            *(prior_messages or []),
            {"role": "user", "content": initial_message},
        ]

        # El system prompt se cachea con prompt caching de Anthropic.
        # Costo: solo se cobra en el primer hit por ventana de 5 min.
        # Impacto: ~90% de reducción de costo en tokens de input del system prompt.
        cached_system = [
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ]

        for i in range(max_iterations):
            response = self._client.messages.create(
                model=model or self._default_model,
                max_tokens=MAX_TOKENS,
                system=cached_system,
                tools=tools,
                messages=messages,
            )

            logger.debug("[anthropic_llm] iteration %d, stop_reason=%s", i, response.stop_reason)

            assistant_content = response.content
            messages.append({"role": "assistant", "content": assistant_content})

            if response.stop_reason == "end_turn":
                for block in reversed(assistant_content):
                    if hasattr(block, "text"):
                        return block.text
                return ""

            if response.stop_reason != "tool_use":
                logger.warning("[anthropic_llm] unexpected stop_reason: %s", response.stop_reason)
                break

            tool_results = []
            for block in assistant_content:
                if block.type != "tool_use":
                    continue
                tool_name = block.name
                tool_input = block.input
                tool_use_id = block.id

                logger.info("[anthropic_llm] calling tool: %s", tool_name)

                if tool_name not in tool_map:
                    result = {"error": f"Unknown tool: {tool_name}"}
                else:
                    try:
                        result = tool_map[tool_name](tool_input)
                    except Exception as exc:
                        logger.error("[anthropic_llm] tool %s failed: %s", tool_name, exc)
                        result = {"error": str(exc)}

                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": json.dumps(result, ensure_ascii=False, default=str),
                    }
                )

            messages.append({"role": "user", "content": tool_results})

        logger.error("[anthropic_llm] reached max_iterations (%d) without end_turn", max_iterations)
        return ""

    def simple_complete(
        self,
        messages: list[dict],
        *,
        system: str | None = None,
        max_tokens: int = 512,
        model: str | None = None,
    ) -> str:
        kwargs: dict = {
            "model": model or self._default_model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system:
            kwargs["system"] = system

        response = self._client.messages.create(**kwargs)
        for block in response.content:
            if hasattr(block, "text"):
                return block.text
        return ""
