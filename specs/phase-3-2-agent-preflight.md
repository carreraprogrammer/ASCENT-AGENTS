# Fase 3.2 - Agent Preflight en el Brain

Fecha: 2026-04-17

## Objetivo

Hacer que el Brain no responda a intents de planeación "a ciegas".

## Qué quedó implementado

- heurística mínima de detección de intent:
  - `budgeting`
  - `monthly_status`
  - `overflow`
- llamada a `POST /api/v1/agents/preflight`
- decisión local:
  - `allow`
  - `soft_nudge`
  - `block`

## Comportamiento

### `block`

Si el backend devuelve bloqueo y pide `budget_planning`:

- el chat no improvisa
- dispara el wizard mensual

### `soft_nudge`

Si el backend devuelve nudge:

- el chat responde igual
- pero recibe instrucción explícita de cerrar con recordatorio breve

### `allow`

- flujo normal

## Ajuste adicional incluido

El webhook ahora reenvía mensajes de texto al wizard activo cuando existe un `PendingAction` de:

- `budget_planning`
- `financial_context_setup`

Eso evita que respuestas por texto se vayan por error al chat general.

## Criterios de aceptación cubiertos

- el agente hace `preflight` antes de intents sensibles
- decide entre flujo normal, wizard y nudge
- el wizard puede retomarse desde el chat sin duplicar lógica
