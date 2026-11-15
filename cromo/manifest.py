"""El manifiesto de Ascent como cromo: GET /.well-known/cromo.json (TCW-1 fase 3 y 4).

El cerebro lo lee al registrar el cromo por URL. resource es el recurso canónico: el aud que Ascent exige en los
tokens. space es el space del cerebro donde Ascent puede escribir (su consolidado diario).
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
}
