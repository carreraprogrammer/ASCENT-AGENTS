# DANIEL 15K — Brain

> La capa de inteligencia del sistema. Orquesta, razona y actúa. Rails persiste. El Brain decide.

FastAPI corriendo en Railway. Recibe mensajes de Telegram vía webhook, corre agentes LLM con tool use, y programa automatizaciones nocturnas y quincenales con APScheduler — sin GitHub Actions.

## Multi-LLM Phase Status

Implementado en esta fase:

- abstracción mínima `LlmProviderPort`
- provider Anthropic extraído como adapter concreto
- provider OpenAI-compatible para GPT
- provider OpenAI-compatible para Kimi
- selección de provider/model por `LLM_PROVIDER` y `LLM_MODEL`
- compatibilidad con nombres legacy `OPEN_AI_API_KEY` y `KIMI_AI_API_KEY`

Validación real de esta fase:

- OpenAI: probado localmente con tool calling real usando `gpt-4.1-mini`
- Kimi: probado localmente con tool calling real usando `kimi-k2.5`
- Anthropic: compatibilidad preservada por adapter dedicado, pero sin prueba runtime en esta iteración por falta de crédito disponible

Estado operativo elegido:

- provider por defecto recomendado para esta fase: `kimi`
- modelo por defecto recomendado para esta fase: `kimi-k2.5`

Límites explícitos:

- no hay fallback automático entre providers
- no hay benchmark automático
- no hay Gemini en esta fase
- no hay selector persistido por usuario ni UI de preferencias de modelo

## Capacidades financieras expuestas hoy

- leer `summary`, deudas, ingresos y obligaciones recurrentes
- crear y actualizar `recurring_obligations`
- leer, crear y actualizar `planned_expenses`
- consultar relación deuda ↔ obligación recurrente vía `source_type/source_id`
- quitar vínculo deuda ↔ obligación recurrente vía `update_recurring_obligation`

Regla operativa:

- flujo mensual → `recurring_obligations`
- estado del pasivo → `debts`
- planeación futura → `planned_expenses`

El Brain no debe registrar como transacción algo que todavía es solo gasto futuro previsible.

Para desvincular una deuda de una obligación recurrente, el contrato operativo es:

- `update_recurring_obligation`
- `source_type = null`
- `source_id = null`

---

## Rol en la arquitectura

```
Telegram
    ↕  webhook / send message
FastAPI — daniel15k-agents  (este repo)
    ├── /webhook/telegram    ← entrada de mensajes y callbacks
    ├── /agents/nightly      ← trigger manual de revisión nocturna
    ├── /agents/planning     ← trigger manual de planificación quincenal
    └── scheduler            ← APScheduler: cron interno
    ↕  REST + account delegation
Rails API — daniel15k-api   (data layer)
```

**Principio:** Rails no sabe nada del provider LLM ni de Telegram. El Brain orquesta, Rails persiste.

---

## Estructura

```
daniel15k-agents/
├── main.py                    ← FastAPI app con lifespan (arranca/para scheduler)
├── scheduler.py               ← APScheduler: 3 jobs programados
├── ports/
│   ├── rails_api.py           ← interfaz abstracta hacia la API de Rails
│   ├── messenger.py           ← interfaz abstracta hacia el mensajero
│   └── llm_provider.py        ← interfaz mínima del provider LLM
├── adapters/
│   ├── rails_http.py          ← implementación HTTP del puerto Rails
│   ├── telegram_messenger.py  ← implementación Telegram del puerto mensajero
│   ├── anthropic_llm.py       ← adapter Anthropic
│   └── openai_compatible_llm.py ← adapter GPT/Kimi
├── services/
│   ├── claude_client.py       ← wrapper legacy-compatible para Anthropic
│   └── llm_factory.py         ← resuelve provider/model desde env
├── agents/
│   └── nightly.py             ← revisión nocturna (migrado desde GitHub Actions)
├── flows/
│   └── budget_wizard.py       ← máquina de estados del wizard de planificación
├── scripts/
│   └── smoke_llm.py           ← prueba real de provider + tool calling
├── routers/
│   ├── webhook.py             ← POST /webhook/telegram
│   └── agents.py             ← POST /agents/nightly, /agents/planning
├── Dockerfile
├── railway.toml
└── requirements.txt
```

---

## Arquitectura hexagonal

El Brain usa ports & adapters (arquitectura hexagonal):

