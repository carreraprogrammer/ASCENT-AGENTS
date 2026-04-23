from __future__ import annotations

import json
import logging
from typing import Callable

import httpx

from ports.llm_provider import LlmProviderPort

logger = logging.getLogger(__name__)


def _to_openai_tools(tools: list[dict]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
            },
        }
        for tool in tools
    ]


def _normalize_text_content(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                texts.append(item.get("text", ""))
        return "\n".join(part for part in texts if part).strip()
    return ""


class OpenAICompatibleLlmProvider(LlmProviderPort):
    def __init__(
        self,
        api_key: str,
        *,
        provider_name: str,
        base_url: str,
        default_model: str,
    ):
        if not api_key:
            raise ValueError(f"Missing API key for {provider_name} provider.")
        self._provider_name = provider_name
        self._default_model = default_model
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=60,
        )

    def run_agent(
        self,
        *,
        system_prompt: str,
        tools: list[dict],
        tool_map: dict[str, Callable],
        initial_message: str,
        max_iterations: int = 20,
        model: str | None = None,
    ) -> str:
        messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": initial_message},
        ]
        tool_defs = _to_openai_tools(tools)

        for i in range(max_iterations):
            payload = {
                "model": model or self._default_model,
                "messages": messages,
                "tools": tool_defs,
                "tool_choice": "auto",
            }
            if self._provider_name != "kimi":
                payload["temperature"] = 0.2
            else:
                payload["thinking"] = {"type": "disabled"}

            response = self._client.post("/chat/completions", json=payload)
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                body = exc.response.text[:2000]
                raise RuntimeError(
                    f"{self._provider_name} chat completion failed with {exc.response.status_code}: {body}"
                ) from exc

            data = response.json()
            choice = (data.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            finish_reason = choice.get("finish_reason")
            assistant_text = _normalize_text_content(message.get("content"))
            tool_calls = message.get("tool_calls") or []

            logger.debug(
                "[%s_llm] iteration %d, finish_reason=%s, tool_calls=%d",
                self._provider_name,
                i,
                finish_reason,
                len(tool_calls),
            )

            assistant_message: dict[str, object] = {"role": "assistant"}
            assistant_message["content"] = assistant_text or ""
            if tool_calls:
                assistant_message["tool_calls"] = tool_calls
            messages.append(assistant_message)

            if not tool_calls:
                return assistant_text

            for call in tool_calls:
                function = call.get("function") or {}
                tool_name = function.get("name")
                raw_arguments = function.get("arguments") or "{}"
                try:
                    tool_input = json.loads(raw_arguments)
                except json.JSONDecodeError:
                    tool_input = {}

                logger.info("[%s_llm] calling tool: %s", self._provider_name, tool_name)

                if tool_name not in tool_map:
                    result = {"error": f"Unknown tool: {tool_name}"}
                else:
                    try:
                        result = tool_map[tool_name](tool_input)
                    except Exception as exc:
                        logger.error("[%s_llm] tool %s failed: %s", self._provider_name, tool_name, exc)
                        result = {"error": str(exc)}

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id"),
                        "content": json.dumps(result, ensure_ascii=False, default=str),
                    }
                )

        logger.error("[%s_llm] reached max_iterations (%d) without final text", self._provider_name, max_iterations)
        return ""
