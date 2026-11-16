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
    "demo": {
        "claim": "demo",
        "effect": {
            "es": "Las herramientas responden con los datos ficticios de la cuenta demo, en solo lectura.",
            "en": "The tools answer with the demo account's fictional data, read only.",
        },
    },
}
