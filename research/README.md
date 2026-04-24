# Research & A/B Testing

Documentación de experimentos, benchmarks y comparaciones para el agente financiero.

---

## Estructura

```
research/
└── llm-providers/     — Comparaciones de modelos LLM
```

## LLM Providers

| Fecha | Modelo | Proveedor | Veredicto |
|---|---|---|---|
| 2026-04-23 | kimi-k2.6 | Moonshot AI | ❌ Alucinación + retry loop |
| — | gpt-4.1-mini | OpenAI | En evaluación |

## Convenciones

- Un archivo por modelo/experimento, nombrado `YYYY-MM-DD-nombre-modelo.md`
- Incluir siempre: configuración usada, tests realizados, tabla de resultados y veredicto
- Durante tests: incluir "TEST" en el concepto de transacciones de prueba para identificarlas fácilmente
