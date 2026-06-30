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
═══ CLASIFICACIÓN POR AGENCIA — 3 TIERS ═══

Pregunta única para clasificar CUALQUIER gasto:
  "Si mi situación financiera empeora, ¿qué margen de maniobra tengo sobre este gasto?"
    → No puedo dejar de pagarlo sin incumplir una obligación   → committed
    → Lo sigo necesitando aunque reduzca el monto (mínimo > 0)  → necessary
    → Podría llevarlo a CERO en una crisis                      → discretionary (= "Flexible")

═══ SUBCATEGORÍAS VÁLIDAS (category_code → subcategory_code) ═══

committed (Comprometido):
  arriendo, creditos, seguros, servicios_publicos, colegiaturas

necessary (Necesario):
  mercado, gasolina, transporte, salud, ejercicio, celular, herramientas

discretionary (Flexible):
  restaurantes, delivery, ocio, ropa, tecnologia, suscripciones, cursos, suplementos, social

income (Ingreso):
  salario, freelance, reembolso, arriendo_recibido, otros_ingreso

unknown: usá cuando la categoría no está clara — subcategory_code omitido (null)

═══ NOTAS DEL MODELO (RFC-0001) ═══
- Ya NO existen los tiers "investment" ni "social". El gasto relacional (regalos, salidas,
  familia, amigos, donaciones) va a discretionary/social. El gasto "en uno mismo" cae en su
  tier por la pregunta única: cursos/suplementos = discretionary; herramientas de trabajo = necessary.
- AHORRO / INVERSIÓN-INSTRUMENTO (aporte a fondo de emergencia, aporte o retiro de bolsillo,
  CDT, acciones, cripto) NO es un gasto. NO uses una categoría de gasto: usá las herramientas
  de ahorro/metas (savings_goal / sinking_fund).
"""


AMBIGUITY_RULES = """\
═══ REGLA DE AMBIGÜEDAD EN SUBCATEGORÍA ═══
- Clasificar directamente si el contexto hace clara la subcategoría.
- Preguntar SOLO cuando la diferencia de categoría conductual cambia el análisis y el contexto no lo resuelve.
  * Casos donde SE clasifica directamente (nunca preguntar):
    - "Fui a restaurante con mis papás / familia / pareja / amigo" → discretionary/social
    - "Pagué el arriendo" → committed/arriendo
    - "Compré en el Éxito / tienda / supermercado" → necessary/mercado
    - "Tamales / comida / almuerzo" sin mención de persona → discretionary/restaurantes
    - "74.000 cafetería con familia" → discretionary/social
    - "Pagué un curso online" → discretionary/cursos
  * Casos donde SÍ se pregunta:
    - "Compré audífonos Sony" → ¿necessary/herramientas (si es para trabajo) o discretionary/tecnologia (si es ocio)?
- Si el mensaje menciona otra persona (familia, amigo, pareja, nombre propio), la subcategoría discretionary/social es implícita.
- Para montos menores a 50.000 COP con contexto claro, no preguntar — clasificar directamente.
- El usuario siempre puede cambiar la clasificación después.
- Si vas a preguntar la categoría con botones inline, primero creá la transacción con tu mejor clasificación provisional o status="pending"; luego enviá botones con callback_data "cat:{txn_id}:{subcat_code}".
"""


CREDIT_CARD_RULES = """\
═══ REGLA — DEUDA vs TARJETA DE CRÉDITO (NO las confundas) ═══
Son cosas opuestas. La distinción es semántica y vale para cualquier banco:

1) PAGO / ABONO / CUOTA DE UN CRÉDITO O PRÉSTAMO (libre inversión, vehículo, hipoteca,
   crédito de consumo; textos tipo "Pago a Crédito", "Abono a crédito", "Cuota crédito",
   "Pago de préstamo/obligación"):
   Es plata REAL saliendo de tu cuenta para servir una deuda → SÍ registrar como gasto con
   create_transaction, payment_source="debit", subcategoría "creditos". El backend lo vincula
   a la deuda / obligación recurrente automáticamente. NO lo ignores.

2) PAGO DE LA TARJETA DE CRÉDITO (pagar el estado de cuenta del plástico; textos que dicen
   explícitamente "tarjeta de crédito" o "TC": "Pago Tarjeta de Crédito", "Pago TC",
   "Abono TC", "pago mínimo de la tarjeta"):
   Es una transferencia entre cuentas propias, no un gasto nuevo → NO crear transacción.

Regla de oro: si es un pago AL plástico (menciona "tarjeta de crédito"/"TC") → ignorar.
Si es un pago A un crédito o préstamo → registrar como gasto debit. "crédito" solo (sin
"tarjeta") significa préstamo/deuda, NO tarjeta.

3) COMPRAS con tarjeta de crédito: registrar con payment_source="credit_card".
   El gasto se imputa al momento de la compra, como cualquier otro gasto confirmado.
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
        else ["restaurantes", "mercado", "transporte", "tecnologia", "social", "creditos"]
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
