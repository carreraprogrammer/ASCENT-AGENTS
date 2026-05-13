# DANIEL 15K — Brain

> La capa de inteligencia del sistema. Orquesta, razona y actúa. Rails persiste. El Brain decide.

FastAPI corriendo en Railway. Recibe mensajes de Telegram vía webhook, corre agentes LLM con tool use, y programa automatizaciones nocturnas y quincenales con APScheduler — sin GitHub Actions.

## Multi-LLM

Abstracción `LlmProviderPort` con adapters para Anthropic, OpenAI-compatible (GPT, DeepSeek, Kimi).

**Provider activo en producción: `openai` (DeepSeek V3 via `deepseek-chat`)**

Benchmarks realizados (ver `research/llm-providers/`):

- Kimi k2.6: descartado — alucinaciones en tool use, retry loops, inestable en producción
- DeepSeek V3: aprobado — tool calling robusto, costo bajo, respuestas en español coloquial correcto
- GPT-4.1-mini: aprobado como alternativa de respaldo

Configuración activa:

```env
LLM_PROVIDER=openai
LLM_MODEL=deepseek-chat
OPENAI_API_KEY=sk-...        # apunta a api.deepseek.com/v1 via base_url
```

Sin fallback automático entre providers. Sin selector por usuario.

## Capacidades financieras expuestas

**Lectura:**
- `summary`, `safe_to_deploy`, `completeness`
- deudas, ingresos, obligaciones recurrentes, planned_expenses, sinking_funds, savings_goals
- historial de transacciones + pending + structural_match

**Escritura:**
- crear/actualizar transacciones, deudas, recurring_obligations, planned_expenses, sinking_funds
- confirmar/cerrar plan mensual
- crear milestones y marcar paid_off

**Canal web:**
- `POST /api/v1/agents/chat` — recibe mensaje con `source: "web"`, corre el agente, emite eventos UI
- `emit_ui_event` (no `send_telegram`) cuando `source == "web"`

**Reglas operativas (no violar):**

| Entidad | Cuándo usar |
|---------|------------|
| `transactions` | ya ocurrió |
| `recurring_obligations` | impacto mensual fijo en caja |
| `debts` | estado estructural del pasivo |
| `planned_expenses` | gasto futuro previsible, no mensual |
| `sinking_funds` | reserva acumulativa para fondear un planned_expense |

- `creditos` en RecurringObligation **exige** `source_type=Debt` — no crear sin deuda asociada
- no registrar como transacción algo que es solo gasto futuro previsible
- para desvincular deuda ↔ obligación: `update_recurring_obligation` con `source_type=null, source_id=null`

---

## Rol en la arquitectura

```
Telegram             Web App (Ionic PWA)
    ↕ webhook              ↕ POST /agents/chat
FastAPI — daniel15k-agents  (este repo)
    ├── /webhook/telegram      ← entrada Telegram + callbacks
    ├── /agents/chat           ← entrada web con source:"web"
    ├── /agents/nightly        ← trigger manual revisión nocturna
    ├── /agents/planning       ← trigger manual planificación quincenal
    └── scheduler              ← APScheduler: cron interno
    ↕  REST + service_account delegation
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
Step 5 → propone presupuesto para gastos flexibles
Step 6 → define % de recompensa si cumple el plan
Step 7 → muestra plan de flujo de caja completo para aprobación
Step 8 → guarda presupuestos vía Rails API
```

El estado persiste entre mensajes en `PendingAction.context` (jsonb en Postgres). Si el usuario no responde en 48h, el PendingAction expira automáticamente.

**Estado:** ✅ Construido. ⏳ Pendiente de validación con datos reales (~20/04/2026, segunda quincena EMAPTA).

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

Configuración activa en producción:

```env
LLM_PROVIDER=openai
LLM_MODEL=deepseek-chat
```

Cómo probar localmente:

```bash
source .env
LLM_PROVIDER=openai LLM_MODEL=deepseek-chat ./.venv/bin/python scripts/smoke_llm.py
LLM_PROVIDER=openai LLM_MODEL=gpt-4.1-mini ./.venv/bin/python scripts/smoke_llm.py
```

---

## Endpoints disponibles

```
GET  /health                    → {"ok": true, "service": "daniel15k-agents"}
POST /webhook/telegram          → entrada del webhook de Telegram
POST /agents/chat               → canal web (source: "web") → emite agent_ui_events
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
