"""
agents/debugger.py — Sub-agente que diagnostica errores 500 y deja que el usuario
elija la estrategia de deploy.

Flujo:
  1. Recibe el payload del error (stacktrace, endpoint, params)
  2. Lee los archivos app/ relevantes desde GitHub
  3. Claude diagnostica: root cause + fix propuesto
  4. Envía resumen por Telegram con 3 opciones:
       [🚀 Hotfix a main]  [🔍 Abrir PR]  [🚫 Ignorar]
  5. Hotfix → push directo a main → Railway despliega automáticamente
     PR      → branch nueva + PR para revisión detallada
     Ignorar → descarta sin acción
"""

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from adapters.telegram_messenger import TelegramMessenger
from services import github_client
from services.llm_factory import build_llm_provider

logger = logging.getLogger(__name__)

# error_id → pending fix — persiste en memoria hasta que el usuario responda
PENDING_FIXES: dict[int, dict] = {}

SYSTEM_PROMPT = """Eres un agente debugger de la API Rails 8 (daniel15k-api).
Tu trabajo es analizar errores 500 en producción, identificar el root cause y proponer un fix exacto.

Arquitectura del proyecto:
- Rails 8 API-only con DDD
- Flujo: Controller → Interactor → Repository → Entity → Presenter
- Dominio principal: app/domains/finanzas/
- Modelos ActiveRecord: app/models/ (solo validaciones, associations, scopes)

Cuando respondas, devuelve SIEMPRE un JSON con esta estructura exacta:
{
  "diagnosis": "explicación del root cause en 1-2 oraciones",
  "fix": {
    "file": "app/ruta/al/archivo.rb",
    "description": "descripción del cambio en menos de 80 caracteres",
    "new_content": "contenido completo del archivo con el fix aplicado"
  },
  "confidence": "high" | "medium" | "low",
  "confidence_reason": "por qué tenés esa confianza en el fix"
}

Si no podés proponer un fix concreto, omite el campo "fix" y explicá en "diagnosis" qué necesitaría revisión manual.
"""


@dataclass
class DebugPayload:
    error_id: int
    error_hash: str
    exception_class: str
    message: str
    stacktrace: list[str]
    endpoint: str
    http_method: str
    params: dict[str, Any]


def _extract_app_files(stacktrace: list[str]) -> list[str]:
    files, seen = [], set()
    for line in stacktrace:
        m = re.match(r"(app/[^:]+\.rb)", line)
        if m and m.group(1) not in seen:
            seen.add(m.group(1))
            files.append(m.group(1))
    return files[:6]


def _build_context(payload: DebugPayload) -> str:
    files_content = []
    for path in _extract_app_files(payload.stacktrace):
        content = github_client.get_file(path)
        if content:
            files_content.append(f"### {path}\n```ruby\n{content}\n```")

    stacktrace_str = "\n".join(payload.stacktrace[:15])
    files_str = "\n\n".join(files_content) if files_content else "_No se pudieron leer archivos._"

    return f"""## Error en producción

**Exception:** `{payload.exception_class}`
**Message:** {payload.message}
**Endpoint:** `{payload.http_method} {payload.endpoint}`
**Params:** `{json.dumps(payload.params, ensure_ascii=False)[:300]}`

## Stacktrace
```
{stacktrace_str}
```

## Archivos relevantes

{files_str}

Diagnosticá el root cause y proponé el fix. Devolvé únicamente el JSON solicitado."""


