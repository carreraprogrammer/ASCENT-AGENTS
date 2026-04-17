import logging
from datetime import datetime

from ports.messenger import MessengerPort, ParsedUpdate
from ports.rails_api import RailsApiPort
from services.chat_context import COLOMBIA_TZ, event_source_id, normalize_telegram_html, telegram_context
from services.chat_preflight import detect_preflight_intent, inject_soft_nudge, run_preflight
from services.chat_prompts import CHAT_MODEL, COMMAND_PROMPTS, HELP_TEXT, SYSTEM_PROMPT
from services.chat_tools import build_tool_map, build_tools
from services.claude_client import run_agent

logger = logging.getLogger(__name__)


def _apply_preflight(
    api: RailsApiPort,
    messenger: MessengerPort,
    *,
    initial_message: str,
    command: str | None = None,
    text: str | None = None,
) -> str | None:
    intent = detect_preflight_intent(command=command, text=text)
    if not intent:
        return initial_message

    now_col = datetime.now(COLOMBIA_TZ)
    result = run_preflight(api, intent=intent, now=now_col)
    action = result.get("action")

    if action == "block" and (result.get("wizard") or {}).get("type") == "budget_planning":
        from flows import budget_wizard

        budget_wizard.trigger(api, messenger, reason=result.get("message"))
        return None

    if action == "soft_nudge":
        return inject_soft_nudge(initial_message, result)

    return initial_message


def _run_conversation(
    api: RailsApiPort,
    messenger: MessengerPort,
    initial_message: str,
    source_event_id: str | None = None,
) -> None:
    now_col = datetime.now(COLOMBIA_TZ)
    state = {"responded": False, "mutated": False, "source_event_id": source_event_id}
    final_text = run_agent(
        system_prompt=SYSTEM_PROMPT,
        tools=build_tools(),
        tool_map=build_tool_map(api, messenger, now_col, state),
        initial_message=initial_message,
        max_iterations=12,
        model=CHAT_MODEL,
    )

    if state["responded"]:
        return

    if final_text:
        messenger.send_message(normalize_telegram_html(final_text))
        return

    if state["mutated"]:
        messenger.send_message("✅ Listo.")
        return

    messenger.send_message("⚠️ No pude cerrar bien la respuesta. Intentá de nuevo.")


def handle_command(api: RailsApiPort, messenger: MessengerPort, parsed: ParsedUpdate) -> None:
    command = parsed.command or ""

    if command not in COMMAND_PROMPTS:
        messenger.send_message(f"❓ No conozco el comando <code>/{command}</code>.\n\n{HELP_TEXT}")
        return

    try:
        initial_message = _apply_preflight(
            api,
            messenger,
            initial_message=COMMAND_PROMPTS[command],
            command=command,
            text=parsed.text,
        )
        if initial_message is None:
            return

        _run_conversation(api, messenger, initial_message, source_event_id=None)
    except Exception as exc:
        logger.error("[chat_agent] command error: %s", exc, exc_info=True)
        messenger.send_message("❌ Tuve un problema. Intentá de nuevo.")


def handle_message(api: RailsApiPort, messenger: MessengerPort, parsed: ParsedUpdate) -> None:
    text = (parsed.text or "").strip()
    if not text:
        return

    initial_message = (
        "Mensaje nuevo de Daniel en Telegram. "
        "Interpretalo y actuá en tiempo real usando las herramientas disponibles. "
        "Si es un gasto o ingreso claro, registralo. "
        "Si es una corrección o borrado, usá transacciones recientes para resolverlo. "
        "Si hay ambigüedad real, pedí una aclaración breve.\n\n"
        f"{telegram_context(parsed)}\n\n"
        f"Mensaje: {text}"
    )

    try:
        initial_message = _apply_preflight(
            api,
            messenger,
            initial_message=initial_message,
            text=text,
        )
        if initial_message is None:
            return

        _run_conversation(api, messenger, initial_message, source_event_id=event_source_id(parsed))
    except Exception as exc:
        logger.error("[chat_agent] realtime error: %s", exc, exc_info=True)
        messenger.send_message("❌ No pude procesar eso. Intentá de nuevo.")
