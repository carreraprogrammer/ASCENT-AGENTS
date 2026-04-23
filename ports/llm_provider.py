from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable


class LlmProviderPort(ABC):
    @abstractmethod
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
        raise NotImplementedError
