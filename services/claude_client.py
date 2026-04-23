from typing import Callable

from dotenv import load_dotenv

from adapters.anthropic_llm import AnthropicLlmProvider

load_dotenv()


def run_agent(
    system_prompt: str,
    tools: list[dict],
    tool_map: dict[str, Callable],
    initial_message: str,
    max_iterations: int = 20,
    model: str | None = None,
) -> str:
    import os

    provider = AnthropicLlmProvider(
        api_key=(os.environ.get("ANTHROPIC_API_KEY") or "").strip(),
        default_model=(os.environ.get("CLAUDE_MODEL") or "claude-sonnet-4-6").strip(),
    )
    return provider.run_agent(
        system_prompt=system_prompt,
        tools=tools,
        tool_map=tool_map,
        initial_message=initial_message,
        max_iterations=max_iterations,
        model=model,
    )
