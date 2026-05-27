"""
agents/debugger.py — Sub-agente que diagnostica errores y deja que el usuario
elija la estrategia de deploy.

Flujo:
  1. Recibe el payload del error (stacktrace, endpoint, params)
  2. Explora los 3 repos con read_file / list_files hasta entender el root cause
  3. Diagnostica: user_story + why_it_happened + root cause técnico + fix
  4. Envía resumen por Telegram con opciones:
       [🚀 Hotfix a main]  [🔍 Abrir PR]  [💬 Preguntar]  [🚫 Ignorar]
  5. Hotfix → push directo a main → Railway despliega automáticamente
     PR      → branch nueva + PR para revisión detallada
     Preguntar → conversación con el agente que analizó el error
     Ignorar → descarta sin acción
"""

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any

from adapters.openai_compatible_llm import OpenAICompatibleLlmProvider
from adapters.telegram_messenger import TelegramMessenger
from services import github_client

logger = logging.getLogger(__name__)

COLOMBIA_TZ = timezone(timedelta(hours=-5))

# error_id → contexto completo — persiste en memoria hasta que el usuario responda
PENDING_FIXES: dict[int, dict] = {}

# error_id del error sobre el que el usuario está haciendo preguntas (None si no hay sesión activa)
_ACTIVE_QUESTION_SESSION: int | None = None


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
  "user_story": "qué intentaba hacer el usuario o el agente cuando falló, en lenguaje llano (1-2 oraciones, sin jerga técnica)",
  "why_it_happened": "por qué falló, en lenguaje claro y directo — sin tecnicismos (1-2 oraciones)",
  "diagnosis": "root cause técnico preciso: qué clase/archivo/validación lo causó (1-2 oraciones técnicas)",
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
    occurred_at: str = ""


def _format_time(occurred_at: str) -> str:
    if not occurred_at:
        return "hora desconocida"
    try:
        dt = datetime.fromisoformat(occurred_at).astimezone(COLOMBIA_TZ)
        return dt.strftime("%-d/%-m a las %-I:%M %p").replace("AM", "am").replace("PM", "pm")
    except Exception:
        return occurred_at[:16]


