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

- sigue aceptando el contrato legacy para crear ingresos simples
- ya puede leer y mostrar `schedules` cuando existen

No se reescribió por completo `income_wizard.py`; se mantuvo compatibilidad para no mezclar dos migraciones al mismo tiempo.

## Impacto concreto

`budget_wizard.py` ahora:

- no asume una sola ventana por ingreso
- muestra el breakdown de ventanas si el source trae `schedules`

`rails_api.py` y `rails_http.py` ahora:

- aceptan `cadence`
- aceptan `schedules`
- aceptan `notes`

## Estado

- lectura del modelo nuevo: sí
- escritura avanzada desde Brain: compatible, pero todavía no explotada por el wizard legacy

## Siguiente paso opcional

Si luego se quiere endurecer el flujo conversacional de ingresos, `income_wizard.py` debería pasar de:

- pedir un único rango

a:

- construir un `income_source` con `schedules`

Pero eso ya no bloquea planificación mensual ni UI.
