"""El manifiesto de Ascent como cromo: GET /.well-known/cromo.json (TCW-1 fase 3 y 4).

El cerebro lo lee al registrar el cromo por URL. resource es el recurso canónico: el aud que Ascent exige en los
tokens. space es el space del cerebro donde Ascent puede escribir (su consolidado diario).

demo (ASC-4) documenta el contrato del claim demo: cuando el token intercambiado trae "demo": true (el cerebro lo
agrega si el intercambio pide demo=true o si el interruptor "Mostrar con datos de ejemplo" está activo para este
cromo), todas las herramientas ascent.* responden con los datos ficticios de la cuenta demo, en solo lectura. El
cerebro valida que claim sea "demo" (cyborg-brain-server, brain_server/cromos.py, ADR-0015).
"""
from cromo.tokens import ASCENT_RESOURCE

MANIFEST = {
    "slug": "ascent",
    "name": {"es": "Ascent", "en": "Ascent"},
    "description": {
        "es": "Finanzas personales: lee tus gastos, encuentra patrones y arma tu presupuesto.",
        "en": "Personal finance: reads your spending, finds the patterns and builds your budget.",
    },
    "mcp_url": ASCENT_RESOURCE,
    "resource": ASCENT_RESOURCE,
    "scopes": ["ascent:read", "ascent:write"],
    "space": "ascent",
    "status": "available",
    # TCW-2 (cerebro, ADR-0016): lo que muestra la página de Cromos. Las herramientas son las de cromo/server.py.
    "app_url": "https://ascent.thecyborgway.dev",
    "tools": [
        {"name": "resumen_mes", "access": "read",
         "description": {"es": "Ingresos, gastos y margen del mes.", "en": "The month's income, spending and margin."}},
        {"name": "gasto_por_categoria", "access": "read",
         "description": {"es": "Cuánto va cada categoría contra su presupuesto.",
                         "en": "How each category is doing against its budget."}},
        {"name": "obligaciones_proximas", "access": "read",
         "description": {"es": "Pagos recurrentes que vencen pronto.", "en": "Recurring payments coming due."}},
        {"name": "ultimos_movimientos", "access": "read",
         "description": {"es": "Los movimientos más recientes.", "en": "The most recent transactions."}},
        {"name": "registrar_gasto", "access": "write",
         "description": {"es": "Registra un gasto en Ascent.", "en": "Records an expense in Ascent."}},
    ],
    "scope_descriptions": {
        "ascent:read": {"es": "Leer tus finanzas en Ascent: resumen, categorías, obligaciones y movimientos.",
                        "en": "Read your finances in Ascent: summary, categories, obligations and transactions."},
        "ascent:write": {"es": "Registrar gastos en Ascent en tu nombre.",
                         "en": "Record expenses in Ascent on your behalf."},
    },
    "demo": {
        "claim": "demo",
        "effect": {
            "es": "Las herramientas responden con los datos ficticios de la cuenta demo, en solo lectura.",
            "en": "The tools answer with the demo account's fictional data, read only.",
        },
    },
}
