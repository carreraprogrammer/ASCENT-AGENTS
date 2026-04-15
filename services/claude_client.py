"""
services/claude_client.py — Wrapper para la API de Anthropic.

Los agentes usan esto para correr tool-calling loops con Claude.
"""

import os
import json
import logging
from typing import Callable

import anthropic

logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
MAX_TOKENS = 8096


def run_agent(
    system_prompt: str,
    tools: list[dict],
    tool_map: dict[str, Callable],
    initial_message: str,
    max_iterations: int = 20,
    model: str | None = None,
) -> str:
    """
    Corre un tool-calling loop con Claude.

    Args:
        system_prompt: instrucciones del sistema para el agente
        tools: lista de definiciones de herramientas en formato Anthropic
        tool_map: diccionario nombre_herramienta → función Python
        initial_message: primer mensaje del usuario
        max_iterations: límite de seguridad para evitar loops infinitos
        model: modelo opcional; si no se envía usa MODEL

    Returns:
        El último texto generado por Claude (sin tool calls)
    """
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    messages = [{"role": "user", "content": initial_message}]

    for i in range(max_iterations):
        response = client.messages.create(
            model=model or MODEL,
            max_tokens=MAX_TOKENS,
            system=system_prompt,
            tools=tools,
            messages=messages,
        )

        logger.debug("[claude_client] iteration %d, stop_reason=%s", i, response.stop_reason)

        # Extraer texto y tool_use del response
        assistant_content = response.content
        messages.append({"role": "assistant", "content": assistant_content})

        if response.stop_reason == "end_turn":
            # Devolver el último bloque de texto
            for block in reversed(assistant_content):
                if hasattr(block, "text"):
                    return block.text
            return ""

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in assistant_content:
                if block.type != "tool_use":
                    continue
                tool_name = block.name
                tool_input = block.input
                tool_use_id = block.id

                logger.info("[claude_client] calling tool: %s", tool_name)

                if tool_name not in tool_map:
                    result = {"error": f"Unknown tool: {tool_name}"}
                else:
                    try:
                        result = tool_map[tool_name](tool_input)
                    except Exception as e:
                        logger.error("[claude_client] tool %s failed: %s", tool_name, e)
                        result = {"error": str(e)}

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": json.dumps(result, ensure_ascii=False, default=str),
                })

            messages.append({"role": "user", "content": tool_results})
        else:
            logger.warning("[claude_client] unexpected stop_reason: %s", response.stop_reason)
            break

    logger.error("[claude_client] reached max_iterations (%d) without end_turn", max_iterations)
    return ""
