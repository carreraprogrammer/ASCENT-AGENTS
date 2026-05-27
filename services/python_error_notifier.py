"""
services/python_error_notifier.py — Captura excepciones Python y dispara el debugger.

Equivalente a ErrorCaptureMiddleware + ErrorNotifier de Rails, pero para el agente.
Dedup en memoria (1h window). Thread-safe para uso desde background tasks y schedulers.
"""

import hashlib
import logging
import time
import traceback
from threading import Thread

logger = logging.getLogger(__name__)

_seen_errors: dict[str, float] = {}
_DEDUP_WINDOW = 3600  # 1 hora

_counter = 0


def capture(exc: Exception, context: str = "", params: dict | None = None) -> None:
    """Captura una excepción Python y despacha al agente debugger en background."""
    global _counter

    # No debuguear el debugger (evita loops infinitos)
    tb_frames = traceback.extract_tb(exc.__traceback__)
    if any("debugger" in (frame.filename or "") for frame in tb_frames):
        logger.warning("[python_error_notifier] skipping error originating from debugger")
        return

    tb_lines = traceback.format_tb(exc.__traceback__)
    first_frame = tb_lines[0].strip() if tb_lines else ""

    error_hash = hashlib.sha256(
        f"{type(exc).__name__}:{str(exc)[:200]}:{first_frame}".encode()
    ).hexdigest()[:16]

    now = time.time()
    if error_hash in _seen_errors and (now - _seen_errors[error_hash]) < _DEDUP_WINDOW:
        logger.debug("[python_error_notifier] duplicate suppressed: %s", error_hash)
        return
    _seen_errors[error_hash] = now

    _counter += 1
    error_id = _counter

    stacktrace = [line for raw in tb_lines for line in raw.splitlines() if line.strip()]

    from agents.debugger import DebugPayload, handle as debugger_handle

    payload = DebugPayload(
        error_id=error_id,
        error_hash=error_hash,
        exception_class=type(exc).__name__,
        message=str(exc)[:500],
        stacktrace=stacktrace,
        endpoint=context or "python_agent",
        http_method="INTERNAL",
        params=params or {},
    )

    logger.info(
        "[python_error_notifier] dispatching error_id=%s %s: %s @ %s",
        error_id, type(exc).__name__, str(exc)[:100], context,
    )
    Thread(target=debugger_handle, args=(payload,), daemon=True).start()
