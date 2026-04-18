# Income Source Schedules in Brain

Fecha: 2026-04-17

## Objetivo

Mantener el Brain compatible con el modelo nuevo de ingresos sin romper los flujos conversacionales existentes.

## Qué cambió

La API ahora puede devolver `income_sources` con:

- campos agregados legacy (`expected_day_from`, `expected_day_to`, `expected_amount`)
- colección `schedules`

## Decisión

En esta fase el Brain:

- ya lee `schedules`
- ya crea `income_sources` con `cadence + schedules`
- dejó de depender del wizard legacy de un solo rango

## Impacto concreto

`budget_wizard.py` ahora:

- no asume una sola ventana por ingreso
- muestra el breakdown de ventanas si el source trae `schedules`

`rails_api.py` y `rails_http.py` ahora:

- aceptan `cadence`
- aceptan `schedules`
- aceptan `notes`

`income_wizard.py` ahora:

- pregunta cadencia explícita
- pide el monto por evento cuando la cadencia es quincenal o semanal
- calcula el total mensual internamente antes de persistir `expected_amount`
- soporta mensual / quincenal / semanal / irregular
- crea una sola fuente por ingreso, aunque tenga varias ventanas
- pide confiabilidad solo para el ingreso variable

## Estado

- lectura del modelo nuevo: sí
- escritura del modelo nuevo desde el Brain: sí

## Deuda restante

El Brain sigue conviviendo con campos agregados legacy (`expected_day_from`, `expected_day_to`, `expected_amount`) porque el API todavía los usa como denormalización de lectura.

Eso ya no bloquea:

- perfil de ingresos
- budget wizard
- monthly planning
