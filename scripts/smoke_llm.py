"""
Smoke test real para providers LLM con tool calling.

Uso:
  source .env
  LLM_PROVIDER=openai LLM_MODEL=gpt-4.1-mini ./.venv/bin/python scripts/smoke_llm.py
  LLM_PROVIDER=kimi LLM_MODEL=kimi-k2.6 ./.venv/bin/python scripts/smoke_llm.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.llm_factory import build_llm_provider, resolve_llm_model, resolve_llm_provider_name

load_dotenv()


def _echo_tool(payload: dict) -> dict:
    return {
        "echo": payload.get("text", ""),
        "length": len(payload.get("text", "")),
    }


def main() -> None:
    provider_name = resolve_llm_provider_name()
    model = resolve_llm_model()
    provider = build_llm_provider()

    tools = [
        {
            "name": "echo_tool",
            "description": "Echoes the received text and returns its length.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                },
                "required": ["text"],
            },
        }
    ]

    final_text = provider.run_agent(
        system_prompt=(
            "You are a tool-using smoke test. "
            "Call echo_tool exactly once with the user's phrase, then answer with "
            "'TOOL_OK <echo> <length>'."
        ),
        tools=tools,
        tool_map={"echo_tool": _echo_tool},
        initial_message="Use the tool with text='hola provider'.",
        max_iterations=4,
        model=model,
    )

    print(f"provider={provider_name}")
    print(f"model={model}")
    print(f"result={final_text}")

    if "TOOL_OK" not in final_text:
        raise SystemExit("Smoke test failed: provider did not complete the tool loop as expected.")


if __name__ == "__main__":
    main()
