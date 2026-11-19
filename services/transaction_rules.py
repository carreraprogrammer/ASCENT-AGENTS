"""Reglas compartidas para agentes que crean o clasifican transacciones."""

from __future__ import annotations

import os
import re


CARD_PAYMENT_RE = re.compile(
    r"(descuento\s+pago|pago|abono|abonad[oa]|pago\s+m[ií]nimo|d[eé]bito).*"
    r"(tarjeta\s+de\s+cr[eé]dito|tarjeta\s+credito|\btc\b)"
    r"|"
    r"(tarjeta\s+de\s+cr[eé]dito|tarjeta\s+credito|\btc\b).*"
    r"(pago|abono|abonad[oa]|pago\s+m[ií]nimo)",
    re.IGNORECASE,
)


def _owner_identity_pattern(full_name: str, transfer_key: str) -> str | None:
    """Alternativas que identifican al dueño como destinatario de una transferencia.

    full_name: "Nombre Segundo Apellido" → matchea el primer nombre seguido de
    cualquiera de los demás tokens. transfer_key: una o varias llaves (Bre-B,
    documento, celular) separadas por coma.
    """
    alternatives: list[str] = []
    first, *rest = full_name.split() or [""]
    if first and rest:
        others = "|".join(re.escape(token) for token in rest)
        alternatives.append(rf"{re.escape(first)}\s+({others})")
    alternatives.extend(re.escape(key.strip()) for key in transfer_key.split(",") if key.strip())
    return "|".join(alternatives) or None


def build_self_transfer_re(full_name: str, transfer_key: str) -> re.Pattern[str]:
    identity = _owner_identity_pattern(full_name, transfer_key)
    if identity is None:
        # Sin identidad configurada no hay forma de saber que el destino es propio:
        # nunca matchea y la transferencia cae en AMBIGUOUS_OUTBOUND_TRANSFER_RE (pending).
        return re.compile(r"(?!)")
    return re.compile(
        rf"(env[ií]o|transferencia\s+enviada).*(bre-b|llave).*({identity})",
        re.IGNORECASE,
    )


# La identidad del dueño vive en el entorno, no en el código.
SELF_TRANSFER_RE = build_self_transfer_re(
    os.environ.get("OWNER_FULL_NAME", ""),
    os.environ.get("OWNER_TRANSFER_KEY", ""),
)

# Movimientos entre la cuenta y sus bolsillos (Davivienda "Retiro de bolsillo a la cuenta",
# "Traslado a bolsillo"). Es la misma plata del usuario cambiando de cajón: ni ingreso ni
# gasto. Los aportes a bolsillo no se registran como gasto (RFC-0001), así que contar el
# retiro como ingreso duplicaba esa plata en "lo que tienes hoy".
POCKET_TRANSFER_RE = re.compile(
    r"(retiro|traslado|transferencia|abono|aporte|env[ií]o)\s+(de|a|al|desde|hacia)\s+(tu\s+|un\s+|el\s+)?bolsillo"
    r"|bolsillo\s+(a|hacia)\s+(la|tu)\s+cuenta",
    re.IGNORECASE,
)

INCOME_SUBCATEGORY_CODES = {"salario", "freelance", "reembolso", "arriendo_recibido", "otros_ingreso"}

INBOUND_TRANSFER_RE = re.compile(
    r"(abono|transferencia|recibiste|recibido).*(bre-b|llave|cta\s+de\s+ahorros|cuenta\s+de\s+ahorros)",
    re.IGNORECASE,
)
# Transferencia SALIENTE a una llave/Bre-B sin destinatario identificable: el banco no dice
# si la llave es propia o de otra persona, así que puede ser un pago real. No se descarta:
# se fuerza a pending para revisión. (SELF_TRANSFER_RE cubre el caso claramente propio → guard.)
AMBIGUOUS_OUTBOUND_TRANSFER_RE = re.compile(
    r"transferencia\s+a\s+una\s+llave|(env[ií]o|transferencia\s+enviada|descuento\s+transferencia).*(bre-b|llave)",
    re.IGNORECASE,
)