- **Ports** (`ports/`) — interfaces abstractas. Los agentes y servicios solo conocen los ports.
- **Adapters** (`adapters/`) — implementaciones concretas. Hoy: HTTP + Telegram. Mañana: pueden cambiar sin tocar los agentes.
- **Services** (`services/`) — wiring transversal (factory, compatibilidad, prompts).
- **Agents** (`agents/`) — lógica de negocio de cada agente autónomo.
- **Flows** (`flows/`) — máquinas de estado para flujos interactivos multi-paso.

```python
# Los agentes dependen de abstracciones, no de implementaciones
def run_nightly(api: RailsApiPort, messenger: MessengerPort):
    ...

# Los adapters se inyectan en main.py
api = RailsHttpAdapter(base_url=..., token=...)
messenger = TelegramMessenger(token=..., chat_id=...)
run_nightly(api, messenger)
```

---

## Scheduler (reemplaza GitHub Actions)

APScheduler corre dentro del mismo proceso de FastAPI:

| Job | Cron (UTC) | Hora Colombia | Qué hace |
|-----|-----------|--------------|----------|
| Revisión nocturna | `0 4 * * *` | 11pm | Registra gastos, analiza, envía resumen |
| Planificación día 1 | `0 13 1 * *` | 8am día 1 | Inicia wizard de presupuesto quincenal |
| Planificación día 15 | `0 13 15 * *` | 8am día 15 | Inicia wizard de presupuesto quincenal |
| Expirar PendingActions | `0 * * * *` | cada hora | Cancela flujos abiertos que vencieron |

---

## Webhook de Telegram

`POST /webhook/telegram` — lógica de despacho:

```
recibe update de Telegram
  → ¿es callback_query? (botón presionado)
      → delegar a callback_handler
  → ¿hay PendingAction activo para este usuario?
      sí → delegar a budget_wizard con el mensaje
      no → flujo conversacional en tiempo real
```

El webhook responde a Telegram en < 2 segundos (answerCallbackQuery inmediato, procesamiento en background).

---

## Revisión nocturna (`agents/nightly.py`)

Equivalente al `revision_nocturna.py` original pero usando ports. Cada noche:

1. `get_summary` → estado financiero del mes (balance, burn_rate, deudas)
2. `get_telegram_messages` → respaldo/conciliación de mensajes del día
3. `get_gmail_emails` → correos bancarios (Davivienda, Nequi) vía IMAP directo
4. `get_transactions` + `get_pending_transactions` → historial y pendientes de aclaración
5. `create_transaction` → registra cada gasto nuevo detectado
6. `get_balance` → balance actualizado tras crear transacciones
7. `send_telegram` → envía resumen con coaching en lenguaje coloquial colombiano

**Estado:** ✅ Funcionando en Railway desde el 13/04/2026. GitHub Actions cron deshabilitado.

---

## Wizard de planificación quincenal (`flows/budget_wizard.py`)

Máquina de estados de 8 pasos. Se activa el día 1 y día 15 de cada mes:

```
Step 0 → pregunta si quiere planificar ("Sí / No / Mañana")
Step 1 → confirma ingresos esperados del período
Step 2 → muestra gastos comprometidos (arriendo, créditos, obligaciones fijas)
Step 3 → recomienda abono extra a deuda (estrategia snowball/avalanche)
Step 4 → propone presupuesto para gastos necesarios
Step 5 → propone presupuesto para gastos discrecionales
Step 6 → define % de recompensa si cumple el plan
Step 7 → muestra plan de flujo de caja completo para aprobación
Step 8 → guarda presupuestos vía Rails API
```

El estado persiste entre mensajes en `PendingAction.context` (jsonb en Postgres). Si el usuario no responde en 48h, el PendingAction expira automáticamente.

**Estado:** ✅ Construido. ⏳ Pendiente de validación con datos reales (~20/04/2026, segunda quincena Acme).

---

## Variables de entorno (Railway)

| Variable | Descripción |
|----------|-------------|
| `DANIEL15K_API_URL` | URL de la API Rails en Railway |
| `DANIEL15K_API_TOKEN` | JWT legacy de compatibilidad |
| `DANIEL15K_SERVICE_TOKEN` | Token del `service_account` del Brain |
| `DANIEL15K_ACCOUNT_ID` | Cuenta objetivo sobre la que actúa el Brain |
| `DANIEL15K_AGENT_TYPE` | Tipo de agente. Default: `finance_coach` |
| `TELEGRAM_BOT_TOKEN` | Token del bot |
| `TELEGRAM_CHAT_ID` | ID del chat personal de Daniel |
| `LLM_PROVIDER` | Provider activo: `anthropic`, `openai` o `kimi` |
| `LLM_MODEL` | Modelo activo del provider |
| `ANTHROPIC_API_KEY` | API key de Anthropic |
| `OPENAI_API_KEY` | API key estándar de OpenAI |
| `OPEN_AI_API_KEY` | Alias legacy aceptado para OpenAI |
| `KIMI_API_KEY` | API key estándar de Kimi |
| `KIMI_AI_API_KEY` | Alias legacy aceptado para Kimi |
| `CLAUDE_MODEL` | Fallback legacy para Anthropic si no defines `LLM_MODEL` |
| `GMAIL_ADDRESS` | Correo donde llegan extractos bancarios |
| `GMAIL_APP_PASSWORD` | App password de Google (16 chars) |
| `INTERNAL_TOKEN` | Token para autenticar triggers manuales (`/agents/*`) |
| `PORT` | Asignado automáticamente por Railway |

