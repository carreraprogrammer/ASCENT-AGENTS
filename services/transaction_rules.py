"""Reglas compartidas para agentes que crean o clasifican transacciones."""

from __future__ import annotations

import re


CARD_PAYMENT_RE = re.compile(
    r"(descuento\s+pago|pago|abono|abonad[oa]|pago\s+m[ií]nimo|d[eé]bito).*"
    r"(tarjeta\s+de\s+cr[eé]dito|tarjeta\s+credito|\btc\b)"
    r"|"
    r"(tarjeta\s+de\s+cr[eé]dito|tarjeta\s+credito|\btc\b).*"
    r"(pago|abono|abonad[oa]|pago\s+m[ií]nimo)",
    re.IGNORECASE,
)
SELF_TRANSFER_RE = re.compile(
    r"(env[ií]o|transferencia\s+enviada).*(bre-b|llave).*(daniel\s+(carrera|alejandro)|1085333083)",
    re.IGNORECASE,
)
INBOUND_TRANSFER_RE = re.compile(
    r"(abono|transferencia|recibiste|recibido).*(bre-b|llave|cta\s+de\s+ahorros|cuenta\s+de\s+ahorros)",
    re.IGNORECASE,
)


SUBCATEGORY_REFERENCE = """\
═══ SUBCATEGORÍAS VÁLIDAS ═══

committed (Comprometido):
  arriendo, creditos, seguros, servicios_publicos, colegiaturas

necessary (Necesario):
  mercado, gasolina, transporte, salud, ejercicio, celular

discretionary (Flexible):
  restaurantes, delivery, ocio, ropa, tecnologia, suscripciones

investment (Inversión):
  cursos, libros, suplementos, herramientas, ahorro_voluntario

social (Social):
  regalos, salidas, familia, donaciones

income (Ingreso):
  salario, freelance, reembolso, arriendo_recibido, otros_ingreso

unknown: usá cuando la categoría no está clara — subcategory_code omitido (null)
"""


AMBIGUITY_RULES = """\
═══ REGLA DE AMBIGÜEDAD EN SUBCATEGORÍA ═══
- Clasificar directamente si el contexto hace clara la subcategoría.
- Preguntar SOLO cuando la diferencia de categoría conductual cambia el análisis y el contexto no lo resuelve.
  * Casos donde SE clasifica directamente (nunca preguntar):
    - "Fui a restaurante con mis papás / familia / pareja / amigo" → social/salidas
    - "Pagué el arriendo" → committed/arriendo
    - "Compré en el Éxito / tienda / supermercado" → necessary/mercado
    - "Tamales / comida / almuerzo" sin mención de persona → discretionary/restaurantes
    - "74.000 cafetería con familia" → social/salidas
  * Casos donde SÍ se pregunta:
    - "Compré audífonos Sony" → ¿discretionary/tecnologia o investment/herramientas?
    - "Pagué un curso online" → preguntar si no está claro si es inversión o ocio
- Si el mensaje menciona otra persona (familia, amigo, pareja, nombre propio), la subcategoría social es implícita.
- Para montos menores a 50.000 COP con contexto claro, no preguntar — clasificar directamente.
- El usuario siempre puede cambiar la clasificación después.
- Si vas a preguntar la categoría con botones inline, primero creá la transacción con tu mejor clasificación provisional o status="pending"; luego enviá botones con callback_data "cat:{txn_id}:{subcat_code}".
"""


CREDIT_CARD_RULES = """\
═══ REGLA — TARJETA DE CRÉDITO ═══
Compras con TC: registrar con create_transaction y payment_source="credit_card".
El gasto se imputa al momento de la compra, como cualquier otro gasto confirmado.

Abonos/pagos al banco (email o mensaje dice "Abono TC", "Pago TC", "Pago tarjeta",
"Descuento Pago Tarjeta de Crédito", "se han abonado", "pago mínimo", o similar):
NO crear transacción — es una transferencia entre banco y tarjeta, no un gasto nuevo.

Cuotas de deuda diferida en TC (celular a cuotas, etc.): registrar con payment_source="debit"
cuando sea plata saliendo de la cuenta de ahorros para servir una deuda ya capturada.
"""


TRANSFER_RULES = """\
═══ REGLA — TRANSFERENCIAS ENTRE CUENTAS PROPIAS ═══
Mover plata entre cuentas propias, llaves propias, bolsillos o tarjeta propia NO es ingreso ni gasto.
Si el texto apunta a transferencia interna, no registres una transacción confirmada.
Si hay duda real sobre si es ingreso externo o movimiento interno, crea una transacción pending para revisión.
"""


TRANSACTION_CREATION_RULES = "\n\n".join([
    SUBCATEGORY_REFERENCE,
    AMBIGUITY_RULES,
    CREDIT_CARD_RULES,
    TRANSFER_RULES,
])


def transaction_guard_reason(text: str) -> str | None:
    """Razón determinística para impedir que un agente cree una transacción."""
    if CARD_PAYMENT_RE.search(text):
        return "credit_card_payment"
    if SELF_TRANSFER_RE.search(text):
        return "self_transfer"
    return None


def should_force_pending(text: str) -> bool:
    return bool(INBOUND_TRANSFER_RE.search(text))


def normalize_categories(raw_rows: list[dict]) -> list[dict]:
    categories: list[dict] = []
    for raw in raw_rows:
        attributes = raw.get("attributes", raw)
        subcategories = raw.get("relationships", {}).get("subcategories", {}).get("data", [])
        categories.append(
            {
                "id": raw.get("id"),
                "name": attributes.get("name"),
                "code": attributes.get("code"),
                "category_type": attributes.get("category_type"),
                "subcategories": [
                    {
                        "id": sub.get("id"),
                        "name": sub.get("attributes", {}).get("name"),
                        "code": sub.get("attributes", {}).get("code"),
                    }
                    for sub in subcategories
                ],
            }
        )
    return categories


def quick_category_buttons(categories: list[dict], transaction_type: str | None = None) -> list[dict]:
    preferred = (
        ["salario", "freelance", "reembolso", "otros_ingreso"]
        if transaction_type == "income"
        else ["restaurantes", "mercado", "transporte", "tecnologia", "salidas", "creditos"]
    )
    by_code = {
        sub.get("code"): sub
        for cat in categories
        for sub in cat.get("subcategories", [])
        if sub.get("code")
    }
    buttons = []
    for code in preferred:
        sub = by_code.get(code)
        if sub:
            buttons.append({"text": sub.get("name") or code, "code": code})
    return buttons