SUBCATEGORY_REFERENCE = """\
═══ CLASIFICÁS EN DOS EJES INDEPENDIENTES ═══
Cada gasto tiene un TIER (agencia) y una FUNCIÓN (qué es). Son ORTOGONALES: una misma
función puede ir con cualquier tier. Mandá SIEMPRE category_code (tier) + subcategory_code (función).

── EJE 1 · TIER (category_code) — pregunta única ──
  "Si mi situación empeora, ¿qué margen tengo sobre este gasto?"
    No puedo dejar de pagarlo sin incumplir una obligación   → committed
    Lo sigo necesitando aunque reduzca el monto (mínimo > 0)  → necessary
    Podría llevarlo a CERO en una crisis                      → discretionary (= "Flexible")

── EJE 2 · FUNCIÓN (subcategory_code) — qué es (lista plana, sirve para cualquier tier) ──
  arriendo, creditos, seguros, servicios_publicos, colegiaturas,
  mercado, almuerzo, gasolina, transporte, salud, ejercicio, celular, herramientas,
  restaurantes, delivery, ocio, ropa, tecnologia, suscripciones, cursos, suplementos, social
  income (category_code=income): salario, freelance, reembolso, arriendo_recibido, otros_ingreso
  unknown: si el tier no está claro — subcategory_code null

── La MISMA función abarca tiers (elegí el tier por la pregunta, no por la función) ──
  Medicina / EPS               → necessary + salud
  Tratamiento electivo, boxeo  → discretionary + salud   (elegido, cortable en crisis)
  Mercado básico               → necessary + mercado
  Restaurante con familia      → discretionary + social

── ALMUERZO (regla del usuario) ──
  Comida en horario de almuerzo (~12:00–15:00) → necessary + almuerzo. El almuerzo NO es
  negociable (necessary), no discretionary/restaurantes. El usuario suele ir a los mismos
  sitios, así que el historial (classification_hints) ya lo sugiere para esos comercios —
  respetalo. La comida fuera de ese horario sigue la pregunta única (cena/antojo → normalmente flexible).

═══ NOTA (RFC-0001) ═══
AHORRO / INVERSIÓN-INSTRUMENTO (aporte a fondo de emergencia, aporte o retiro de bolsillo,
CDT, acciones, cripto) NO es un gasto ni un tier. Usá las herramientas de ahorro/metas
(savings_goal / sinking_fund), no una categoría de gasto.
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
Solo descartá cuando el texto identifica CLARAMENTE que el destino es tuyo (tu nombre, tu cédula,
tu propia cuenta/llave/tarjeta). En ese caso no registres una transacción confirmada.

⚠️ "Transferencia a una llave" / Bre-B SALIENTE sin destinatario identificable (ej. Davivienda:
"Descuento Transferencia a una llave", "App Davivienda", sin nombre de quien recibe): NO la descartes.
Una llave puede ser de OTRA persona → es un pago real (gasto). Como el banco no dice de quién es la
llave, registrala SIEMPRE como gasto pending (payment_source="debit", status="pending") para que el
usuario confirme si fue pago a un tercero o movimiento interno. Nunca la ignores por completo.
Si hay duda real sobre si es ingreso externo o movimiento interno, crea una transacción pending para revisión.

BOLSILLOS: "Retiro de bolsillo a la cuenta", "Traslado a bolsillo" y similares son plata tuya cambiando
de cajón dentro del mismo banco → NO registrar (ni ingreso ni gasto).

Un GASTO nunca lleva category_code="income" ni una subcategoría de ingreso (salario, freelance,
reembolso, arriendo_recibido, otros_ingreso). Si no sabés qué es, dejalo sin subcategoría y pending.
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
    if POCKET_TRANSFER_RE.search(text):
        return "pocket_transfer"
    return None


def sanitize_expense_category(payload: dict) -> bool:
    """Un gasto nunca lleva categoría de ingreso. Si el agente la puso (pasaba con
    transferencias a llaves), se borra y el gasto queda pending para revisión.
    Devuelve True si corrigió algo."""
    if payload.get("transaction_type") != "expense":
        return False
    wrong = payload.get("category_code") == "income" or payload.get("subcategory_code") in INCOME_SUBCATEGORY_CODES
    if not wrong:
        return False
    payload.pop("category_code", None)
    payload.pop("subcategory_code", None)
    payload["status"] = "pending"
    return True


def should_force_pending(text: str) -> bool:
    if SELF_TRANSFER_RE.search(text):
        # Transferencia claramente propia → la maneja el guard, no forzar pending aquí.
        return False
    return bool(INBOUND_TRANSFER_RE.search(text) or AMBIGUOUS_OUTBOUND_TRANSFER_RE.search(text))


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
