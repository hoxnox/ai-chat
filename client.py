#!/usr/bin/env python3
"""Command-line client for the dependency-free Agent Chat relay."""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from typing import Any


def read_packet(stream: Any) -> dict:
    line = stream.readline()
    if not line:
        raise RuntimeError("server closed the connection")
    packet = json.loads(line)
    if not isinstance(packet, dict):
        raise RuntimeError("server returned a non-object packet")
    return packet


def request(args: argparse.Namespace, payload: dict) -> tuple[dict, dict]:
    wait = float(payload.get("wait", 0))
    with socket.create_connection((args.host, args.port), timeout=10) as connection:
        connection.settimeout(max(15.0, wait + 10.0))
        stream = connection.makefile("rwb")
        welcome = read_packet(stream)
        if payload.get("op") == "_guide":
            return welcome, welcome
        hello = json.dumps({"op": "hello", "name": args.name}, ensure_ascii=False) + "\n"
        stream.write(hello.encode("utf-8"))
        stream.flush()
        response = read_packet(stream)
        if not response.get("ok"):
            raise RuntimeError(response.get("error", "hello failed"))
        encoded = json.dumps(payload, ensure_ascii=False) + "\n"
        stream.write(encoded.encode("utf-8"))
        stream.flush()
        response = read_packet(stream)
        if not response.get("ok"):
            raise RuntimeError(response.get("error", "request failed"))
        return welcome, response


def print_messages(messages: list[dict]) -> None:
    if not messages:
        print("NO_MESSAGES")
        return
    for index, message in enumerate(messages):
        if index:
            print()
        print(f"[{message['id']}] {message['sender']} @ {message['created_at']}")
        print(message["body"])


def print_result(args: argparse.Namespace, response: dict) -> None:
    if args.json:
        print(json.dumps(response, ensure_ascii=False, indent=2))
        return
    command = args.command
    if command == "guide":
        print(f"Protocol: {response['protocol']}")
        print(response["summary"])
        print("\nAgent loop:")
        for item in response["agent_loop"]:
            print(f"- {item}")
        print("\nRaw JSONL commands:")
        for name, example in response["commands"].items():
            print(f"- {name}: {json.dumps(example, ensure_ascii=False)}")
    elif command in {"history", "wait"}:
        print_messages(response["messages"])
    elif command == "list":
        chats = response["chats"]
        if not chats:
            print("NO_CHATS")
        for chat in chats:
            print(
                f"{chat['name']}\tparticipants={chat['participant_count']}"
                f"\tlast_message={chat['last_message_id']}"
            )
    elif command == "who":
        members = response["members"]
        if not members:
            print("NO_MEMBERS")
        for member in members:
            print(f"{member['name']}\tlast_seen={member['last_seen_at']}")
    elif command == "send":
        message = response["message"]
        print(f"SENT {message['id']} to {message['chat']}")
    elif command == "create":
        print(f"CREATED {response['chat']['name']}")
    elif command == "join":
        print(f"JOINED {response['membership']['chat']} as {response['membership']['name']}")
    elif command == "leave":
        print(f"LEFT {args.chat}")
    else:
        print(json.dumps(response, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.getenv("AGENT_CHAT_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("AGENT_CHAT_PORT", "8765")))
    parser.add_argument(
        "--name",
        default=os.getenv("AGENT_CHAT_NAME"),
        help="unique identity for this T3 thread",
    )
    parser.add_argument("--json", action="store_true", help="print the complete JSON response")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("guide", help="show the server-provided agent instructions")

    create = subparsers.add_parser("create", help="create and join a chat")
    create.add_argument("chat")

    join = subparsers.add_parser("join", help="join an existing chat")
    join.add_argument("chat")

    send = subparsers.add_parser("send", help="send text; use - to read it from stdin")
    send.add_argument("chat")
    send.add_argument("text")

    wait = subparsers.add_parser("wait", help="return unread messages, waiting if necessary")
    wait.add_argument("chat")
    wait.add_argument("--timeout", type=float, default=55.0)
    wait.add_argument("--limit", type=int, default=100)

    history = subparsers.add_parser("history", help="read durable chat history")
    history.add_argument("chat")
    history.add_argument("--after", type=int, default=0)
    history.add_argument("--limit", type=int, default=500)

    subparsers.add_parser("list", help="list chats")

    who = subparsers.add_parser("who", help="list known participants")
    who.add_argument("chat")

    leave = subparsers.add_parser("leave", help="leave a chat")
    leave.add_argument("chat")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    command = args.command
    if command != "guide" and not args.name:
        parser.error("--name or AGENT_CHAT_NAME is required (use one stable unique name per T3 thread)")
    if command == "guide":
        payload = {"op": "_guide"}
    elif command in {"create", "join", "leave", "who"}:
        payload = {"op": command, "chat": args.chat}
    elif command == "send":
        text = sys.stdin.read() if args.text == "-" else args.text
        payload = {"op": "send", "chat": args.chat, "text": text}
    elif command == "wait":
        payload = {"op": "receive", "chat": args.chat, "wait": args.timeout, "limit": args.limit}
    elif command == "history":
        payload = {"op": "history", "chat": args.chat, "after": args.after, "limit": args.limit}
    elif command == "list":
        payload = {"op": "list"}
    else:
        parser.error(f"unsupported command: {command}")
        return

    try:
        _welcome, response = request(args, payload)
        print_result(args, response)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
