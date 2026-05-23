"""
agents/debugger.py — Sub-agente que diagnostica errores 500 y propone o aplica fixes.

Flujo:
  1. Recibe el payload del error (stacktrace, endpoint, params)
  2. Extrae rutas de archivos app/ del stacktrace
  3. Lee esos archivos desde GitHub
  4. Claude diagnostica: root cause + fix propuesto + scope (simple/complex)
  5. Simple  → abre PR automáticamente → notifica por Telegram con el link
  6. Complex → envía mensaje Telegram con botones [Autorizar] [Rechazar]
              → si autoriza, abre PR
"""

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from adapters.telegram_messenger import TelegramMessenger
from services import github_client
from services.claude_client import run_agent

logger = logging.getLogger(__name__)

# error_id → pending fix payload — persiste en memoria hasta que el usuario responda
PENDING_APPROVALS: dict[int, dict] = {}

SYSTEM_PROMPT = """Eres un agente debugger de la API Rails 8 (daniel15k-api).
Tu trabajo es analizar errores 500 en producción, identificar el root cause y proponer un fix exacto.

Arquitectura del proyecto:
- Rails 8 API-only con DDD
- Flujo: Controller → Interactor → Repository → Entity → Presenter
- Dominio principal: app/domains/finanzas/
- Modelos ActiveRecord: app/models/ (solo validaciones, associations, scopes)

Reglas para clasificar el scope:
- SIMPLE: 1 archivo, sin migración, sin lógica de negocio nueva (validaciones, constantes, allowlists, typos)
- COMPLEX: múltiples archivos, cambio de lógica de negocio, migración requerida, cambio arquitectural

Cuando respondas, devuelve SIEMPRE un JSON con esta estructura exacta:
{
  "diagnosis": "explicación del root cause en 1-2 oraciones",
  "scope": "simple" | "complex",
  "scope_reason": "por qué es simple o complejo",
  "fix": {
    "file": "app/ruta/al/archivo.rb",
    "description": "descripción del cambio en menos de 80 caracteres",
    "new_content": "contenido completo del archivo con el fix aplicado"
  }
}

Si no podés proponer un fix concreto (falta contexto, bug lógico profundo), devuelve scope: "complex" y omite el campo "fix".
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
    files = []
    seen = set()
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
            f"🔍 <b>Error detectado</b>\n"
            f"<code>{payload.exception_class}</code>\n"
            f"<code>{payload.http_method} {payload.endpoint}</code>\n"
            f"Analizando..."
        )

        context = _build_context(payload)
        response_text = run_agent(
            system_prompt=SYSTEM_PROMPT,
            tools=[],
            tool_map={},
            initial_message=context,
            max_iterations=1,
        )

        result = _parse_response(response_text)
        if not result:
            messenger.send_message(
                f"⚠️ <b>No pude diagnosticar el error</b>\n"
                f"<code>{payload.exception_class}: {payload.message[:200]}</code>\n"
                f"Revisá los logs manualmente."
            )
            return

        diagnosis  = result.get("diagnosis", "")
        scope      = result.get("scope", "complex")
        fix        = result.get("fix")

        if scope == "simple" and fix and fix.get("file") and fix.get("new_content"):
            _handle_simple(payload, messenger, diagnosis, fix)
        else:
            _handle_complex(payload, messenger, diagnosis, scope, result.get("scope_reason", ""), fix)

    except Exception as e:
        logger.error("[debugger] Unexpected error: %s", e, exc_info=True)
        messenger.send_message(f"❌ <b>Debugger falló</b>: {e}")


def _handle_simple(payload: DebugPayload, messenger: TelegramMessenger,
                   diagnosis: str, fix: dict) -> None:
    pr_url = github_client.open_fix_pr(
        error_id=payload.error_id,
        error_hash=payload.error_hash,
        file_path=fix["file"],
        new_content=fix["new_content"],
        diagnosis=diagnosis,
        fix_description=fix.get("description", "auto-fix"),
    )

    if pr_url:
        messenger.send_message(
            f"✅ <b>Fix listo</b>\n\n"
            f"<b>Diagnóstico:</b> {diagnosis}\n\n"
            f"<b>Archivo:</b> <code>{fix['file']}</code>\n"
            f"<b>Fix:</b> {fix.get('description', '')}\n\n"
            f"🔗 <a href='{pr_url}'>Ver PR</a>"
        )
    else:
        messenger.send_message(
            f"⚠️ <b>Fix diagnosticado pero no pude abrir el PR</b>\n\n"
            f"<b>Diagnóstico:</b> {diagnosis}\n"
            f"<b>Fix:</b> {fix.get('description', '')} en <code>{fix['file']}</code>"
        )


def _handle_complex(payload: DebugPayload, messenger: TelegramMessenger,
                    diagnosis: str, scope: str, scope_reason: str, fix: dict | None) -> None:
    fix_summary = (
        f"\n<b>Fix propuesto:</b> {fix.get('description', '')} en <code>{fix.get('file', '')}</code>"
        if fix else "\n<i>No hay fix concreto — requiere revisión manual.</i>"
    )

    PENDING_APPROVALS[payload.error_id] = {
        "payload": payload,
        "fix": fix,
        "diagnosis": diagnosis,
    }

    messenger.send_with_buttons(
        text=(
            f"🟡 <b>Error complejo detectado</b>\n\n"
            f"<b>Diagnóstico:</b> {diagnosis}\n"
            f"<b>Razón:</b> {scope_reason}"
            f"{fix_summary}\n\n"
            f"¿Autorizo que el agente abra un PR?"
        ),
        buttons=[[
            {"text": "✅ Autorizar", "callback_data": f"debug:approve:{payload.error_id}"},
            {"text": "❌ Ignorar",   "callback_data": f"debug:reject:{payload.error_id}"},
        ]],
    )


def handle_approve(error_id: int) -> None:
    messenger = TelegramMessenger()
    pending = PENDING_APPROVALS.pop(error_id, None)

    if not pending:
        messenger.send_message("⚠️ No encontré el fix pendiente (puede haber expirado si el agente se reinició).")
        return

    fix     = pending.get("fix")
    payload = pending["payload"]

    if not fix or not fix.get("file") or not fix.get("new_content"):
        messenger.send_message("⚠️ El fix propuesto no tiene contenido suficiente para crear un PR automático. Revisá manualmente.")
        return

    pr_url = github_client.open_fix_pr(
        error_id=error_id,
        error_hash=payload.error_hash,
        file_path=fix["file"],
        new_content=fix["new_content"],
        diagnosis=pending["diagnosis"],
        fix_description=fix.get("description", "authorized fix"),
    )

    if pr_url:
        messenger.send_message(f"✅ PR abierto: <a href='{pr_url}'>Ver PR</a>")
    else:
        messenger.send_message("❌ No pude abrir el PR. Revisá los logs del agente.")


def handle_reject(error_id: int) -> None:
    PENDING_APPROVALS.pop(error_id, None)
    TelegramMessenger().send_message(f"🚫 Fix ignorado para error #{error_id}.")


def _parse_response(text: str) -> dict | None:
    try:
        # Extraer JSON del bloque de código si viene envuelto
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        raw = match.group(1) if match else text.strip()
        # Encontrar el primer { y el último }
        start = raw.index("{")
        end   = raw.rindex("}") + 1
        return json.loads(raw[start:end])
    except Exception as e:
        logger.warning("[debugger] Failed to parse response: %s", e)
        return None
