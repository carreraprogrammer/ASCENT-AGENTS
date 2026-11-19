"""La identidad del dueño sale del entorno; la detección debe comportarse igual
que cuando estaba escrita en el código. Los datos de estos tests son ficticios."""

import importlib
import os
import re

import services.transaction_rules as rules

FULL_NAME = "Ana Maria Perez"
TRANSFER_KEY = "3000000000"

# Forma exacta del regex anterior al cambio, con la identidad ficticia en el
# lugar donde antes iba la real.
LEGACY_SELF_TRANSFER_RE = re.compile(
    r"(env[ií]o|transferencia\s+enviada).*(bre-b|llave).*(ana\s+(perez|maria)|3000000000)",
    re.IGNORECASE,
)

CORPUS = [
    # Claramente propias (nombre o llave del dueño como destino)
    "Envío por Bre-B a ANA PEREZ por $50.000",
    "Envio a la llave de Ana  Maria por $1.200.000",
    "Transferencia enviada Bre-B llave 3000000000 por $80.000",
    "TRANSFERENCIA ENVIADA a la LLAVE 3000000000",
    "envío bre-b ana maria perez",
    # No propias / ambiguas
    "Envío por Bre-B a CARLOS GOMEZ por $50.000",
    "Transferencia enviada Bre-B llave 3111111111",
    "Descuento Transferencia a una llave App Banco",
    "Envío por Bre-B a ANA GOMEZ",
    "Envío por Bre-B a MARIA PEREZ",
    "Ana Perez envío por Bre-B",
    "Recibiste una transferencia por Bre-B de ANA PEREZ",
    "Abono cta de ahorros por $2.000.000",
    "Pago Tarjeta de Crédito por $300.000",
    "Compra en supermercado por $74.000",
    "",
]


def _reload_with_env(**env):
    saved = {k: os.environ.get(k) for k in ("OWNER_FULL_NAME", "OWNER_TRANSFER_KEY")}
    try:
        for key in saved:
            os.environ.pop(key, None)
        os.environ.update(env)
        return importlib.reload(rules)
    finally:
        for key, value in saved.items():
            os.environ.pop(key, None)
            if value is not None:
                os.environ[key] = value


def test_env_built_regex_matches_exactly_like_the_hardcoded_one():
    built = rules.build_self_transfer_re(FULL_NAME, TRANSFER_KEY)
    for text in CORPUS:
        assert bool(built.search(text)) == bool(LEGACY_SELF_TRANSFER_RE.search(text)), text


def test_corpus_exercises_both_outcomes():
    hits = [bool(LEGACY_SELF_TRANSFER_RE.search(text)) for text in CORPUS]
    assert hits.count(True) == 5 and hits.count(False) == len(CORPUS) - 5


def test_module_reads_identity_from_environment():
    mod = _reload_with_env(OWNER_FULL_NAME=FULL_NAME, OWNER_TRANSFER_KEY=TRANSFER_KEY)
    assert mod.transaction_guard_reason("Envío por Bre-B a ANA PEREZ") == "self_transfer"
    assert mod.transaction_guard_reason("Transferencia enviada llave 3000000000") == "self_transfer"
    assert mod.should_force_pending("Envío por Bre-B a ANA PEREZ") is False
    assert mod.transaction_guard_reason("Envío por Bre-B a CARLOS GOMEZ") is None
    assert mod.should_force_pending("Envío por Bre-B a CARLOS GOMEZ") is True


def test_several_transfer_keys_and_key_only():
    built = rules.build_self_transfer_re("", "3000000000, 1020304050")
    assert built.search("Envío llave 1020304050")
    assert built.search("Envío llave 3000000000")
    assert not built.search("Envío llave 3999999999")


def test_without_identity_nothing_is_a_self_transfer():
    mod = _reload_with_env()
    assert mod.transaction_guard_reason("Envío por Bre-B a ANA PEREZ") is None
    # Sin identidad, la saliente a una llave queda en pending para revisión.
    assert mod.should_force_pending("Envío por Bre-B a ANA PEREZ") is True
    # La regla de pago de tarjeta no depende de la identidad.
    assert mod.transaction_guard_reason("Pago Tarjeta de Crédito por $300.000") == "credit_card_payment"


def test_pocket_movements_are_internal_transfers():
    from services.transaction_rules import transaction_guard_reason

    assert transaction_guard_reason("Retiro de bolsillo a la cuenta") == "pocket_transfer"
    assert transaction_guard_reason("Traslado a bolsillo viaje") == "pocket_transfer"
    assert transaction_guard_reason("Abono transferencia ACH") is None


def test_expense_never_keeps_income_category():
    from services.transaction_rules import sanitize_expense_category

    payload = {"transaction_type": "expense", "category_code": "income",
               "subcategory_code": "otros_ingreso", "status": "confirmed"}
    assert sanitize_expense_category(payload) is True
    assert payload == {"transaction_type": "expense", "category_code": "unknown", "status": "confirmed"}

    income = {"transaction_type": "income", "category_code": "income", "subcategory_code": "salario"}
    assert sanitize_expense_category(income) is False
    assert income["subcategory_code"] == "salario"
