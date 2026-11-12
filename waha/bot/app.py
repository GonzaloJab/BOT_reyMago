"""Rey Mago: WhatsApp chat -> LangGraph memory -> wish list per household member."""

import json
import logging
import os
import re
import sqlite3
import threading
import time
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field
from typing_extensions import TypedDict

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("reyes")

WAHA_BASE_URL = os.environ.get("WAHA_BASE_URL", "http://waha:3000").rstrip("/")
WAHA_API_KEY = os.environ.get("WAHA_API_KEY", "")
WAHA_SESSION = os.environ.get("WAHA_SESSION", "default")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "").rstrip("/")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "")
DATA_DIR = os.environ.get("DATA_DIR", "/data")
MEMBERS_PATH = os.environ.get("MEMBERS_PATH", "/app/members.json")
PROMPT_PATH = "/app/system-prompt.txt"
REPLY_TO_GROUPS = os.environ.get("REPLY_TO_GROUPS", "false").lower() == "true"

URL_RE = re.compile(r"https?://[^\s<>\"]+")
CLEAR_HINTS = (
    "borra todo",
    "borra la lista",
    "elimina todo",
    "elimina la lista",
    "quita todo",
    "vacía la lista",
    "vacia la lista",
    "olvidalo todo",
    "olvídalo todo",
    "clear the list",
    "delete everything",
)

os.makedirs(DATA_DIR, exist_ok=True)

app = FastAPI(title="Carta de Reyes")
_lock = threading.Lock()
_seen: set[str] = set()
_last_turn: dict[str, dict] = {}


class WishItem(BaseModel):
    title: str = Field(description="Nombre del regalo, en el idioma de la persona")
    details: str = Field(
        default="",
        description="Características que ha dicho: talla, color, modelo u otra precisión. Vacío si no hay.",
    )


class WishWrite(BaseModel):
    id: str = ""
    title: str = ""
    details: str = ""
    url: str = ""
    notes: str = ""
    bought: bool = False
    visible: bool = True
    price: float | None = None


class KingTurn(BaseModel):
    reply: str = Field(description="Respuesta breve de WhatsApp del Rey Mago")
    wishes: list[WishItem] = Field(
        description="Lista completa de quien escribe, solo con nombre y características. Si el mensaje no cambia nada, repítela igual."
    )


class SecretGift(BaseModel):
    member: str = Field(description="Nombre de otra persona de la casa")
    title: str = Field(description="Nombre del regalo a apuntar para esa persona")
    details: str = Field(default="", description="Talla, color u otra característica. Vacío si no la dijo.")


class Outbound(BaseModel):
    member: str = Field(description="Persona de la casa a la que hay que escribir")
    instruction: str = Field(description="Qué hay que decirle, concreto y breve")


class AdminTurn(BaseModel):
    reply: str = Field(description="Respuesta breve de WhatsApp para quien organiza")
    wishes: list[WishItem] = Field(
        description="Lista propia de quien escribe. Si no habla de sus regalos, repítela igual. No metas aquí regalos de otras personas."
    )
    secret_gifts: list[SecretGift] = Field(
        default_factory=list,
        description="Regalos que pide apuntar para otra persona. Vacío si no lo ha pedido. Siempre son secretos.",
    )
    outbox: list[Outbound] = Field(
        default_factory=list,
        description="Mensajes que pide enviar a otra persona. Vacío si no ha pedido escribir a nadie.",
    )


class State(TypedDict):
    messages: Annotated[list, add_messages]


def env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def llm_ready() -> bool:
    return bool(LLM_BASE_URL and LLM_API_KEY and LLM_MODEL)


def load_prompt() -> str:
    text = ""
    try:
        with open(PROMPT_PATH, encoding="utf-8") as handle:
            text = handle.read().strip()
    except FileNotFoundError:
        text = ""
    return text or "Eres un Rey Mago. Apunta los regalos de Reyes sin inventarlos."


