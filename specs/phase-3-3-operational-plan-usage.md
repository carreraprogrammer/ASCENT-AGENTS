# Fase 3.3 - Uso operativo del plan en agentes

Fecha: 2026-04-17

## Objetivo

Hacer que chat y nightly lean el `monthly_plan` como contrato del mes y no como dato decorativo.

## Qué quedó implementado

- el chat ya está instruido para usar `get_summary` en:
  - presupuesto
  - resumen del mes
  - ingreso extra / overflow
- el nightly ya está instruido para leer `overflow_status`
- ambos agentes reciben `monthly_plan` y `overflow_status` desde `summary`

## Regla de comportamiento

- ingreso extra != permiso para subir el presupuesto base
- si `overflow_status.status == available`, el agente debe nombrar:
  - cuánto extra ya entró
  - hacia dónde debería ir según el plan

## Lo que todavía no entra

- aplicación automática del overflow
- tracking explícito de si el usuario obedeció la regla
- intervención por patrón histórico de overflow
