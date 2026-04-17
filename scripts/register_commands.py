"""
scripts/register_commands.py — Registra los slash commands del bot en Telegram.

Ejecutar cada vez que se agregue o cambie un comando:
  railway run python scripts/register_commands.py
"""

import os
import httpx

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_BASE   = f"https://api.telegram.org/bot{BOT_TOKEN}"

COMMANDS = [
    {"command": "resumen",     "description": "Resumen del mes con burn rate y alertas"},
    {"command": "balance",     "description": "Saldo disponible ahora mismo"},
    {"command": "plan",        "description": "Ver o armar el plan mensual"},
    {"command": "ingresos",    "description": "Registrar o revisar fuentes de ingreso"},
]

resp = httpx.post(f"{TG_BASE}/setMyCommands", json={"commands": COMMANDS})
data = resp.json()

if data.get("ok"):
    print(f"✅ {len(COMMANDS)} comandos registrados correctamente.")
    for c in COMMANDS:
        print(f"   /{c['command']} — {c['description']}")
else:
    print(f"❌ Error: {data}")
