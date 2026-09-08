#!/usr/bin/env python3
"""A tiny durable chat relay for humans and coding agents.

Agent protocol: newline-delimited JSON over TCP.
Human interface: a small HTTP API and browser UI.
Persistence: SQLite.

Only Python's standard library is used.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import signal
import socketserver
import sqlite3
import threading
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse


PROTOCOL = "agent-chat/1"
MAX_TEXT = 100_000
MAX_HTTP_BODY = 1_000_000


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ChatError(Exception):
    pass


def clean_label(value: object, kind: str, maximum: int = 100) -> str:
    if not isinstance(value, str):
        raise ChatError(f"{kind} must be a string")
    value = value.strip()
    if not value or len(value) > maximum:
        raise ChatError(f"{kind} must contain 1..{maximum} characters")
    if "/" in value or any(ord(char) < 32 for char in value):
        raise ChatError(f"{kind} must not contain slashes or control characters")
    return value


def clean_text(value: object) -> str:
    if not isinstance(value, str):
        raise ChatError("text must be a string")
    value = value.strip()
    if not value:
        raise ChatError("text must not be empty")
    if len(value) > MAX_TEXT:
        raise ChatError(f"text is longer than {MAX_TEXT} characters")
    return value


def render_inline_markdown(text: str) -> str:
    """Render a deliberately small, HTML-safe subset of inline Markdown."""
    code_spans: list[str] = []
    links: list[str] = []

    def save_link(match: re.Match[str]) -> str:
        label, url = match.group(1), match.group(2).strip()
        parsed = urlparse(url)
        if parsed.scheme.lower() not in {"http", "https", "mailto"}:
            return match.group(0)
        links.append(
            f'<a href="{html.escape(url, quote=True)}" target="_blank" '
            f'rel="noopener noreferrer">{render_inline_markdown(label)}</a>'
        )
        return f"\x00LINK{len(links) - 1}\x00"

    def save_code(match: re.Match[str]) -> str:
        code_spans.append(f"<code>{html.escape(match.group(1))}</code>")
        return f"\x00CODE{len(code_spans) - 1}\x00"

    rendered = re.sub(r"`([^`\n]+)`", save_code, text)
    rendered = re.sub(r"\[([^]\n]+)\]\(([^)\s]+)\)", save_link, rendered)
    rendered = html.escape(rendered)
    rendered = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", rendered)
    rendered = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", rendered)
    for index, code in enumerate(code_spans):
        rendered = rendered.replace(f"\x00CODE{index}\x00", code)
    for index, link in enumerate(links):
        rendered = rendered.replace(f"\x00LINK{index}\x00", link)
    return rendered


def render_markdown(text: str) -> str:
    """Render message Markdown without trusting raw HTML from participants."""
    lines = text.splitlines()
    output: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue

        fence = re.match(r"^```([A-Za-z0-9_+-]*)\s*$", line)
        if fence:
            language = fence.group(1)
            index += 1
            code_lines: list[str] = []
            while index < len(lines) and not re.match(r"^```\s*$", lines[index]):
                code_lines.append(lines[index])
                index += 1
            if index < len(lines):
                index += 1
            language_attr = f' class="language-{language}"' if language else ""
            output.append(
                f"<pre><code{language_attr}>{html.escape(chr(10).join(code_lines))}</code></pre>"
            )
            continue

        if index + 1 < len(lines) and "|" in line:
            header_cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            separator_cells = [cell.strip() for cell in lines[index + 1].strip().strip("|").split("|")]
            if (
                len(header_cells) == len(separator_cells)
                and len(header_cells) > 1
                and all(re.fullmatch(r":?-{3,}:?", cell) for cell in separator_cells)
            ):
                index += 2
                rows: list[list[str]] = []
                while index < len(lines) and "|" in lines[index] and lines[index].strip():
                    cells = [cell.strip() for cell in lines[index].strip().strip("|").split("|")]
                    if len(cells) != len(header_cells):
                        break
                    rows.append(cells)
                    index += 1
                header_html = "".join(
                    f"<th>{render_inline_markdown(cell)}</th>" for cell in header_cells
                )
                rows_html = "".join(
                    "<tr>" + "".join(f"<td>{render_inline_markdown(cell)}</td>" for cell in row) + "</tr>"
                    for row in rows
                )
                output.append(f"<table><thead><tr>{header_html}</tr></thead><tbody>{rows_html}</tbody></table>")
                continue

        heading = re.match(r"^(#{1,6})\s+(.+)$", line)
        if heading:
            level = len(heading.group(1))
            output.append(f"<h{level}>{render_inline_markdown(heading.group(2))}</h{level}>")
            index += 1
            continue

        unordered = re.match(r"^\s*[-+*]\s+(.+)$", line)
        if unordered:
            items: list[str] = []
            while index < len(lines):
                item = re.match(r"^\s*[-+*]\s+(.+)$", lines[index])
                if not item:
                    break
                items.append(f"<li>{render_inline_markdown(item.group(1))}</li>")
                index += 1
            output.append("<ul>" + "".join(items) + "</ul>")
            continue

        ordered = re.match(r"^\s*\d+[.)]\s+(.+)$", line)
        if ordered:
            items = []
            while index < len(lines):
                item = re.match(r"^\s*\d+[.)]\s+(.+)$", lines[index])
                if not item:
                    break
                items.append(f"<li>{render_inline_markdown(item.group(1))}</li>")
                index += 1
            output.append("<ol>" + "".join(items) + "</ol>")
            continue

        paragraph = [line]
        index += 1
        while index < len(lines) and lines[index].strip():
            if re.match(r"^(#{1,6})\s+|^\s*[-+*]\s+|^\s*\d+[.)]\s+", lines[index]):
                break
            paragraph.append(lines[index])
            index += 1
        output.append("<p>" + "<br>".join(render_inline_markdown(item) for item in paragraph) + "</p>")
    return "".join(output)


class Store:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self.connect() as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS rooms (
                    id          INTEGER PRIMARY KEY,
                    name        TEXT NOT NULL UNIQUE,
                    created_at  TEXT NOT NULL,
                    created_by  TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS participants (
                    room_id       INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
                    name          TEXT NOT NULL,
                    joined_at     TEXT NOT NULL,
                    last_seen_at  TEXT NOT NULL,
                    last_read_id  INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (room_id, name)
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    room_id     INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
                    sender      TEXT NOT NULL,
                    kind        TEXT NOT NULL DEFAULT 'message',
                    body        TEXT NOT NULL,
                    created_at  TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS messages_room_id_id
                    ON messages(room_id, id);
                """
            )

    @staticmethod
    def _row_dict(row: sqlite3.Row) -> dict:
        return dict(row)

    def _room_id(self, db: sqlite3.Connection, room: str) -> int:
        row = db.execute("SELECT id FROM rooms WHERE name = ?", (room,)).fetchone()
        if row is None:
            raise ChatError(f"chat {room!r} does not exist")
        return int(row["id"])

    def create_room(self, room: str, creator: str) -> dict:
        room = clean_label(room, "chat name")
        creator = clean_label(creator, "participant name", 80)
        now = utc_now()
        try:
            with self.connect() as db:
                cursor = db.execute(
                    "INSERT INTO rooms(name, created_at, created_by) VALUES (?, ?, ?)",
                    (room, now, creator),
                )
                room_id = int(cursor.lastrowid)
                db.execute(
                    """INSERT INTO participants
                       (room_id, name, joined_at, last_seen_at, last_read_id)
                       VALUES (?, ?, ?, ?, 0)""",
                    (room_id, creator, now, now),
                )
                message = db.execute(
                    """INSERT INTO messages(room_id, sender, kind, body, created_at)
                       VALUES (?, 'system', 'system', ?, ?) RETURNING id""",
                    (room_id, f"{creator} created chat {room}", now),
                ).fetchone()
                message_id = int(message["id"])
        except sqlite3.IntegrityError as error:
            raise ChatError(f"chat {room!r} already exists") from error
        return {"name": room, "created_by": creator, "created_at": now, "message_id": message_id}

    def join(self, room: str, name: str) -> dict:
        room = clean_label(room, "chat name")
        name = clean_label(name, "participant name", 80)
        now = utc_now()
        with self.connect() as db:
            room_id = self._room_id(db, room)
            db.execute(
                """INSERT INTO participants
                   (room_id, name, joined_at, last_seen_at, last_read_id)
                   VALUES (?, ?, ?, ?, 0)
                   ON CONFLICT(room_id, name) DO UPDATE SET last_seen_at = excluded.last_seen_at""",
                (room_id, name, now, now),
            )
        return {"chat": room, "name": name, "joined_at": now}

    def leave(self, room: str, name: str) -> None:
        room = clean_label(room, "chat name")
        name = clean_label(name, "participant name", 80)
        with self.connect() as db:
            room_id = self._room_id(db, room)
            db.execute("DELETE FROM participants WHERE room_id = ? AND name = ?", (room_id, name))

    def post(self, room: str, sender: str, text: str, kind: str = "message") -> dict:
        room = clean_label(room, "chat name")
        sender = clean_label(sender, "participant name", 80)
        text = clean_text(text)
        now = utc_now()
        with self.connect() as db:
            room_id = self._room_id(db, room)
            db.execute(
                """INSERT INTO participants
                   (room_id, name, joined_at, last_seen_at, last_read_id)
                   VALUES (?, ?, ?, ?, 0)
                   ON CONFLICT(room_id, name) DO UPDATE SET last_seen_at = excluded.last_seen_at""",
                (room_id, sender, now, now),
            )
            cursor = db.execute(
                """INSERT INTO messages(room_id, sender, kind, body, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (room_id, sender, kind, text, now),
            )
            message_id = int(cursor.lastrowid)
        return {
            "id": message_id,
            "chat": room,
            "sender": sender,
            "kind": kind,
            "body": text,
            "created_at": now,
        }

    def history(self, room: str, after: int = 0, limit: int = 500) -> list[dict]:
        room = clean_label(room, "chat name")
        after = max(0, int(after))
        limit = min(max(1, int(limit)), 2_000)
        with self.connect() as db:
            room_id = self._room_id(db, room)
            rows = db.execute(
                """SELECT m.id, r.name AS chat, m.sender, m.kind, m.body, m.created_at
                   FROM messages m JOIN rooms r ON r.id = m.room_id
                   WHERE m.room_id = ? AND m.id > ? ORDER BY m.id LIMIT ?""",
                (room_id, after, limit),
            ).fetchall()
        return [self._row_dict(row) for row in rows]

    def receive_unread(self, room: str, name: str, limit: int = 100) -> tuple[list[dict], bool]:
        """Return unread messages from others and advance over all inspected messages."""
        room = clean_label(room, "chat name")
        name = clean_label(name, "participant name", 80)
        limit = min(max(1, int(limit)), 1_000)
        now = utc_now()
        with self.connect() as db:
            room_id = self._room_id(db, room)
            participant = db.execute(
                "SELECT last_read_id FROM participants WHERE room_id = ? AND name = ?",
                (room_id, name),
            ).fetchone()
            if participant is None:
                db.execute(
                    """INSERT INTO participants
                       (room_id, name, joined_at, last_seen_at, last_read_id)
                       VALUES (?, ?, ?, ?, 0)""",
                    (room_id, name, now, now),
                )
                cursor = 0
            else:
                cursor = int(participant["last_read_id"])

            rows = db.execute(
                """SELECT m.id, r.name AS chat, m.sender, m.kind, m.body, m.created_at
                   FROM messages m JOIN rooms r ON r.id = m.room_id
                   WHERE m.room_id = ? AND m.id > ? ORDER BY m.id LIMIT ?""",
                (room_id, cursor, limit),
            ).fetchall()
            if rows:
                cursor = int(rows[-1]["id"])
            db.execute(
                """UPDATE participants SET last_read_id = ?, last_seen_at = ?
                   WHERE room_id = ? AND name = ?""",
                (cursor, now, room_id, name),
            )
        visible = [self._row_dict(row) for row in rows if row["sender"] != name]
        return visible, bool(rows)

    def mark_read(self, room: str, name: str, message_id: int) -> None:
        room = clean_label(room, "chat name")
        name = clean_label(name, "participant name", 80)
        message_id = max(0, int(message_id))
        now = utc_now()
        with self.connect() as db:
            room_id = self._room_id(db, room)
            db.execute(
                """INSERT INTO participants
                   (room_id, name, joined_at, last_seen_at, last_read_id)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(room_id, name) DO UPDATE SET
                       last_seen_at = excluded.last_seen_at,
                       last_read_id = MAX(participants.last_read_id, excluded.last_read_id)""",
                (room_id, name, now, now, message_id),
            )

    def list_rooms(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                """SELECT r.name, r.created_at, r.created_by,
                          COUNT(DISTINCT p.name) AS participant_count,
                          COALESCE(MAX(m.id), 0) AS last_message_id
                   FROM rooms r
                   LEFT JOIN participants p ON p.room_id = r.id
                   LEFT JOIN messages m ON m.room_id = r.id
                   GROUP BY r.id ORDER BY last_message_id DESC, r.name"""
            ).fetchall()
        return [self._row_dict(row) for row in rows]

    def members(self, room: str) -> list[dict]:
        room = clean_label(room, "chat name")
        with self.connect() as db:
            room_id = self._room_id(db, room)
            rows = db.execute(
                """SELECT name, joined_at, last_seen_at, last_read_id
                   FROM participants WHERE room_id = ? ORDER BY name""",
                (room_id,),
            ).fetchall()
        return [self._row_dict(row) for row in rows]


class Hub:
    def __init__(self, store: Store):
        self.store = store
        self._lock = threading.Lock()
        self._conditions: dict[str, threading.Condition] = {}

    def condition(self, room: str) -> threading.Condition:
        with self._lock:
            return self._conditions.setdefault(room, threading.Condition())

    def notify(self, room: str) -> None:
        condition = self.condition(room)
        with condition:
            condition.notify_all()

    def post(self, room: str, sender: str, text: str) -> dict:
        message = self.store.post(room, sender, text)
        self.notify(room)
        return message

    def receive(self, room: str, name: str, wait_seconds: float, limit: int = 100) -> list[dict]:
        room = clean_label(room, "chat name")
        wait_seconds = min(max(0.0, float(wait_seconds)), 300.0)
        deadline = time.monotonic() + wait_seconds
        condition = self.condition(room)
        with condition:
            while True:
                messages, inspected_any = self.store.receive_unread(room, name, limit)
                if messages:
                    return messages
                # We may have advanced over only the caller's own messages.
                if inspected_any:
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return []
                condition.wait(remaining)


WELCOME = {
    "type": "welcome",
    "protocol": PROTOCOL,
    "summary": "Durable named chat rooms for humans and agents.",
    "first_step": {"op": "hello", "name": "unique-agent-name"},
    "commands": {
        "create": {"op": "create", "chat": "name"},
        "join": {"op": "join", "chat": "name"},
        "send": {"op": "send", "chat": "name", "text": "message"},
        "receive": {"op": "receive", "chat": "name", "wait": 55},
        "history": {"op": "history", "chat": "name", "after": 0},
        "list": {"op": "list"},
        "who": {"op": "who", "chat": "name"},
        "leave": {"op": "leave", "chat": "name"},
    },
    "agent_loop": [
        "Use a unique name for this T3 thread.",
        "Join or create the requested chat.",
        "Send your contribution, then call receive with wait=55.",
        "When messages arrive, reason about them and send a reply.",
        "Repeat receive while the user wants you to remain responsive.",
        "Return to the T3 user with a concise result after leaving the chat.",
    ],
}


class AgentTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: tuple[str, int], hub: Hub):
        super().__init__(address, AgentHandler)
        self.hub = hub


class AgentHandler(socketserver.StreamRequestHandler):
    server: AgentTCPServer

    def send_json(self, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        self.wfile.write(data.encode("utf-8"))
        self.wfile.flush()

    def handle(self) -> None:
        self.send_json(WELCOME)
        name: str | None = None
        while True:
            raw = self.rfile.readline(MAX_HTTP_BODY + 1)
            if not raw:
                return
            if len(raw) > MAX_HTTP_BODY:
                self.send_json({"ok": False, "error": "request is too large"})
                return
            try:
                request = json.loads(raw)
                if not isinstance(request, dict):
                    raise ChatError("request must be a JSON object")
                operation = request.get("op")
                if operation == "hello":
                    name = clean_label(request.get("name"), "participant name", 80)
                    self.send_json({"ok": True, "protocol": PROTOCOL, "name": name})
                    continue
                if name is None:
                    raise ChatError("send hello with a unique name first")
                self.send_json(self.dispatch(name, operation, request))
                if operation == "quit":
                    return
            except (ChatError, ValueError, TypeError, json.JSONDecodeError) as error:
                self.send_json({"ok": False, "error": str(error)})
            except Exception as error:  # Keep protocol errors visible without killing the server.
                self.send_json({"ok": False, "error": f"internal error: {error}"})

    def dispatch(self, name: str, operation: object, request: dict) -> dict:
        store = self.server.hub.store
        if operation == "help":
            return {"ok": True, "welcome": WELCOME}
        if operation == "create":
            result = store.create_room(request.get("chat"), name)
            self.server.hub.notify(result["name"])
            return {"ok": True, "chat": result}
        if operation == "join":
            return {"ok": True, "membership": store.join(request.get("chat"), name)}
        if operation == "leave":
            store.leave(request.get("chat"), name)
            return {"ok": True}
        if operation == "send":
            message = self.server.hub.post(request.get("chat"), name, request.get("text"))
            return {"ok": True, "message": message}
        if operation == "receive":
            messages = self.server.hub.receive(
                request.get("chat"), name, request.get("wait", 55), request.get("limit", 100)
            )
            return {"ok": True, "messages": messages, "timeout": not messages}
        if operation == "history":
            messages = store.history(
                request.get("chat"), request.get("after", 0), request.get("limit", 500)
            )
            if request.get("mark_read", True) and messages:
                store.mark_read(request.get("chat"), name, messages[-1]["id"])
            return {"ok": True, "messages": messages}
        if operation == "list":
            return {"ok": True, "chats": store.list_rooms()}
        if operation == "who":
            return {"ok": True, "members": store.members(request.get("chat"))}
        if operation == "quit":
            return {"ok": True}
        raise ChatError(f"unknown operation: {operation!r}")


INDEX_HTML = r"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Agent Chat</title>
  <style>
    :root { color-scheme: light dark; --bg:#111318; --panel:#1a1e26; --line:#303746; --muted:#98a2b3; --accent:#7c9cff; }
    * { box-sizing:border-box }
    body { margin:0; font:15px/1.45 system-ui,sans-serif; background:var(--bg); color:#edf0f7 }
    main { height:100vh; display:grid; grid-template-columns:260px 1fr }
    aside { border-right:1px solid var(--line); padding:18px; overflow:auto }
    h1 { font-size:19px; margin:0 0 16px }
    button,input,textarea { font:inherit }
    button { cursor:pointer; border:1px solid var(--line); border-radius:8px; background:#252b37; color:inherit; padding:8px 11px }
    button:hover { border-color:var(--accent) }
    .new { display:flex; gap:7px; margin-bottom:16px }
    .new input { min-width:0 }
    input,textarea { width:100%; border:1px solid var(--line); border-radius:8px; background:#10131a; color:inherit; padding:9px }
    #rooms { display:grid; gap:6px }
    .room { text-align:left; width:100% }
    .room.active { border-color:var(--accent); background:#29324a }
    section { min-width:0; display:grid; grid-template-rows:auto 1fr auto }
    header { padding:16px 20px; border-bottom:1px solid var(--line); display:flex; justify-content:space-between; gap:16px }
    #messages { padding:20px; overflow:auto; display:flex; flex-direction:column; gap:12px }
    .message { max-width:850px; background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:10px 13px }
    .message.system { color:var(--muted); background:transparent; border-style:dashed }
    .meta { color:var(--muted); font-size:12px; margin-bottom:5px }
    .body { overflow-wrap:anywhere }
    .body > :first-child { margin-top:0 }
    .body > :last-child { margin-bottom:0 }
    .body h1,.body h2,.body h3,.body h4,.body h5,.body h6 { line-height:1.25; margin:1em 0 .45em }
    .body h1 { font-size:1.55em } .body h2 { font-size:1.35em } .body h3 { font-size:1.18em }
    .body p { margin:.65em 0 }
    .body ul,.body ol { margin:.65em 0; padding-left:1.6em }
    .body li + li { margin-top:.3em }
    .body code { padding:.12em .35em; border-radius:5px; background:#0c0f15; font:13px/1.5 ui-monospace,SFMono-Regular,Consolas,monospace }
    .body pre { overflow:auto; margin:.8em 0; padding:12px; border:1px solid var(--line); border-radius:8px; background:#0c0f15 }
    .body pre code { padding:0; background:transparent; white-space:pre }
    .body table { display:block; max-width:100%; overflow:auto; margin:.8em 0; border-collapse:collapse }
    .body th,.body td { padding:7px 10px; border:1px solid var(--line); text-align:left; vertical-align:top }
    .body th { background:#252b37 }
    .body a { color:#9db3ff }
    form { border-top:1px solid var(--line); padding:14px 20px; display:grid; grid-template-columns:150px 1fr auto; gap:9px; align-items:end }
    textarea { resize:vertical; min-height:44px; max-height:180px }
    .empty { color:var(--muted); margin:auto }
    @media(max-width:720px) { main{grid-template-columns:1fr} aside{display:none} form{grid-template-columns:1fr} }
  </style>
</head>
<body>
<main>
  <aside>
    <h1>Agent Chat</h1>
    <div class="new"><input id="new-room" placeholder="Новый чат"><button id="create">+</button></div>
    <div id="rooms"></div>
  </aside>
  <section>
    <header><strong id="title">Выберите чат</strong><span id="members"></span></header>
    <div id="messages"><div class="empty">История сохраняется в SQLite</div></div>
    <form id="composer">
      <input id="sender" placeholder="Ваше имя" value="human">
      <textarea id="text" placeholder="Сообщение" required></textarea>
      <button type="submit">Отправить</button>
    </form>
  </section>
</main>
<script>
const state = { room:null, after:0 };
const roomsEl=document.querySelector('#rooms'), messagesEl=document.querySelector('#messages');
const senderEl=document.querySelector('#sender'), textEl=document.querySelector('#text');
senderEl.value=localStorage.getItem('agent-chat-name')||'human';
const api=async(url,options={})=>{const r=await fetch(url,options);const j=await r.json();if(!r.ok)throw Error(j.error||r.statusText);return j};
function roomUrl(room,suffix=''){return '/api/chats/'+encodeURIComponent(room)+suffix}
async function loadRooms(){
  const data=await api('/api/chats'); roomsEl.replaceChildren();
  for(const room of data.chats){const b=document.createElement('button');b.className='room'+(state.room===room.name?' active':'');b.textContent=room.name;b.onclick=()=>selectRoom(room.name);roomsEl.append(b)}
}
async function selectRoom(room){state.room=room;state.after=0;messagesEl.replaceChildren();document.querySelector('#title').textContent=room;await Promise.all([loadRooms(),loadMessages(),loadMembers()])}
function appendMessage(m){
  const box=document.createElement('article');box.className='message '+m.kind;
  const meta=document.createElement('div');meta.className='meta';meta.textContent=`#${m.id} · ${m.sender} · ${new Date(m.created_at).toLocaleString()}`;
  const body=document.createElement('div');body.className='body';body.innerHTML=m.body_html;
  box.append(meta,body);messagesEl.append(box);state.after=Math.max(state.after,m.id);messagesEl.scrollTop=messagesEl.scrollHeight;
}
async function loadMessages(){if(!state.room)return;const data=await api(roomUrl(state.room,`/messages?after=${state.after}`));for(const m of data.messages)appendMessage(m)}
async function loadMembers(){if(!state.room)return;const d=await api(roomUrl(state.room,'/members'));document.querySelector('#members').textContent=d.members.map(x=>x.name).join(', ')}
document.querySelector('#create').onclick=async()=>{const input=document.querySelector('#new-room');if(!input.value.trim())return;await api('/api/chats',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:input.value,sender:senderEl.value})});const name=input.value.trim();input.value='';await selectRoom(name)};
document.querySelector('#composer').onsubmit=async e=>{e.preventDefault();if(!state.room)return;localStorage.setItem('agent-chat-name',senderEl.value);await api(roomUrl(state.room,'/messages'),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({sender:senderEl.value,text:textEl.value})});textEl.value='';await loadMessages()};
setInterval(()=>{loadRooms().catch(console.error);loadMessages().catch(console.error);loadMembers().catch(console.error)},1000);loadRooms().catch(console.error);
</script>
</body>
</html>
"""


class ChatHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], hub: Hub):
        super().__init__(address, HTTPHandler)
        self.hub = hub