def digits(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


def parse_price(value) -> float | None:
    if value is None or value is False or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = str(value).strip().replace("€", "").replace(" ", "").replace(",", ".")
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
    if number < 0 or number != number:
        return None
    return round(number, 2)


def load_members() -> list[dict]:
    try:
        with open(MEMBERS_PATH, encoding="utf-8") as handle:
            data = json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    members = []
    for item in data:
        if not isinstance(item, dict):
            continue
        phone = digits(str(item.get("phone") or ""))
        name = str(item.get("name") or "").strip()
        context = str(item.get("context") or item.get("contexto") or "").strip()
        admin = bool(item.get("admin") or item.get("is_admin"))
        if phone and name:
            members.append({"name": name, "phone": phone, "context": context, "admin": admin})
    return members


def same_phone(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if left == right:
        return True
    short, long = (left, right) if len(left) <= len(right) else (right, left)
    return len(short) >= 9 and long.endswith(short)


def jid_phone(value: str) -> str:
    if not value or "@lid" in value:
        return ""
    user = value.split("@", 1)[0].split(":", 1)[0]
    return digits(user)


def phone_candidates(chat_id: str, payload: dict) -> list[str]:
    raw = [chat_id]
    data = payload.get("_data")
    info = data.get("Info") if isinstance(data, dict) and isinstance(data.get("Info"), dict) else {}
    raw.extend(
        str(info.get(key) or "")
        for key in ("SenderAlt", "RecipientAlt", "Chat", "Sender")
    )
    phones = []
    for value in raw:
        phone = jid_phone(value)
        if phone and phone not in phones:
            phones.append(phone)
    return phones


def sender_name(payload: dict) -> str:
    for key in ("notifyName", "pushName", "senderName"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    data = payload.get("_data")
    if isinstance(data, dict):
        info = data.get("Info") if isinstance(data.get("Info"), dict) else data
        for key in ("PushName", "pushName", "Notify", "notify"):
            value = info.get(key) if isinstance(info, dict) else None
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def identify(chat_id: str, payload: dict) -> dict | None:
    phones = phone_candidates(chat_id, payload)
    members = load_members()
    for member in members:
        for phone in phones:
            if same_phone(phone, member["phone"]):
                return {
                    "key": member["phone"],
                    "name": member["name"],
                    "phone": member["phone"],
                    "context": member.get("context") or "",
                    "admin": bool(member.get("admin")),
                    "known": True,
                    "reply_to": f"{member['phone']}@c.us",
                }
    if members:
        return None
    phone = phones[0] if phones else ""
    name = sender_name(payload) or (f"Teléfono ···{phone[-4:]}" if phone else "Desconocido")
    return {"key": phone or chat_id, "name": name, "phone": phone, "known": False, "reply_to": chat_id}


base_llm = None


def build_graph():
    global base_llm
    if not llm_ready():
        return None
    llm = ChatOpenAI(
        model=LLM_MODEL,
        api_key=LLM_API_KEY,
        base_url=LLM_BASE_URL,
        default_headers={"api-key": LLM_API_KEY},
        timeout=60,
    )
    base_llm = llm
    structured = llm.with_structured_output(KingTurn)
    structured_admin = llm.with_structured_output(AdminTurn)

    def respond(state: State, config: RunnableConfig) -> dict:
        cfg = config.get("configurable", {})
        thread_id = str(cfg.get("thread_id") or "")
        name = str(cfg.get("member_name") or "esta persona")
        context = str(cfg.get("member_context") or "").strip()
        current = cfg.get("current_wishes") or []
        context_line = context or "No hay contexto de esta persona. No inventes aficiones."
        admin = bool(cfg.get("admin"))
        household_line = json.dumps(cfg.get("household") or [], ensure_ascii=False)
        if admin:
            system = (
                f"{load_prompt()}\n\n"
                f"Hablas con {name}, que organiza los regalos de la casa.\n"
                "Con esta persona puedes resumir la carta de cualquiera: qué ha pedido y qué falta.\n"
                "Falta si su lista está vacía, o si un regalo tiene bought en false.\n"
                "visible false significa que esa persona no lo sabe. A ella no se lo menciones. "
                "A quien organiza sí puedes decírselo.\n"
                "No cites enlaces ni notas internas: no están en estos datos.\n"
                "No inventes regalos. Usa solo el JSON de la casa.\n"
                "Si solo pregunta, secret_gifts va vacío y su lista propia se repite igual.\n"
                "Si pide apuntar un regalo para otra persona, va en secret_gifts y no en su lista propia. "
                "Ese regalo es secreto.\n"
                "Si pide algo para sí, va en su lista propia y no en secret_gifts.\n"
                "Si pide que escribas, envíes o le digas algo a otra persona, ponlo en outbox: "
                "su nombre y la instrucción concreta. outbox va vacío si no ha pedido escribir a nadie.\n"
                f"Contexto de {name}:\n{context_line}\n"
                f"Su lista propia, solo lo que ya conoce:\n{json.dumps(current, ensure_ascii=False)}\n"
                f"Casa:\n{household_line}"
            )
            parser = structured_admin
        else:
            system = (
                f"{load_prompt()}\n\n"
                f"Hablas con {name}.\n"
                f"Contexto de {name}:\n{context_line}\n"
                "Lista actual, solo nombre y características:\n"
                f"{json.dumps(current, ensure_ascii=False)}\n"
                "Devuelve esa lista completa, solo con nombre y características. "
                "No conoces enlaces, notas de compra ni si algo ya está comprado: no los menciones. "
                "Una sugerencia no entra en la lista hasta que la acepte."
            )
            parser = structured
        secret_gifts: list[dict] = []
        outbox: list[dict] = []
        try:
            turn = parser.invoke([SystemMessage(content=system), *state["messages"]])
            wishes = [item.model_dump() for item in turn.wishes]
            reply = turn.reply.strip()
            if admin:
                secret_gifts = [item.model_dump() for item in turn.secret_gifts]
                outbox = [item.model_dump() for item in turn.outbox]
        except Exception:
            log.exception("structured reply failed for %s", name)
            plain = llm.invoke([SystemMessage(content=system), *state["messages"]])
            wishes = list(current)
            reply = reply_text(plain)
        _last_turn[thread_id] = {
            "reply": reply,
            "wishes": wishes,
            "secret_gifts": secret_gifts,
            "outbox": outbox,
        }
        return {"messages": [AIMessage(content=reply)]}

    builder = StateGraph(State)
    builder.add_node("respond", respond)
    builder.add_edge(START, "respond")
    builder.add_edge("respond", END)
    conn = sqlite3.connect(f"{DATA_DIR}/checkpoints.sqlite", check_same_thread=False)
    return builder.compile(checkpointer=SqliteSaver(conn))


graph = build_graph()

_db = sqlite3.connect(f"{DATA_DIR}/chats.sqlite", check_same_thread=False)
_db.execute(
    """
    CREATE TABLE IF NOT EXISTS chats (
        chat_id TEXT PRIMARY KEY,
        last_text TEXT,
        last_reply TEXT,
        updated_at TEXT
    )
    """
)
_db.execute(
    """
    CREATE TABLE IF NOT EXISTS lists (
        member_key TEXT PRIMARY KEY,
        name TEXT,
        phone TEXT,
        wishes_json TEXT,
        updated_at TEXT
    )
    """
)
_db.execute(
    """
    CREATE TABLE IF NOT EXISTS nudges (
        member_key TEXT PRIMARY KEY,
        last_sent_at TEXT,
        last_text TEXT
    )
    """
)
_db.commit()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_wishes(member_key: str) -> list[dict]:
    row = _db.execute("SELECT wishes_json FROM lists WHERE member_key = ?", (member_key,)).fetchone()
    if not row or not row[0]:
        return []
    try:
        data = json.loads(row[0])
    except json.JSONDecodeError:
        return []
    return normalize_wishes(data if isinstance(data, list) else [])


def save_wishes(member: dict, wishes: list[dict]) -> None:
    _db.execute(
        """
        INSERT INTO lists (member_key, name, phone, wishes_json, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(member_key) DO UPDATE SET
            name = excluded.name,
            phone = excluded.phone,
            wishes_json = excluded.wishes_json,
            updated_at = excluded.updated_at
        """,
        (member["key"], member["name"], member["phone"], json.dumps(wishes, ensure_ascii=False), now()),
    )
    _db.commit()


def remember(chat_id: str, text: str, reply: str) -> None:
    _db.execute(
        """
        INSERT INTO chats (chat_id, last_text, last_reply, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
            last_text = excluded.last_text,
            last_reply = excluded.last_reply,
            updated_at = excluded.updated_at
        """,
        (chat_id, text, reply, now()),
    )
    _db.commit()


def reply_text(message) -> str:
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content.strip()
    parts = []
    for block in content or []:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "\n".join(part for part in parts if part).strip()


def normalize_wish(item: dict) -> dict | None:
    if not isinstance(item, dict):
        return None
    title = str(item.get("title") or "").strip()
    legacy_note = str(item.get("note") or "").strip()
    details = str(item.get("details") if item.get("details") is not None else "").strip() or legacy_note
    url = str(item.get("url") or "").strip().rstrip(").,]>")
    notes = str(item.get("notes") or "").strip()
    if not title and url:
        title = "Enlace"
    if not title:
        return None
    existing = str(item.get("id") or "").strip()
    wid = existing or uuid.uuid5(uuid.NAMESPACE_URL, f"{title.casefold()}|{url}").hex[:12]
    return {
        "id": wid,
        "title": title,
        "details": details,
        "url": url,
        "notes": notes,
        "bought": bool(item.get("bought")),
        "visible": True if "visible" not in item else bool(item.get("visible")),
        "price": parse_price(item.get("price")),
    }


def normalize_wishes(items: list) -> list[dict]:
    wishes = []
    seen = set()
    for item in items or []:
        wish = normalize_wish(item)
        if not wish or wish["id"] in seen:
            continue
        seen.add(wish["id"])
        wishes.append(wish)
    return wishes


def for_agent(wishes: list[dict]) -> list[dict]:
    return [
        {"title": wish["title"], "details": wish["details"]}
        for wish in normalize_wishes(wishes)
        if wish["visible"]
    ]


def clean_wishes(text: str, previous: list[dict], proposed: list[dict]) -> list[dict]:
    previous_norm = normalize_wishes(previous)
    visible_prev = [wish for wish in previous_norm if wish["visible"]]
    hidden_prev = [wish for wish in previous_norm if not wish["visible"]]
    used: set[str] = set()
    cleaned = []
    seen_titles = set()

    def take_match(title: str) -> dict | None:
        key = title.casefold()

        def claim(pool: list[dict], fuzzy: bool) -> dict | None:
            for prev in pool:
                if prev["id"] in used:
                    continue
                other = prev["title"].casefold()
                if other == key or (fuzzy and other and (other in key or key in other)):
                    used.add(prev["id"])
                    return prev
            return None

        return claim(visible_prev, False) or claim(visible_prev, True) or claim(hidden_prev, False)

    for item in proposed:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        details = str(item.get("details") or item.get("note") or "").strip()
        if not title:
            continue
        key = title.casefold()
        if key in seen_titles:
            continue
        seen_titles.add(key)
        match = take_match(title)
        if match:
            cleaned.append(
                {
                    "id": match["id"],
                    "title": title,
                    "details": details,
                    "url": match["url"],
                    "notes": match["notes"],
                    "bought": match["bought"],
                    "visible": True,
                    "price": match.get("price"),
                }
            )
        else:
            cleaned.append(
                {
                    "id": uuid.uuid4().hex[:12],
                    "title": title,
                    "details": details,
                    "url": "",
                    "notes": "",
                    "bought": False,
                    "visible": True,
                    "price": None,
                }
            )

    known_urls = {wish["url"] for wish in cleaned if wish["url"]}
    fresh_ids = {wish["id"] for wish in cleaned} - {wish["id"] for wish in previous_norm}
    fresh = [wish for wish in cleaned if wish["id"] in fresh_ids and not wish["url"]]
    new_urls = []
    for raw_url in URL_RE.findall(text):
        url = raw_url.rstrip(").,]>")
        if url and url not in known_urls and url not in new_urls:
            new_urls.append(url)
    if len(cleaned) == 1 and len(new_urls) == 1 and not cleaned[0]["url"]:
        cleaned[0]["url"] = new_urls[0]
    elif len(fresh) == 1 and len(new_urls) == 1:
        fresh[0]["url"] = new_urls[0]
    else:
        for url in new_urls:
            cleaned.append(
                {
                    "id": uuid.uuid4().hex[:12],
                    "title": "Enlace",
                    "details": "",
                    "url": url,
                    "notes": "",
                    "bought": False,
                    "visible": True,
                    "price": None,
                }
            )

    lowered = text.casefold()
    cleared = any(hint in lowered for hint in CLEAR_HINTS)
    hidden_kept = [wish for wish in hidden_prev if wish["id"] not in used]
    if not cleaned and visible_prev and not cleared:
        return previous_norm
    return cleaned + hidden_kept


def fold(value: str) -> str:
    text = unicodedata.normalize("NFKD", value.casefold())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(text.split())


def find_member(label: str) -> tuple[dict | None, str]:
    folded = fold(label)
    if not folded:
        return None, "Falta el nombre de la persona."
    members = load_members()
    exact = [member for member in members if fold(member["name"]) == folded]
    if len(exact) == 1:
        return exact[0], ""
    tokens = folded.split()
    scored = []
    for member in members:
        parts = fold(member["name"]).split()
        if all(any(part.startswith(token) or token.startswith(part) for part in parts) for token in tokens):
            scored.append(member)
    if len(scored) == 1:
        return scored[0], ""
    if len(scored) > 1:
        names = ", ".join(member["name"] for member in scored)
        return None, f"Hay varios que encajan: {names}."
    return None, f"No encuentro a {label.strip()} en la casa."


def household_brief() -> list[dict]:
    brief = []
    for member in load_members():
        wishes = [
            {
                "title": wish["title"],
                "details": wish["details"],
                "bought": wish["bought"],
                "visible": wish["visible"],
            }
            for wish in load_wishes(member["phone"])
        ]
        brief.append(
            {
                "name": member["name"],
                "context": member.get("context") or "",
                "wishes": wishes,
            }
        )
    return brief


def apply_secret_gifts(actor: dict, gifts: list, own: list[dict]) -> tuple[list[dict], str]:
    notes = []
    actor_phone = str(actor.get("phone") or actor.get("key") or "")
    for gift in gifts or []:
        if not isinstance(gift, dict):
            continue
        title = str(gift.get("title") or "").strip()
        details = str(gift.get("details") or "").strip()
        if not title:
            continue
        target, problem = find_member(str(gift.get("member") or ""))
        if target is None:
            notes.append(problem)
            continue
        if target["phone"] == actor_phone:
            if not any(wish["title"].casefold() == title.casefold() for wish in own):
                own.append(
                    {
                        "id": uuid.uuid4().hex[:12],
                        "title": title,
                        "details": details,
                        "url": "",
                        "notes": "",
                        "bought": False,
                        "visible": True,
                        "price": None,
                    }
                )
            continue
        wishes = load_wishes(target["phone"])
        if any(wish["title"].casefold() == title.casefold() for wish in wishes):
            notes.append(f"«{title}» ya estaba para {target['name']}.")
            continue
        wishes.append(
            {
                "id": uuid.uuid4().hex[:12],
                "title": title,
                "details": details,
                "url": "",
                "notes": "",
                "bought": False,
                "visible": False,
                "price": None,
            }
        )
        save_wishes(
            {"key": target["phone"], "name": target["name"], "phone": target["phone"]},
            wishes,
        )
        log.info("secret gift for %s", target["name"])
    return own, " ".join(notes)


def compose_instruction(target: dict, instruction: str) -> str:
    visible = for_agent(load_wishes(target["phone"]))
    context = (target.get("context") or "").strip() or "No hay contexto. No inventes aficiones."
    system = (
        f"{load_prompt()}\n\n"
        f"Escribes tú a {target['name']} por WhatsApp.\n"
        f"Instrucción, síguela sin añadir otra cosa: {instruction}\n"
        "No digas que alguien te ha pedido escribir, ni quién.\n"
        "No añadas precios, enlaces ni si algo ya está comprado, salvo que la instrucción lo pida con esas palabras.\n"
        f"Lo que {target['name']} ya conoce:\n{json.dumps(visible, ensure_ascii=False)}\n"
        f"Contexto:\n{context}\n"
        "Responde solo con el texto del WhatsApp, sin comillas."
    )
    message = base_llm.invoke(
        [SystemMessage(content=system), HumanMessage(content=f"Escribe a {target['name']}.")]
    )
    return reply_text(message).strip().strip('"')


def deliver_instructions(actor: dict, orders: list, sender, writer, keep: bool = True) -> str:
    notes = []
    sent = 0
    actor_phone = str(actor.get("phone") or actor.get("key") or "")
    for order in orders or []:
        if sent >= 5:
            notes.append("He parado: como mucho cinco mensajes por vez.")
            break
        if not isinstance(order, dict):
            continue
        instruction = str(order.get("instruction") or "").strip()
        target, problem = find_member(str(order.get("member") or ""))
        if target is None:
            notes.append(problem)
            continue
        if target["phone"] == actor_phone:
            notes.append("Eso me lo dices tú; no me mando un mensaje a mí.")
            continue
        if not instruction:
            notes.append(f"Falta qué decirle a {target['name']}.")
            continue
        try:
            text = writer(target, instruction)
        except Exception:
            log.exception("instruction failed for %s", target["name"])
            notes.append(f"No he podido escribir a {target['name']}.")
            continue
        if not text:
            notes.append(f"No he podido escribir a {target['name']}.")
            continue
        chat_id = f"{target['phone']}@c.us"
        try:
            sender(chat_id, text, WAHA_SESSION)
        except Exception:
            log.exception("send failed for %s", target["name"])
            notes.append(f"No he podido enviar el mensaje a {target['name']}.")
            continue
        if keep:
            remember(chat_id, "", text)
            remember_outbound(chat_id, text)
        sent += 1
        log.info("instruction sent to %s", target["name"])
        notes.append(f"Ya se lo he escrito a {target['name']}.")
    return " ".join(notes)


def interpret(chat_id: str, member: dict, text: str) -> tuple[str, list[dict]]:
    current = load_wishes(member["key"])
    admin = bool(member.get("admin"))
    graph.invoke(
        {"messages": [HumanMessage(content=text)]},
        config={
            "configurable": {
                "thread_id": chat_id,
                "member_name": member["name"],
                "member_context": member.get("context") or "",
                "current_wishes": for_agent(current),
                "admin": admin,
                "household": household_brief() if admin else [],
            }
        },
    )
    turn = _last_turn.get(chat_id) or {
        "reply": "",
        "wishes": for_agent(current),
        "secret_gifts": [],
        "outbox": [],
    }
    wishes = clean_wishes(text, current, turn.get("wishes") or [])
    reply = (turn.get("reply") or "").strip()
    if not reply:
        reply = "Ya lo he apuntado en la carta."
    if admin:
        wishes, note = apply_secret_gifts(member, turn.get("secret_gifts") or [], wishes)
        sent = deliver_instructions(
            member,
            turn.get("outbox") or [],
            send_whatsapp,
            compose_instruction,
        )
        extra = " ".join(part for part in (note, sent) if part)
        if extra:
            reply = f"{reply}\n{extra}".strip()
    return reply, wishes


def send_whatsapp(chat_id: str, text: str, session: str) -> None:
    headers = {"Content-Type": "application/json"}
    if WAHA_API_KEY:
        headers["X-Api-Key"] = WAHA_API_KEY
    response = httpx.post(
        f"{WAHA_BASE_URL}/api/sendText",
        headers=headers,
        json={"session": session, "chatId": chat_id, "text": text},
        timeout=60,
    )
    response.raise_for_status()


def should_skip(payload: dict) -> str | None:
    if payload.get("fromMe"):
        return "fromMe"
    chat_id = str(payload.get("from") or "")
    if not chat_id:
        return "no chat"
    if payload.get("hasMedia") and not (payload.get("body") or "").strip():
        return "media only"
    if chat_id.endswith("@g.us") and not REPLY_TO_GROUPS:
        return "group"
    if chat_id.endswith("@broadcast") or chat_id.endswith("@newsletter"):
        return "broadcast"
    if not (payload.get("body") or "").strip():
        return "empty"
    return None


def household() -> list[dict]:
    rows = _db.execute(
        "SELECT member_key, name, phone, wishes_json, updated_at FROM lists ORDER BY name COLLATE NOCASE"
    ).fetchall()
    saved = {row[0]: row for row in rows}
    people = []
    for member in load_members():
        row = saved.get(member["phone"])
        wishes = []
        updated = None
        if row and row[3]:
            try:
                wishes = normalize_wishes(json.loads(row[3]))
            except json.JSONDecodeError:
                wishes = []
            updated = row[4]
        people.append(
            {
                "name": member["name"],
                "phone": member["phone"],
                "admin": bool(member.get("admin")),
                "wishes": wishes,
                "updatedAt": updated,
            }
        )
    if people:
        return people
    for row in rows:
        try:
            wishes = normalize_wishes(json.loads(row[3] or "[]"))
        except json.JSONDecodeError:
            wishes = []
        people.append({"name": row[1], "phone": row[2], "wishes": wishes, "updatedAt": row[4]})
    return people


@app.get("/health")
def health():
    return {
        "ok": True,
        "llm": llm_ready(),
        "session": WAHA_SESSION,
        "members": len(load_members()),
        "nudgeEveryDays": env_float("NUDGE_EVERY_DAYS", 2),
    }


@app.get("/api/wishes")
def wishes_api():
    return {"members": household()}


@app.put("/api/wishes/{phone}")
def update_member_wishes(phone: str, items: list[WishWrite]):
    number = digits(phone)
    member = next((item for item in load_members() if same_phone(number, item["phone"])), None)
    if member is None:
        raise HTTPException(status_code=404, detail="member not found")
    wishes = normalize_wishes([item.model_dump() for item in items])
    with _lock:
        previous = {wish["id"]: wish for wish in load_wishes(member["phone"])}
        for wish in wishes:
            old = previous.get(wish["id"])
            wish["visible"] = old["visible"] if old else False
        save_wishes(
            {"key": member["phone"], "name": member["name"], "phone": member["phone"]},
            wishes,
        )
    return {"ok": True, "wishes": wishes}


@app.get("/", response_class=HTMLResponse)
def dashboard():
    days = env_float("NUDGE_EVERY_DAYS", 2)
    day_label = f"{days:g} día" if days == 1 else f"{days:g} días"
    lead = (
        "Lo que cada persona de la casa ha pedido. "
        f"Si alguien no ha pedido nada, se le pregunta cada {day_label}. "
        "El enlace, las notas, el precio y si ya está comprado no llegan al chat. "
        "La suma, al lado del nombre, es el presupuesto. "
        "Lo que ha pedido la persona va más oscuro y se ve en su chat. "
        "Lo que añadís vosotros queda oculto, y solo eso se puede quitar."
    )
    return HTMLResponse(
        """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Carta de Reyes</title>
<style>
  :root { color-scheme: dark; }
  body { margin: 0; font-family: Georgia, "Iowan Old Style", serif; background: #1c140c; color: #f4e7d0; }
  main { max-width: 1440px; margin: 0 auto; padding: 32px 20px 64px; }
  h1 { font-weight: 500; letter-spacing: 0.04em; margin-bottom: 0; }
  p.lead { color: #d7c4a3; margin-top: 8px; }
  .sheet { overflow-x: auto; background: #2a1d12; border: 1px solid #6b4b24; border-radius: 16px; }
  table { width: 100%; border-collapse: collapse; min-width: 1220px; }
  tr.said td { background: #120c08; }
  tr.said input { background: #0e0a07; border-color: #4a3218; }
  .role { display: block; margin-top: 4px; color: #f0c36a; font-size: 0.8rem; }
  .total { margin-left: 0.45rem; color: #f0c36a; font-size: 1rem; }
  th {
    position: sticky; top: 0; text-align: left; font-size: 0.75rem; font-weight: 500;
    letter-spacing: 0.04em; text-transform: uppercase; color: #d7c4a3;
    background: #2a1d12; padding: 12px 10px; border-bottom: 1px solid #6b4b24;
  }
  th.flag { text-align: center; }
  th.when { text-align: right; }
  td { padding: 8px 10px; border-top: 1px solid #6b4b24; vertical-align: middle; }
  td.who { width: 11rem; font-size: 1.05rem; }
  td.price { width: 7rem; }
  td.flag { width: 6.2rem; text-align: center; }
  td.when { width: 9.5rem; text-align: right; white-space: nowrap; color: #b7a48a; font-size: 0.85rem; }
  td.drop { width: 5.5rem; text-align: center; }
  input {
    width: 100%; box-sizing: border-box; font: 1rem Georgia, "Iowan Old Style", serif;
    color: #f4e7d0; background: #1c140c; border: 1px solid #6b4b24; border-radius: 8px; padding: 8px 10px;
  }
  input[data-field="price"] { text-align: right; }
  button.add, button.remove {
    display: inline-block; padding: 4px 10px; cursor: pointer;
    color: #f0c36a; background: transparent; border: 1px solid #6b4b24; border-radius: 999px;
    font: 0.85rem Georgia, "Iowan Old Style", serif;
  }
  button.add { margin-top: 8px; }
  .switch { position: relative; display: inline-flex; align-items: center; gap: 8px; cursor: pointer; text-transform: none; letter-spacing: 0; font-size: 0.95rem; }
  .switch input { position: absolute; opacity: 0; width: 1px; height: 1px; }
  .track {
    position: relative; width: 44px; height: 26px; border-radius: 999px;
    background: #3a2a18; border: 1px solid #6b4b24; flex: 0 0 auto;
  }
  .track::after {
    content: ""; position: absolute; width: 18px; height: 18px; left: 3px; top: 3px;
    border-radius: 50%; background: #b7a48a;
  }
  .switch input:checked + .track { background: #6b4b24; }
  .switch input:checked + .track::after { transform: translateX(18px); background: #f0c36a; }
  .switch input:focus-visible + .track { outline: 2px solid #f0c36a; outline-offset: 2px; }
  .bought input[data-field="title"] { text-decoration: line-through; opacity: 0.7; }
  .empty, .muted, .status, time { color: #b7a48a; }
  .status, time { display: block; margin-top: 6px; font-size: 0.8rem; }
</style>
</head>
<body>
<main>
  <h1>Carta de Reyes</h1>
  <p class="lead">LEAD</p>
  <div id="list"></div>
</main>
<script>
function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, ch => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[ch]));
}
function parsePrice(value) {
  const text = String(value ?? "").replace(/€/g, "").replace(/\s/g, "").replace(",", ".");
  if (!text) return null;
  const number = Number(text);
  if (!Number.isFinite(number) || number < 0) return null;
  return Math.round(number * 100) / 100;
}
function formatPrice(value) {
  const number = typeof value === "number" ? value : parsePrice(value);
  if (number === null) return "";
  return number.toLocaleString("es-ES", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function formatTotal(value) {
  return value.toLocaleString("es-ES", { style: "currency", currency: "EUR" });
}
function formatWhen(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("es-ES", {
    timeZone: "Europe/Madrid",
    day: "2-digit", month: "2-digit", year: "numeric",
    hour: "2-digit", minute: "2-digit"
  });
}
function sumWishes(wishes) {
  return (wishes || []).reduce((acc, wish) => acc + (Number(wish.price) || 0), 0);
}
function flagCell(field, on, label) {
  const checked = on ? " checked" : "";
  return `<td class="flag"><label class="switch"><input data-field="${field}" type="checkbox"${checked} aria-label="${label}"><span class="track"></span></label></td>`;
}
function render(data) {
  const root = document.getElementById("list");
  const members = data.members || [];
  if (!members.length) {
    root.innerHTML = '<p class="empty">Todavía no hay nadie en la carta. Cuando escriban por WhatsApp, aparecerán aquí.</p>';
    return;
  }
  const rows = members.map(member => {
    const wishes = member.wishes || [];
    const phone = escapeHtml(member.phone);
    const role = member.admin ? '<span class="role">Organiza</span>' : "";
    const add = `<button type="button" class="add" data-add="${phone}">Añadir</button>`;
    const total = `<span class="total">${escapeHtml(formatTotal(sumWishes(wishes)))}</span>`;
    const whoCell = `<td class="who" rowspan="ROWS">${escapeHtml(member.name)}${total}${role}<span class="status"></span>${add}</td>`;
    const when = `<td class="when" rowspan="ROWS">${escapeHtml(formatWhen(member.updatedAt))}</td>`;
    if (!wishes.length) {
      return `<tr data-phone="${phone}">${whoCell.replace("ROWS", "1")}<td class="muted" colspan="7">Todavía no ha pedido nada.</td>${when.replace("ROWS", "1")}</tr>`;
    }
    return wishes.map((wish, index) => {
      const bought = wish.bought ? " bought" : "";
      const said = wish.visible === false ? "" : " said";
      const secret = wish.visible === false ? " secret" : "";
      const who = index === 0 ? whoCell.replaceAll("ROWS", String(wishes.length)) : "";
      const updated = index === 0 ? when.replaceAll("ROWS", String(wishes.length)) : "";
      const ours = wish.visible === false;
      return `<tr class="wish${bought}${said}${secret}" data-phone="${phone}" data-id="${escapeHtml(wish.id || "")}" data-visible="${ours ? "0" : "1"}">
        ${who}
        <td><input data-field="title" type="text" value="${escapeHtml(wish.title || "")}" aria-label="Nombre"></td>
        <td><input data-field="details" type="text" value="${escapeHtml(wish.details || "")}" placeholder="talla, color" aria-label="Características"></td>
        <td><input data-field="url" type="text" value="${escapeHtml(wish.url || "")}" placeholder="https://" aria-label="URL"></td>
        <td><input data-field="notes" type="text" value="${escapeHtml(wish.notes || "")}" aria-label="Notas"></td>
        <td class="price"><input data-field="price" type="text" inputmode="decimal" value="${escapeHtml(formatPrice(wish.price))}" placeholder="0,00" aria-label="Precio"></td>
        ${flagCell("bought", Boolean(wish.bought), "Comprado")}
        <td class="drop">${ours ? '<button type="button" class="remove" data-remove>Quitar</button>' : ""}</td>
        ${updated}
      </tr>`;
    }).join("");
  }).join("");
  root.innerHTML = `<div class="sheet"><table>
    <thead><tr>
      <th>Persona</th><th>Nombre</th><th>Características</th><th>URL</th><th>Notas</th><th>Precio</th>
      <th class="flag">Comprado</th><th></th><th class="when">Actualizado</th>
    </tr></thead>
    <tbody>${rows}</tbody>
  </table></div>`;
}
const saveTimers = new Map();
const dirtyPhones = new Set();
let lastPayload = "";
let lastData = { members: [] };
function wishRows(phone) {
  return [...document.querySelectorAll("tr.wish")].filter(row => row.dataset.phone === phone);
}
function rowWish(row, trim) {
  const read = field => {
    const value = row.querySelector(`[data-field="${field}"]`).value;
    return trim ? value.trim() : value;
  };
  return {
    id: row.dataset.id || "",
    title: read("title"),
    details: read("details"),
    url: read("url"),
    notes: read("notes"),
    bought: row.querySelector('[data-field="bought"]').checked,
    visible: row.dataset.visible !== "0",
    price: trim ? parsePrice(row.querySelector('[data-field="price"]').value) : row.querySelector('[data-field="price"]').value
  };
}
function rowHasContent(wish) {
  return Boolean(wish.title || wish.details || wish.url || wish.notes || wish.price);
}
function wishesFrom(phone) {
  return wishRows(phone).map(row => rowWish(row, true)).filter(rowHasContent);
}
function blankPending(phone) {
  return wishRows(phone).some(row => !rowHasContent(rowWish(row, true)));
}
function paintTotal(phone) {
  const total = document.querySelector(`tr[data-phone="${phone}"] .total`);
  if (!total) return;
  const amount = wishRows(phone).reduce((acc, row) => acc + (parsePrice(row.querySelector('[data-field="price"]').value) || 0), 0);
  total.textContent = formatTotal(amount);
}
async function savePhone(phone) {
  const status = document.querySelector(`tr[data-phone="${phone}"] .status`);
  if (status) status.textContent = "Guardando…";
  try {
    const response = await fetch("/api/wishes/" + encodeURIComponent(phone), {
      method: "PUT",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(wishesFrom(phone))
    });
    if (!response.ok) throw new Error("save");
    const data = await response.json();
    const nodes = wishRows(phone).filter(row => rowHasContent(rowWish(row, true)));
    (data.wishes || []).forEach((wish, index) => {
      if (nodes[index]) nodes[index].dataset.id = wish.id || "";
    });
    if (blankPending(phone)) dirtyPhones.add(phone);
    else dirtyPhones.delete(phone);
    if (status) status.textContent = "Guardado";
  } catch (error) {
    if (status) status.textContent = "No se ha podido guardar";
  }
}
function scheduleSave(phone) {
  dirtyPhones.add(phone);
  wishRows(phone).forEach(row => {
    const box = row.querySelector('[data-field="bought"]');
    row.classList.toggle("bought", Boolean(box && box.checked));
  });
  paintTotal(phone);
  clearTimeout(saveTimers.get(phone));
  saveTimers.set(phone, setTimeout(() => savePhone(phone), 400));
}
function addWish(phone) {
  const member = (lastData.members || []).find(item => item.phone === phone);
  if (!member) return;
  member.wishes = wishRows(phone).map(row => rowWish(row, false));
  member.wishes.push({ id: "", title: "", details: "", url: "", notes: "", bought: false, visible: false, price: null });
  dirtyPhones.add(phone);
  render(lastData);
  const title = wishRows(phone).at(-1).querySelector('[data-field="title"]');
  if (title) title.focus();
}
document.getElementById("list").addEventListener("input", event => {
  const row = event.target.closest("tr.wish");
  if (row) scheduleSave(row.dataset.phone);
});
document.getElementById("list").addEventListener("change", event => {
  const row = event.target.closest("tr.wish");
  if (row) scheduleSave(row.dataset.phone);
});
function removeWish(row) {
  if (row.dataset.visible !== "0") return;
  const phone = row.dataset.phone;
  const member = (lastData.members || []).find(item => item.phone === phone);
  if (!member) return;
  member.wishes = wishRows(phone).filter(item => item !== row).map(item => rowWish(item, false));
  dirtyPhones.add(phone);
  render(lastData);
  savePhone(phone);
}
document.getElementById("list").addEventListener("click", event => {
  const add = event.target.closest("[data-add]");
  if (add) addWish(add.dataset.add);
  const remove = event.target.closest("[data-remove]");
  if (remove) removeWish(remove.closest("tr.wish"));
});
async function load() {
  const root = document.getElementById("list");
  if (dirtyPhones.size || root.contains(document.activeElement)) return;
  try {
    const response = await fetch("/api/wishes", { cache: "no-store" });
    const data = await response.json();
    const payload = JSON.stringify(data.members || []);
    if (payload === lastPayload) return;
    lastPayload = payload;
    lastData = data;
    render(data);
  } catch (error) {
    if (!root.childElementCount) {
      root.innerHTML = '<p class="empty">No se ha podido cargar la carta.</p>';
    }
  }
}
load();
setInterval(load, 8000);
</script>
</body>
</html>""".replace("LEAD", lead)
    )


@app.get("/chats")
def list_chats():
    rows = _db.execute(
        "SELECT chat_id, last_text, last_reply, updated_at FROM chats ORDER BY updated_at DESC"
    ).fetchall()
    return [
        {"chatId": row[0], "lastText": row[1], "lastReply": row[2], "updatedAt": row[3]}
        for row in rows
    ]


@app.post("/webhook")
async def webhook(request: Request):
    body = await request.json()
    if body.get("event") != "message":
        return {"ok": True, "ignored": body.get("event") or "none"}

    payload = body.get("payload") or {}
    reason = should_skip(payload)
    if reason:
        return {"ok": True, "ignored": reason}

    message_id = str(payload.get("id") or "")
    if message_id:
        with _lock:
            if message_id in _seen:
                return {"ok": True, "ignored": "duplicate"}
            _seen.add(message_id)
            if len(_seen) > 2000:
                _seen.clear()
                _seen.add(message_id)

    chat_id = str(payload["from"])
    member = identify(chat_id, payload)
    session = body.get("session") or WAHA_SESSION
    if member is None or not member.get("known"):
        log.info("ignored, number is not on the list")
        send_whatsapp(
            chat_id,
            "Esta carta es solo para la casa. Si Sus Majestades te esperaban, que apunten tu número.",
            session,
        )
        return {"ok": True, "ignored": "unknown member"}
    chat_id = member["reply_to"]

    if graph is None:
        log.error("LLM is not configured.")
        return {"ok": False, "error": "llm_not_configured"}

    text = str(payload.get("body") or "").strip()
    log.info("carta de %s", member["name"])
    try:
        with _lock:
            reply, wishes = interpret(chat_id, member, text)
            save_wishes(member, wishes)
            remember(chat_id, text, reply)
        send_whatsapp(chat_id, reply, session)
    except Exception:
        log.exception("failed to answer %s", member["name"])
        if message_id:
            with _lock:
                _seen.discard(message_id)
        raise
    return {"ok": True, "chatId": chat_id, "wishes": len(wishes)}


def last_nudge(member_key: str) -> tuple[datetime | None, str]:
    row = _db.execute(
        "SELECT last_sent_at, last_text FROM nudges WHERE member_key = ?",
        (member_key,),
    ).fetchone()
    if not row or not row[0]:
        return None, ""
    try:
        sent = datetime.fromisoformat(row[0])
    except ValueError:
        return None, row[1] or ""
    return sent, row[1] or ""


def save_nudge(member_key: str, text: str) -> None:
    _db.execute(
        """
        INSERT INTO nudges (member_key, last_sent_at, last_text)
        VALUES (?, ?, ?)
        ON CONFLICT(member_key) DO UPDATE SET
            last_sent_at = excluded.last_sent_at,
            last_text = excluded.last_text
        """,
        (member_key, now(), text),
    )
    _db.commit()


def within_nudge_hours() -> bool:
    hour = datetime.now(ZoneInfo("Europe/Madrid")).hour
    return 9 <= hour < 21


def compose_nudge(member: dict, previous: str) -> str:
    previous_line = previous.strip() or "ninguno"
    context = (member.get("context") or "").strip()
    context_line = (
        context
        or "No hay contexto. No inventes aficiones: pregunta qué le ayudaría a descansar y a estar con amigos."
    )
    system = (
        f"{load_prompt()}\n\n"
        "TAREA DE ESTE TURNO:\n"
        f"Escribes tú primero a {member['name']}. Su lista está vacía.\n"
        f"Contexto de esta persona:\n{context_line}\n"
        f"Último mensaje que ya le enviaste: {previous_line}\n"
        "Sé proactivo. Sugiere una o dos cosas concretas, sacadas solo de ese contexto, "
        "que le ayuden a descansar o a pasar un buen rato con amigos. "
        "Ofrécelas para que las quiera, las cambie o las ignore. No las des por pedidas. "
        "No repitas el último mensaje.\n"
        "Responde solo con el texto del WhatsApp, sin comillas y sin JSON."
    )
    message = base_llm.invoke(
        [SystemMessage(content=system), HumanMessage(content=f"Escribe a {member['name']}.")]
    )
    return reply_text(message).strip().strip('"')


def remember_outbound(chat_id: str, text: str) -> None:
    if graph is None:
        return
    graph.update_state(
        {"configurable": {"thread_id": chat_id}},
        {"messages": [AIMessage(content=text)]},
        as_node="respond",
    )


def nudge_due(member: dict) -> tuple[bool, str]:
    if any(wish["visible"] for wish in load_wishes(member["phone"])):
        return False, ""
    sent, previous = last_nudge(member["phone"])
    interval = timedelta(days=env_float("NUDGE_EVERY_DAYS", 2))
    if sent is not None and datetime.now(timezone.utc) - sent < interval:
        return False, previous
    return True, previous


def run_nudges() -> None:
    if graph is None or base_llm is None:
        log.error("nudge skipped: LLM is not configured")
        return
    if env_float("NUDGE_EVERY_DAYS", 2) <= 0 or not within_nudge_hours():
        return
    sent = 0
    for member in load_members():
        with _lock:
            due, previous = nudge_due(member)
        if not due:
            continue
        chat_id = f"{member['phone']}@c.us"
        try:
            text = compose_nudge(member, previous)
            if not text:
                continue
            send_whatsapp(chat_id, text, WAHA_SESSION)
            with _lock:
                save_nudge(member["phone"], text)
                remember(chat_id, "", text)
                remember_outbound(chat_id, text)
            log.info("nudge sent to %s", member["name"])
            sent += 1
        except Exception:
            log.exception("nudge failed for %s", member["name"])
        time.sleep(8)
    if sent == 0:
        log.info("no reminders sent; restart does not message people again")


def nudge_loop() -> None:
    # El arranque no escribe a nadie. Solo se avisa cuando pasan los días.
    while True:
        time.sleep(30 * 60)
        try:
            run_nudges()
        except Exception:
            log.exception("nudge loop failed")


threading.Thread(target=nudge_loop, name="nudge", daemon=True).start()
