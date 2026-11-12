#!/usr/bin/env python3
"""Envía un texto por la API local de WAHA Core.

Uso:
    python3 send_text.py 34600111222 "Hola desde WAHA"

El teléfono va en formato internacional, sin + ni espacios.
La sesión tiene que estar en estado WORKING (QR escaneado en el dashboard).
"""

import sys
from pathlib import Path

import requests

BASE_URL = "http://127.0.0.1:3001"
SESSION = "default"
ENV_FILE = Path(__file__).with_name(".env")


def load_api_key() -> str:
    if not ENV_FILE.exists():
        return ""
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("WAHA_API_KEY="):
            return line.split("=", 1)[1].strip()
    return ""


def main() -> None:
    if len(sys.argv) < 3:
        print('Uso: python3 send_text.py <telefono> "<mensaje>"')
        print('Ejemplo: python3 send_text.py 34600111222 "Hola desde WAHA"')
        sys.exit(1)

    phone = sys.argv[1].lstrip("+").replace(" ", "").replace("-", "")
    text = " ".join(sys.argv[2:])
    api_key = load_api_key()

    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if api_key:
        headers["X-Api-Key"] = api_key

    response = requests.post(
        f"{BASE_URL}/api/sendText",
        headers=headers,
        json={"session": SESSION, "chatId": f"{phone}@c.us", "text": text},
        timeout=60,
    )
    print(response.status_code)
    print(response.text)
    response.raise_for_status()


if __name__ == "__main__":
    main()