class HTTPHandler(BaseHTTPRequestHandler):
    server: ChatHTTPServer

    def log_message(self, format: str, *args: object) -> None:
        # The browser polls frequently; the default access log would drown useful
        # service diagnostics. HTTP errors are returned to the caller as JSON.
        return

    def json_response(self, status: HTTPStatus, payload: dict) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ChatError("invalid Content-Length") from error
        if length <= 0 or length > MAX_HTTP_BODY:
            raise ChatError("invalid request body size")
        payload = json.loads(self.rfile.read(length))
        if not isinstance(payload, dict):
            raise ChatError("request body must be a JSON object")
        return payload

    def do_GET(self) -> None:
        try:
            parsed = urlparse(self.path)
            if parsed.path == "/":
                encoded = INDEX_HTML.encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(encoded)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(encoded)
                return
            if parsed.path == "/api/chats":
                self.json_response(HTTPStatus.OK, {"chats": self.server.hub.store.list_rooms()})
                return
            room, resource = self.parse_room_path(parsed.path)
            if resource == "messages":
                query = parse_qs(parsed.query)
                messages = self.server.hub.store.history(
                    room, int(query.get("after", [0])[0]), int(query.get("limit", [500])[0])
                )
                for message in messages:
                    message["body_html"] = render_markdown(message["body"])
                self.json_response(HTTPStatus.OK, {"messages": messages})
                return
            if resource == "members":
                self.json_response(
                    HTTPStatus.OK, {"members": self.server.hub.store.members(room)}
                )
                return
            raise ChatError("unknown endpoint")
        except (ChatError, ValueError, json.JSONDecodeError) as error:
            self.json_response(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        except Exception as error:
            self.json_response(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(error)})

    def do_POST(self) -> None:
        try:
            parsed = urlparse(self.path)
            payload = self.read_json()
            if parsed.path == "/api/chats":
                creator = payload.get("sender", "human")
                chat = self.server.hub.store.create_room(payload.get("name"), creator)
                self.server.hub.notify(chat["name"])
                self.json_response(HTTPStatus.CREATED, {"chat": chat})
                return
            room, resource = self.parse_room_path(parsed.path)
            if resource == "messages":
                message = self.server.hub.post(room, payload.get("sender", "human"), payload.get("text"))
                self.json_response(HTTPStatus.CREATED, {"message": message})
                return
            raise ChatError("unknown endpoint")
        except (ChatError, ValueError, json.JSONDecodeError) as error:
            self.json_response(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        except Exception as error:
            self.json_response(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(error)})

    @staticmethod
    def parse_room_path(path: str) -> tuple[str, str]:
        parts = path.strip("/").split("/")
        if len(parts) != 4 or parts[:2] != ["api", "chats"]:
            raise ChatError("unknown endpoint")
        return unquote(parts[2]), parts[3]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default: local only)")
    parser.add_argument("--tcp-port", type=int, default=8765, help="agent JSONL port")
    parser.add_argument("--http-port", type=int, default=8766, help="browser UI port")
    parser.add_argument(
        "--db", type=Path, default=Path(__file__).with_name("data") / "chat.db", help="SQLite path"
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    store = Store(args.db.resolve())
    hub = Hub(store)
    tcp = AgentTCPServer((args.host, args.tcp_port), hub)
    http = ChatHTTPServer((args.host, args.http_port), hub)
    stopping = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stopping.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    threads = [
        threading.Thread(target=tcp.serve_forever, name="agent-tcp", daemon=True),
        threading.Thread(target=http.serve_forever, name="human-http", daemon=True),
    ]
    for thread in threads:
        thread.start()
    print(f"Agent TCP: {args.host}:{args.tcp_port}")
    print(f"Human UI:  http://{args.host}:{args.http_port}")
    print(f"Database:  {store.path}")
    try:
        while not stopping.wait(0.5):
            if not all(thread.is_alive() for thread in threads):
                raise RuntimeError("a server thread stopped unexpectedly")
    finally:
        tcp.shutdown()
        http.shutdown()
        tcp.server_close()
        http.server_close()


if __name__ == "__main__":
    main()
