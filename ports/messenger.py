"""
ports/messenger.py — Contrato para enviar mensajes y parsear updates entrantes.

El Brain no sabe si el mensaje viene de Telegram, WhatsApp o una web UI.
`ParsedUpdate` es el objeto de dominio agnóstico de plataforma que el router
usa para decidir qué agente activar.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class UserIntent:
    """Intents de dominio — sin ningún detalle de Telegram."""
    WIZARD_CALLBACK          = "wizard_callback"          # botón de step del budget wizard
    FC_WIZARD_CALLBACK       = "fc_wizard_callback"       # botón del financial context wizard
    INCOME_WIZARD_CALLBACK   = "income_wizard_callback"   # botón del income wizard (wi:...)
    WIZARD_TRIGGER           = "wizard_trigger"           # botones de inicio del wizard (start/tomorrow/skip)
    CATEGORIZATION_CALLBACK  = "categorization_callback"  # cat: / confirm: / skip:
    CHAT_CALLBACK            = "chat_callback"            # chat:... respuestas rápidas del chat
    COMMAND                  = "command"                  # /resumen /deudas /balance ...
    EXPENSE_REPORT           = "expense_report"           # texto plano — va al agente nocturno


@dataclass
class ParsedUpdate:
    intent: str
    command: str | None       = None   # "resumen", "deudas", "balance" ...
    command_args: str | None  = None   # texto después del comando, o None
    text: str                 = ""     # texto original completo
    callback_query_id: str | None = None
    callback_data: str | None     = None
    raw: dict                 = field(default_factory=dict)


class MessengerPort(ABC):

    @abstractmethod
    def parse_update(self, update: dict) -> ParsedUpdate:
        """
        Traduce un update de la plataforma (Telegram, WhatsApp…) a un
        ParsedUpdate agnóstico. Toda la lógica de formato de plataforma
        pertenece aquí — el router nunca toca `update` directamente.
        """
        ...

    @abstractmethod
    def send_message(self, text: str, parse_mode: str = "HTML") -> None:
        """Envía un mensaje de texto al usuario."""
        ...

    @abstractmethod
    def send_with_buttons(
        self,
        text: str,
        buttons: list[list[dict]],
        parse_mode: str = "HTML",
    ) -> None:
        """
        Envía un mensaje con botones inline.
        buttons: lista de filas, cada fila es lista de {text, callback_data}
        """
        ...

    @abstractmethod
    def answer_callback(
        self,
        callback_query_id: str,
        text: str,
        show_alert: bool = False,
    ) -> None:
        """Responde a un callback_query (quita el spinner del botón)."""
        ...