def _make_provider() -> OpenAICompatibleLlmProvider:
    return OpenAICompatibleLlmProvider(
        api_key=os.environ.get("OPENAI_API_KEY") or os.environ.get("OPEN_AI_API_KEY", ""),
        provider_name="openai",
        base_url="https://api.openai.com/v1",
        default_model="gpt-5.4",
    )


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
Cuando estés seguro, devolvé el JSON con user_story + why_it_happened + diagnosis + fix."""


def handle(payload: DebugPayload) -> None:
    messenger = TelegramMessenger()
    try:
        messenger.send_message(
            f"🔍 <b>Analizando error...</b>\n"
            f"<code>{payload.exception_class}</code>\n"
            f"<code>{payload.http_method} {payload.endpoint}</code>"
        )

        result = _parse_response(_make_provider().run_agent(
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

        user_story     = result.get("user_story", "")
        why_happened   = result.get("why_it_happened", "")
        diagnosis      = result.get("diagnosis", "Sin diagnóstico")
        fix            = result.get("fix")
        confidence     = result.get("confidence", "low")
        conf_reason    = result.get("confidence_reason", "")
        time_str       = _format_time(payload.occurred_at)

        PENDING_FIXES[payload.error_id] = {
            "payload":       payload,
            "fix":           fix,
            "diagnosis":     diagnosis,
            "user_story":    user_story,
            "why_happened":  why_happened,
        }

        confidence_emoji = {"high": "🟢", "medium": "🟡", "low": "🔴"}.get(confidence, "⚪")
        confidence_label = {"high": "alta", "medium": "media", "low": "baja"}.get(confidence, confidence)

        user_story_block   = f"\n\n👤 <b>¿Qué pasó?</b>\n{user_story}" if user_story else ""
        why_happened_block = f"\n\n❓ <b>¿Por qué?</b>\n{why_happened}" if why_happened else ""

        if fix:
            fix_block = (
                f"\n\n🔧 <b>Fix propuesto</b>\n"
                f"<code>{fix.get('file', '')}</code>\n"
                f"{fix.get('description', '')}"
            )
            buttons = [[
                {"text": "🚀 Hotfix a main", "callback_data": f"debug:hotfix:{payload.error_id}"},
                {"text": "🔍 Abrir PR",      "callback_data": f"debug:pr:{payload.error_id}"},
            ], [
                {"text": "💬 Preguntar",     "callback_data": f"debug:ask:{payload.error_id}"},
                {"text": "🚫 Ignorar",       "callback_data": f"debug:reject:{payload.error_id}"},
            ]]
        else:
            fix_block = "\n\n<i>No hay fix automático — requiere revisión manual.</i>"
            buttons = [[
                {"text": "💬 Preguntar", "callback_data": f"debug:ask:{payload.error_id}"},
                {"text": "🚫 Ignorar",   "callback_data": f"debug:reject:{payload.error_id}"},
            ]]

        messenger.send_with_buttons(
            text=(
                f"🐛 <b>Error detectado</b> — {time_str}"
                f"{user_story_block}"
                f"{why_happened_block}\n\n"
                f"📋 <b>Técnico</b>\n"
                f"<code>{payload.exception_class}</code> en <code>{payload.http_method} {payload.endpoint}</code>\n"
                f"{diagnosis}"
                f"{fix_block}\n\n"
                f"{confidence_emoji} Confianza <b>{confidence_label}</b> — {conf_reason}"
            ),
            buttons=buttons,
        )

    except Exception as e:
        logger.error("[debugger] Unexpected error: %s", e, exc_info=True)
        messenger.send_message(f"❌ <b>Debugger falló</b>: {e}")


def handle_hotfix(error_id: int) -> None:
    global _ACTIVE_QUESTION_SESSION
    messenger = TelegramMessenger()
    pending = PENDING_FIXES.pop(error_id, None)
    if error_id == _ACTIVE_QUESTION_SESSION:
        _ACTIVE_QUESTION_SESSION = None

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
    global _ACTIVE_QUESTION_SESSION
    messenger = TelegramMessenger()
    pending = PENDING_FIXES.pop(error_id, None)
    if error_id == _ACTIVE_QUESTION_SESSION:
        _ACTIVE_QUESTION_SESSION = None

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
    global _ACTIVE_QUESTION_SESSION
    PENDING_FIXES.pop(error_id, None)
    if error_id == _ACTIVE_QUESTION_SESSION:
        _ACTIVE_QUESTION_SESSION = None
    TelegramMessenger().send_message(f"🚫 Error #{error_id} ignorado.")


def handle_ask(error_id: int) -> None:
    global _ACTIVE_QUESTION_SESSION
    pending = PENDING_FIXES.get(error_id)
    if not pending:
        TelegramMessenger().send_message("⚠️ Este error ya no está en memoria.")
        return
    _ACTIVE_QUESTION_SESSION = error_id
    TelegramMessenger().send_message(
        f"💬 <b>Preguntale al agente sobre el error #{error_id}</b>\n\n"
        f"Escribí tu pregunta directamente. El agente tiene todo el contexto del análisis.\n\n"
        f"<i>Cuando termines, usá los botones del mensaje anterior para tomar acción.</i>"
    )


def handle_question(question: str) -> None:
    """Responde una pregunta sobre el error activo usando el contexto del análisis previo."""
    global _ACTIVE_QUESTION_SESSION
    messenger = TelegramMessenger()

    error_id = _ACTIVE_QUESTION_SESSION
    if error_id is None:
        return

    pending = PENDING_FIXES.get(error_id)
    if not pending:
        _ACTIVE_QUESTION_SESSION = None
        messenger.send_message("⚠️ El contexto del error ya no está en memoria.")
        return

    payload: DebugPayload = pending["payload"]
    fix = pending.get("fix") or {}

    context = (
        f"Contexto del error analizado (error_id={error_id}):\n\n"
        f"- Excepción: {payload.exception_class}: {payload.message[:200]}\n"
        f"- Endpoint: {payload.http_method} {payload.endpoint}\n"
        f"- Historia de usuario: {pending.get('user_story', '')}\n"
        f"- Por qué ocurrió: {pending.get('why_happened', '')}\n"
        f"- Diagnóstico técnico: {pending.get('diagnosis', '')}\n"
        f"- Fix propuesto: {fix.get('description', 'Ninguno')} en {fix.get('file', '')}\n\n"
        f"El desarrollador pregunta: {question}\n\n"
        f"Responde en español, de forma clara y directa. "
        f"Podés mezclar lenguaje de negocio y técnico según lo que requiera la pregunta. "
        f"Si la pregunta requiere ver código específico, indicá el archivo y qué buscar."
    )

    try:
        provider = _make_provider()
        answer = provider.simple_complete(
            messages=[{"role": "user", "content": context}],
            system=SYSTEM_PROMPT,
            max_tokens=800,
        )
        messenger.send_message(f"💬 {answer[:2000]}")
    except Exception as e:
        logger.error("[debugger] handle_question failed: %s", e, exc_info=True)
        messenger.send_message(f"❌ No pude responder: {e}")


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
