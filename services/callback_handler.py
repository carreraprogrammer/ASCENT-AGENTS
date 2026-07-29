"""
services/callback_handler.py — Procesa callbacks de categorización en tiempo real.

Esto reemplaza la lógica que estaba en TelegramController de Rails.
El Brain responde inmediatamente (< 2s) y actualiza la transacción en Rails.
"""

import logging

import httpx

from ports.rails_api import RailsApiPort
from ports.messenger import MessengerPort

logger = logging.getLogger(__name__)


def _is_not_found(exc: Exception) -> bool:
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 404


_PAYMENT_SOURCE_LABEL = {
    "credit_card": "Tarjeta de Crédito 💳",
    "debit":       "Débito / Transferencia 🏦",
    "cash":        "Efectivo 💵",
}


def handle(api: RailsApiPort, messenger: MessengerPort, data: str) -> None:
    """
    Procesa un callback_query que NO es del wizard.

    Formatos esperados:
      cat:{txn_id}:{subcat_code}         → clasificar y confirmar
      confirm:{txn_id}                   → confirmar pendiente
      skip:{txn_id}                      → posponer
      pay:{txn_id}:{payment_source}      → asignar medio de pago (credit_card|debit|cash)
    """
    parts = data.split(":")

    def is_numeric_id(val: str) -> bool:
        return val.isdigit()

    if parts[0] == "cat" and len(parts) == 3:
        txn_id, subcat_code = parts[1], parts[2]
        if not is_numeric_id(txn_id):
            logger.warning("[callback_handler] callback ignorado: txn_id no numérico: %s", data)
            return
        try:
            result = api.update_transaction(txn_id, subcategory_code=subcat_code, status="confirmed")
            attrs = result.get("data", {}).get("attributes", result)
            concept = attrs.get("concept", "transacción")
            messenger.send_message(f"✅ <b>{concept}</b> → <i>{subcat_code}</i>")
        except Exception as e:
            logger.error("[callback_handler] cat update failed: %s", e)
            if _is_not_found(e):
                messenger.send_message("⚠️ Esa transacción ya no existe. Vuelve a registrarla.")
            else:
                messenger.send_message("❌ Error al actualizar la transacción.")

    elif parts[0] == "confirm" and len(parts) == 2:
        txn_id = parts[1]
        if not is_numeric_id(txn_id):
            logger.warning("[callback_handler] callback ignorado: txn_id no numérico: %s", data)
            return
        try:
            result = api.update_transaction(txn_id, status="confirmed")
            attrs = result.get("data", {}).get("attributes", result)
            concept = attrs.get("concept", "transacción")
            messenger.send_message(f"✅ <b>{concept}</b> confirmado")
        except Exception as e:
            logger.error("[callback_handler] confirm update failed: %s", e)
            if _is_not_found(e):
                messenger.send_message("⚠️ Esa transacción ya no existe. Vuelve a registrarla.")
            else:
                messenger.send_message("❌ Error al confirmar.")

    elif parts[0] == "pay" and len(parts) == 3:
        txn_id, payment_source = parts[1], parts[2]
        if not is_numeric_id(txn_id):
            logger.warning("[callback_handler] callback ignorado: txn_id no numérico: %s", data)
            return
        try:
            kwargs: dict = {"payment_source": payment_source}
            result = api.update_transaction(txn_id, **kwargs)
            attrs = result.get("data", {}).get("attributes", result)
            concept = attrs.get("concept", "transacción")
            label = _PAYMENT_SOURCE_LABEL.get(payment_source, payment_source)
            messenger.send_message(f"✅ <b>{concept}</b> → {label}")
        except Exception as e:
            logger.error("[callback_handler] pay update failed: %s", e)
            if _is_not_found(e):
                messenger.send_message("⚠️ Esa transacción ya no existe. Vuelve a registrarla.")
            else:
                messenger.send_message("❌ Error al actualizar el medio de pago.")

    elif parts[0] == "skip" and len(parts) == 2:
        txn_id = parts[1]
        if not is_numeric_id(txn_id):
            logger.warning("[callback_handler] callback ignorado: txn_id no numérico: %s", data)
            return
        try:
            from datetime import datetime, timezone
            api.update_transaction(txn_id, clarification_resolved_at=datetime.now(timezone.utc).isoformat())
            # No enviamos mensaje — "skip" es silencioso
        except Exception as e:
            logger.error("[callback_handler] skip update failed: %s", e)

    else:
        logger.warning("[callback_handler] callback no reconocido: %s", data)
