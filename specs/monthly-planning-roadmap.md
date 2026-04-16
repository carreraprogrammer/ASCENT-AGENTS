# Monthly Planning Roadmap - daniel15k-agents

Fecha: 2026-04-16

## Objetivo

Traducir la investigacion de `deep-research-report.md` a un plan implementable sin intentar construir el sistema ideal completo de una sola vez.

La prioridad no es "hacer mas prompts". La prioridad es:

- detectar gaps reales
- construir un `monthly_plan` confiable
- usar ese plan para operar chat, nightly y presupuesto

---

## Problema actual

Hoy el sistema tiene estas piezas:

- `transactions`
- `budgets`
- `income_sources`
- `financial_context`
- `recurring_obligations`
- `debts`
- chat en tiempo real
- agente nocturno

Pero no tiene una pieza central que haga de contrato del mes.

Consecuencias:

- el wizard pregunta cosas sin un modelo estable detras
- `income_sources` existe pero no gobierna el presupuesto
- `projected transactions` terminan ocupando parte del rol de planeacion
- el sistema no sabe con que ingreso vivir este mes y que hacer con el extra

---

## Norte de producto

Daniel15K debe evolucionar de:

- motor que registra y responde

a:

- motor que detecta incompletitud
- pide el minimo dato de mayor impacto
- confirma un `monthly_plan`
- y luego interpreta transacciones contra ese plan

---

## MVP recomendado

No hace falta implementar todo el reporte para capturar el mayor valor.

### Pieza 1 - `monthly_financial_plan`

El sistema necesita una entidad propia del mes con:

- `mode`
- `base_budget_income`
- `expected_variable_income`
- `discretionary_limit`
- `overflow_rule`
- `reward_pct`
- `debt_strategy`
- `confirmed_at`

Sin esto, presupuesto, ingreso variable y coaching siguen mezclados.

### Pieza 2 - `income_profile` util

`income_sources` debe distinguir:

- `base`
- `variable`
- `seasonal`
- `one_time`

`is_variable` solo no alcanza.

### Pieza 3 - `completeness_state` minimo

No hace falta un grafo gigante para empezar. Alcanzan estas dimensiones:

- `income_profile`
- `debts`
- `recurring_expenses`
- `strategy`
- `monthly_plan`

Estados minimos:

- `missing`
- `partial`
- `sufficient`
- `stale`

### Pieza 4 - `agent preflight`

Antes de responder, el agente decide:

- responder normal
- abrir wizard bloqueante
- lanzar soft nudge

### Pieza 5 - wizard corto y contextual

El wizard ya no debe ser una encuesta quincenal fija. Debe destrabar lo minimo para:

- crear el plan del mes
- confirmar el plan
- corregir el plan

---

## Orden de implementacion

### Paso 1 - API

- crear `monthly_financial_plans`
- ampliar `income_sources`
- exponer endpoints de generate/confirm/current
- dejar de depender de campos legacy en `summary`

### Paso 2 - Brain

- reescribir `budget_wizard.py` como wizard de `monthly_plan`
- agregar `preflight`
- agregar deteccion minima de gaps
- hacer que chat y nightly lean el plan actual

### Paso 3 - UI

- mostrar el plan del mes como contrato visible
- separar claramente:
  - presupuesto
  - cashflow esperado
  - ingreso extra
  - overflow

---

## Criterio de exito del MVP

El MVP se considera logrado cuando este caso funciona limpio:

- ingreso base confiable: `6.400.000`
- ingreso variable posible: `2.900.000`

Resultado esperado:

- el presupuesto del mes se construye con `6.4M`
- los `2.9M` no inflan el presupuesto base
- si entran, se procesan como `overflow`
- el agente puede explicarlo y operar sobre eso

---

## Lo que queda fuera de este MVP

Queda para despues:

- `behavior_profile_v2` completo
- experimentacion avanzada de nudges
- logs detallados de intervenciones
- motor de if-then rules complejo
- medicion formal de bienestar financiero

Eso sigue importando, pero viene despues de cerrar la base mensual.

---

## Regla arquitectonica

La planeacion mensual no debe volver a vivir implicitamente en:

- `financial_context` legacy
- `projected transactions`
- prompts ad hoc del agente

Debe vivir en una capa explicita y versionada:

- `monthly_financial_plan`

Esa es la pieza que convierte la investigacion en producto operable.
