#!/usr/bin/env python3
"""Presenta al Rey Mago a las personas de members.json.

Sin argumentos solo muestra el mensaje y los destinatarios.
No envía nada hasta que lo ejecutes con --send.

    python3 presentacion.py
    python3 presentacion.py --send
"""

import json
import sys
import time
from pathlib import Path

import requests

BASE_URL = "http://127.0.0.1:3001"
SESSION = "default"
HERE = Path(__file__).resolve().parent
ENV_FILE = HERE / ".env"
MEMBERS_FILE = HERE / "members.json"
PAUSE_SECONDS = 8

MESSAGE = """Hola, {nombre}. Soy el representante de Sus Majestades del acogerdor C.M. Peñafiel.

Si te parece bien, querido hermano, usaremos este chat para apuntar lo que te gustaría recibir, que no te extrañe si te escribo recordandote que me envíes la carta. Sus majestades estan activos (incluso en Granada) y no quieren desaprovechar la oportunidad de empezara a hacer compras.

Feliz navidad y no dejes de soñar... siempre hay sorpresas, he visto ofertas irresistibles de cinturones"""


def load_api_key() -> str:
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("WAHA_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"')
    return ""


def load_members() -> list[dict]:
    members = json.loads(MEMBERS_FILE.read_text(encoding="utf-8"))
    people = []
    for member in members:
        name = str(member.get("name") or "").strip()
        phone = "".join(ch for ch in str(member.get("phone") or "") if ch.isdigit())
        if name and phone:
            people.append({"name": name, "nombre": name.split()[0], "phone": phone})
    return people


def send(phone: str, text: str, api_key: str) -> None:
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if api_key:
        headers["X-Api-Key"] = api_key
    response = requests.post(
        f"{BASE_URL}/api/sendText",
        headers=headers,
        json={"session": SESSION, "chatId": f"{phone}@c.us", "text": text},
        timeout=60,
    )
    print(response.status_code, response.text)
    response.raise_for_status()


def main() -> None:
    people = load_members()
    if not people:
        print("No hay nadie en members.json")
        sys.exit(1)

    print(MESSAGE.format(nombre=people[0]["nombre"]))
    print("\n---")
    print(f"Destinatarios ({len(people)}):")
    for person in people:
        print(f"  {person['name']}  +{person['phone']}")

    if "--send" not in sys.argv:
        print("\nNo se ha enviado nada.")
        print("Cuando el texto esté bien: python3 presentacion.py --send")
        return

    api_key = load_api_key()
    for index, person in enumerate(people):
        text = MESSAGE.format(nombre=person["nombre"])
        print(f"\nEnviando a {person['name']}...")
        send(person["phone"], text, api_key)
        if index < len(people) - 1:
            time.sleep(PAUSE_SECONDS)


if __name__ == "__main__":
    main()
