"""In-memory conversation history with TTL per session key."""
from __future__ import annotations

import threading
import time

TTL_SECONDS = 1800   # 30 min inactivity resets the conversation
MAX_TURNS   = 8      # keep last 8 user/assistant pairs


class ConversationStore:
    def __init__(self):
        self._lock  = threading.Lock()
        self._store: dict[str, tuple[float, list[dict]]] = {}

    def get(self, key: str) -> list[dict]:
        with self._lock:
            entry = self._store.get(key)
            if not entry:
                return []
            expires_at, messages = entry
            if time.time() > expires_at:
                del self._store[key]
                return []
            return list(messages)

    def append(self, key: str, user_text: str, assistant_text: str) -> None:
        with self._lock:
            entry = self._store.get(key)
            messages = list(entry[1]) if entry else []
            messages.append({"role": "user",      "content": user_text})
            messages.append({"role": "assistant", "content": assistant_text})
            if len(messages) > MAX_TURNS * 2:
                messages = messages[-(MAX_TURNS * 2):]
            self._store[key] = (time.time() + TTL_SECONDS, messages)

    def clear(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)


_store = ConversationStore()


def get_history(key: str) -> list[dict]:
    return _store.get(key)


def append_history(key: str, user_text: str, assistant_text: str) -> None:
    _store.append(key, user_text, assistant_text)


def clear_history(key: str) -> None:
    _store.clear(key)