Resolución efectiva:

- si `LLM_PROVIDER` no está seteado, el Brain intenta resolver según keys disponibles en este orden: `kimi`, `openai`, `anthropic`
- si `LLM_MODEL` no está seteado, usa un default seguro por provider
- en OpenAI y Kimi se aceptan tanto nombres estándar como aliases legacy del `.env`

Configuración recomendada para esta fase:

```env
LLM_PROVIDER=kimi
LLM_MODEL=kimi-k2.5
```

Cómo probar localmente:

```bash
source .env
./.venv/bin/python -c "from services.llm_factory import resolve_llm_provider_name, resolve_llm_model; print(resolve_llm_provider_name(), resolve_llm_model())"
LLM_PROVIDER=openai LLM_MODEL=gpt-4.1-mini ./.venv/bin/python scripts/smoke_llm.py
LLM_PROVIDER=kimi LLM_MODEL=kimi-k2.5 ./.venv/bin/python scripts/smoke_llm.py
```

Cómo comparar providers con el mismo brain:

1. Mantén iguales prompts, tools y contexto.
2. Cambia solo `LLM_PROVIDER` y `LLM_MODEL`.
3. Corre el mismo `scripts/smoke_llm.py` o el mismo flujo real del Brain.
4. Compara costo, velocidad, tool use y calidad de respuesta por logs/resultados.

La selección por UI queda fuera de esta fase.

---

## Endpoints disponibles

```
GET  /health                    → {"ok": true, "service": "daniel15k-agents"}
POST /webhook/telegram          → entrada del webhook de Telegram
POST /agents/nightly            → trigger manual de revisión nocturna
POST /agents/planning           → trigger manual de planificación quincenal
```

`/agents/*` requieren `Authorization: Bearer <INTERNAL_TOKEN>`.

---

## Deploy

El Brain vive en Railway. Cada push a `main` redespliega automáticamente.

```bash
# Trigger manual desde local
curl -X POST https://daniel15k-agents-production.up.railway.app/agents/nightly \
  -H "Authorization: Bearer $INTERNAL_TOKEN"

# Ver logs
railway logs --tail 100

# Ver variables de entorno
railway run printenv | grep -E "(RAILS|TELEGRAM|LLM_|ANTHROPIC|OPENAI|KIMI)"
```

---

## Aprendizajes de construcción

**Railway asigna el puerto dinámicamente**
No hardcodear el puerto. Usar `${PORT:-8000}` en el Dockerfile CMD. Railway inyecta `$PORT` en runtime.

**`{"data": null}` es un dict truthy en Python**
`if pending_action:` no funciona cuando Rails devuelve `{"data": null}`. Usar `data.get("data") or None` para colapsar null correctamente.

**Tool calls requieren positional arg, no keyword expansion**
`tool_map[name](**tool_input)` falla si `tool_input` es `{}` (no hay parámetro self implícito). Usar `tool_map[name](tool_input)` pasando el dict como argumento posicional.

**Delegación por account**
El Brain ahora puede autenticarse como `service_account` usando `DANIEL15K_SERVICE_TOKEN` + `DANIEL15K_ACCOUNT_ID` + `DANIEL15K_AGENT_TYPE`. Si esas variables no existen, hace fallback al `DANIEL15K_API_TOKEN` legacy para no romper operación.

**answerCallbackQuery tiene ventana de ~10 segundos**
Si el Brain no responde al callback de Telegram en ese tiempo, el botón queda girando. La solución: responder inmediatamente con `answerCallbackQuery` y procesar en background.

**Arquitectura hexagonal para testabilidad**
Los agentes no saben si están hablando con Railway real o un mock. Inyectar los adapters desde `main.py` permite swapear implementaciones para tests o modo de desarrollo sin tocar la lógica de negocio.
