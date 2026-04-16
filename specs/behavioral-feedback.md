# Behavioral Feedback — daniel15k-agents

Fecha: 2026-04-15

## Objetivo

Hacer que el agente no se limite a confirmar operaciones.  
Cada interacción financiera debe devolver una lectura breve que ayude a Daniel a interpretar el movimiento en términos de conducta.

## Principio

El agente no reemplaza la autonomía del usuario.  
Su trabajo es:

- registrar bien
- clasificar bien
- interpretar con timing correcto
- meter una pequeña dosis de fricción o refuerzo cuando haya señal

## Implementado

### Chat en tiempo real

El `SYSTEM_PROMPT` de `agents/chat.py` ahora exige una lectura conductual mínima al confirmar una transacción:

- `discretionary` → nombrar que fue elegido / discrecional
- `investment` → nombrar que construye futuro
- `committed` → nombrar que es carga fija
- `necessary` → nombrar que sostiene / mantiene
- `social` → nombrar que es vínculo / social
- `income` → nombrar que es entrada

Regla:

- breve
- sin narrar herramientas
- sin proceso de razonamiento
- una sola respuesta final

Ejemplo válido:

`✅ Registrado: $14.000 en tamales. Fue discrecional.`

### Revisión nocturna

El prompt de `agents/nightly.py` ahora obliga a incluir una `lectura conductual` del día/mes:

- discretionary alto → fricción
- investment bajo → falta de construcción
- committed alto → presión estructural
- social visible → gasto relacional

Máximo:

- 2 bullets conductuales

## Qué NO hace todavía

- no mantiene memoria explícita de intervención pasada
- no cierra loops tipo `te dije esto ayer, hoy pasó esto`
- no genera automáticamente una acción futura agendada
- no aplica cooling-off real antes de comprar

## Regla de responsabilidad

### Agent

- deduplicación semántica
- interpretación conductual
- timing de la intervención
- preguntas de aclaración

### API

- persistencia
- idempotencia técnica
- exposición consistente de datos

## Siguiente evolución sugerida

### Quick wins

- respuestas con plantillas cortas según `category_type`
- refuerzo explícito si la transacción es `investment`
- fricción suave si la transacción es `discretionary`

### Mediano plazo

- helper interno `behavior_frame(category_type, monthly_context)`
- mensajes distintos según patrón:
  - repetido
  - aislado
  - escalando

### Estructural

- `Behavior Engine` separado:
  - input: transacciones + summary + contexto financiero
  - output: nudges / fricción / refuerzo / seguimiento
