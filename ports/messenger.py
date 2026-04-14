"""
ports/messenger.py — Contrato para enviar mensajes al usuario.

El Brain no sabe si el mensaje va a Telegram, WhatsApp, o Ionic.
"""

from abc import ABC, abstractmethod


class MessengerPort(ABC):

    @abstractmethod
    def send_message(self, text: str, parse_mode: str = "HTML") -> None:
        """Envía un mensaje de texto."""
        ...

    @abstractmethod
    def send_with_buttons(self, text: str, buttons: list[list[dict]], parse_mode: str = "HTML") -> None:
        """
        Envía un mensaje con inline keyboard.
        buttons: lista de filas, cada fila es lista de {text, callback_data}
        Ejemplo:
          [[{"text": "Sí", "callback_data": "resp:yes"}],
           [{"text": "No", "callback_data": "resp:no"}]]
        """
        ...

    @abstractmethod
    def answer_callback(self, callback_query_id: str, text: str, show_alert: bool = False) -> None:
        """Responde a un callback_query (quita el spinner del botón)."""
        ...
