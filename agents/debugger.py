"""
agents/debugger.py — Sub-agente que diagnostica errores 500 y deja que el usuario
elija la estrategia de deploy.

Flujo:
  1. Recibe el payload del error (stacktrace, endpoint, params)
  2. Explora los 3 repos con read_file / list_files hasta entender el root cause
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

import os

from adapters.openai_compatible_llm import OpenAICompatibleLlmProvider
from adapters.telegram_messenger import TelegramMessenger
from services import github_client

logger = logging.getLogger(__name__)

# error_id → pending fix — persiste en memoria hasta que el usuario responda
PENDING_FIXES: dict[int, dict] = {}

SYSTEM_PROMPT = """Eres un agente debugger de un sistema de finanzas personales compuesto por 3 repositorios:

- **api** (`daniel15k-api`): Rails 8 API-only con DDD. Flujo estricto:
  `Request → Controller → Interactor → Repository → Entity → Presenter`
  - Controllers: solo llaman interactors y presenters. Cero lógica de negocio.
  - Interactors: orquestan la operación. Solo conocen repositories de su dominio.
  - Repositories: ÚNICA capa que toca ActiveRecord. Nunca en controllers ni interactors directamente.
  - Models (`app/models/`): solo associations, scopes y validaciones de DB. Sin callbacks de negocio.
  - Dominio principal: `app/domains/finanzas/`

- **agents** (`daniel15k-agents`): Python. El "Brain" — agente que se comunica con la API via HTTP.
  El campo `source: "brain"` en transacciones indica que fue creada por el agente.

- **web** (`daniel15k-web`): React frontend. Raramente relevante para errores 500 de API.

**Actores del sistema:**
- Usuario humano → autenticado con JWT, genera transacciones con `source: "manual"`, `"telegram"`, `"gmail"`
- Agente Brain → autenticado con service token, genera transacciones con `source: "brain"`
- Cuenta (`Account`) → agrupa los datos financieros del usuario

**Invariantes clave:**
- `Transaction.SOURCES` en `app/models/transaction.rb` define los valores válidos de `source`
- `recurring_obligations.amount` es la fuente de verdad del impacto mensual, no `debts.monthly_payment`
- `safe_to_deploy` = dinero disponible después de compromisos del próximo ciclo

**Tu proceso de diagnóstico:**
1. Analiza el stacktrace para identificar qué archivos están involucrados
2. Usa `read_file` y `list_files` para explorar el código relevante — NO asumas, LEE el código
3. Sigue la cadena: controller → interactor → repository → model → spec
4. Cuando el error es de validación, siempre lee el modelo ActiveRecord afectado
5. Lee specs relacionados si necesitás entender el comportamiento esperado

**Reglas para el fix:**
- El fix debe ser MÍNIMO — una línea si es posible
- Nunca cambies la arquitectura DDD ni el flujo del interactor
- Si el problema es un allowlist/enum en el modelo, agrégalo ahí — no cambies el valor en el interactor
- No inventes clases, gems ni métodos que no existan en el proyecto

Cuando tengas suficiente contexto, devuelve ÚNICAMENTE un JSON con esta estructura:
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

Si no podés proponer un fix concreto, omite "fix" y explicá en "diagnosis" qué necesita revisión manual.
"""

TOOLS = [
    {
        "name": "read_file",
        "description": "Lee el contenido completo de un archivo de uno de los 3 repos del proyecto.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Ruta relativa al root del repo. Ej: app/models/transaction.rb",
                },
                "repo": {
                    "type": "string",
                    "enum": ["api", "agents", "web"],
                    "description": "api=Rails backend, agents=Python Brain, web=React frontend",
                },
            },
            "required": ["path", "repo"],
        },
    },
    {
        "name": "list_files",
        "description": "Lista los archivos y carpetas de un directorio de uno de los 3 repos.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Ruta del directorio. Ej: app/domains/finanzas/interactors",
                },
                "repo": {
                    "type": "string",
                    "enum": ["api", "agents", "web"],
                },
            },
            "required": ["path", "repo"],
        },
    },
]

TOOL_MAP = {
    "read_file": lambda args: {
        "content": github_client.get_file(args["path"], repo=args.get("repo", "api")) or "File not found",
        "path": args["path"],
        "repo": args.get("repo", "api"),
    },
    "list_files": lambda args: {
        "files": github_client.list_files(args.get("path", ""), repo=args.get("repo", "api")) or [],
        "path": args.get("path", ""),
        "repo": args.get("repo", "api"),
    },
}


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


def _build_initial_message(payload: DebugPayload) -> str:
    stacktrace_str = "\n".join(payload.stacktrace[:20])
    return f"""## Error en producción

**Exception:** `{payload.exception_class}`
**Message:** {payload.message}
**Endpoint:** `{payload.http_method} {payload.endpoint}`
**Params:** `{json.dumps(payload.params, ensure_ascii=False)[:300]}`

## Stacktrace
```
{stacktrace_str}
```

Explorá el código con `read_file` y `list_files` hasta entender el root cause completo.
Cuando estés seguro, devolvé el JSON con diagnosis + fix."""


def handle(payload: DebugPayload) -> None:
    messenger = TelegramMessenger()
    try:
        messenger.send_message(
            f"🔍 <b>Analizando error...</b>\n"
            f"<code>{payload.exception_class}</code>\n"
            f"<code>{payload.http_method} {payload.endpoint}</code>"
        )

        provider = OpenAICompatibleLlmProvider(
            api_key=os.environ.get("OPENAI_API_KEY") or os.environ.get("OPEN_AI_API_KEY", ""),
            provider_name="openai",
            base_url="https://api.openai.com/v1",
            default_model="gpt-5.4",
        )
        result = _parse_response(provider.run_agent(
            system_prompt=SYSTEM_PROMPT,
            tools=TOOLS,
            tool_map=TOOL_MAP,
            initial_message=_build_initial_message(payload),
            max_iterations=10,
        ))

        if not result:
            messenger.send_message(
                f"⚠️ <b>No pude diagnosticar el error</b>\n"
                f"<code>{payload.exception_class}: {payload.message[:200]}</code>\n"
                f"Revisá los logs manualmente."
            )
            return

        diagnosis   = result.get("diagnosis", "Sin diagnóstico")
        fix         = result.get("fix")
        confidence  = result.get("confidence", "low")
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