def handle(payload: DebugPayload) -> None:
    messenger = TelegramMessenger()
    try:
        messenger.send_message(
            f"🔍 <b>Analizando error...</b>\n"
            f"<code>{payload.exception_class}</code>\n"
            f"<code>{payload.http_method} {payload.endpoint}</code>"
        )

        provider = build_llm_provider()
        result = _parse_response(provider.run_agent(
            system_prompt=SYSTEM_PROMPT,
            tools=[],
            tool_map={},
            initial_message=_build_context(payload),
            max_iterations=1,
        ))

        if not result:
            messenger.send_message(
                f"⚠️ <b>No pude diagnosticar el error</b>\n"
                f"<code>{payload.exception_class}: {payload.message[:200]}</code>\n"
                f"Revisá los logs manualmente."
            )
            return

        diagnosis  = result.get("diagnosis", "Sin diagnóstico")
        fix        = result.get("fix")
        confidence = result.get("confidence", "low")
        conf_reason = result.get("confidence_reason", "")

        PENDING_FIXES[payload.error_id] = {
            "payload":   payload,
            "fix":       fix,
            "diagnosis": diagnosis,
        }

        confidence_emoji = {"high": "🟢", "medium": "🟡", "low": "🔴"}.get(confidence, "⚪")

        if fix:
            fix_line = f"\n\n🔧 <b>Fix:</b> <code>{fix.get('file', '')}</code>\n{fix.get('description', '')}"
            buttons = [[
                {"text": "🚀 Hotfix a main", "callback_data": f"debug:hotfix:{payload.error_id}"},
                {"text": "🔍 Abrir PR",      "callback_data": f"debug:pr:{payload.error_id}"},
                {"text": "🚫 Ignorar",       "callback_data": f"debug:reject:{payload.error_id}"},
            ]]
        else:
            fix_line = "\n\n<i>No hay fix automático — requiere revisión manual.</i>"
            buttons = [[
                {"text": "🚫 Ignorar", "callback_data": f"debug:reject:{payload.error_id}"},
            ]]

        messenger.send_with_buttons(
            text=(
                f"🐛 <b>Error en producción</b>\n\n"
                f"<b>Exception:</b> <code>{payload.exception_class}</code>\n"
                f"<b>Endpoint:</b> <code>{payload.http_method} {payload.endpoint}</code>\n\n"
                f"📋 <b>Diagnóstico</b>\n{diagnosis}"
                f"{fix_line}\n\n"
                f"{confidence_emoji} Confianza: <b>{confidence}</b> — {conf_reason}"
            ),
            buttons=buttons,
        )

    except Exception as e:
        logger.error("[debugger] Unexpected error: %s", e, exc_info=True)
        messenger.send_message(f"❌ <b>Debugger falló</b>: {e}")


def handle_hotfix(error_id: int) -> None:
    messenger = TelegramMessenger()
    pending = PENDING_FIXES.pop(error_id, None)

    if not pending or not pending.get("fix"):
        messenger.send_message("⚠️ No encontré el fix (puede haber expirado si el agente se reinició).")
        return

    fix     = pending["fix"]
    payload = pending["payload"]

    ok = github_client.hotfix_main(
        error_id=error_id,
        file_path=fix["file"],
        new_content=fix["new_content"],
        fix_description=fix.get("description", "hotfix"),
    )

    if ok:
        messenger.send_message(
            f"🚀 <b>Hotfix pusheado a main</b>\n\n"
            f"<code>{fix['file']}</code>\n"
            f"{fix.get('description', '')}\n\n"
            f"Railway está desplegando..."
        )
    else:
        messenger.send_message("❌ No pude pushear a main. Revisá los logs del agente.")


def handle_pr(error_id: int) -> None:
    messenger = TelegramMessenger()
    pending = PENDING_FIXES.pop(error_id, None)

    if not pending or not pending.get("fix"):
        messenger.send_message("⚠️ No encontré el fix (puede haber expirado si el agente se reinició).")
        return

    fix     = pending["fix"]
    payload = pending["payload"]

    pr_url = github_client.open_fix_pr(
        error_id=error_id,
        error_hash=payload.error_hash,
        file_path=fix["file"],
        new_content=fix["new_content"],
        diagnosis=pending["diagnosis"],
        fix_description=fix.get("description", "fix"),
    )

    if pr_url:
        messenger.send_message(
            f"🔍 <b>PR creado</b>\n\n"
            f"<code>{fix['file']}</code>\n"
            f"{fix.get('description', '')}\n\n"
            f"🔗 <a href='{pr_url}'>Revisar PR</a>"
        )
    else:
        messenger.send_message("❌ No pude crear el PR. Revisá los logs del agente.")


def handle_reject(error_id: int) -> None:
    PENDING_FIXES.pop(error_id, None)
    TelegramMessenger().send_message(f"🚫 Error #{error_id} ignorado.")


def _parse_response(text: str) -> dict | None:
    try:
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        raw   = match.group(1) if match else text.strip()
        start = raw.index("{")
        end   = raw.rindex("}") + 1
        return json.loads(raw[start:end])
    except Exception as e:
        logger.warning("[debugger] Failed to parse response: %s", e)
        return None
