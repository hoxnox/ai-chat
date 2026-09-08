from __future__ import annotations

import json
import socket
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server import (  # noqa: E402
    AgentTCPServer,
    ChatHTTPServer,
    Hub,
    Store,
    render_markdown,
)


class RunningService:
    def __init__(self, root: Path):
        self.store = Store(root / "chat.sqlite3")
        self.hub = Hub(self.store)
        self.tcp = AgentTCPServer(("127.0.0.1", 0), self.hub)
        self.http = ChatHTTPServer(("127.0.0.1", 0), self.hub)
        self.threads = [
            threading.Thread(target=self.tcp.serve_forever, daemon=True),
            threading.Thread(target=self.http.serve_forever, daemon=True),
        ]
        for thread in self.threads:
            thread.start()

    @property
    def tcp_port(self) -> int:
        return int(self.tcp.server_address[1])

    @property
    def http_port(self) -> int:
        return int(self.http.server_address[1])

    def close(self) -> None:
        self.tcp.shutdown()
        self.http.shutdown()
        self.tcp.server_close()
        self.http.server_close()
        for thread in self.threads:
            thread.join(timeout=2)

    def request(self, name: str, payload: dict) -> tuple[dict, dict]:
        with socket.create_connection(("127.0.0.1", self.tcp_port), timeout=2) as connection:
            connection.settimeout(float(payload.get("wait", 0)) + 3)
            stream = connection.makefile("rwb")

            def read() -> dict:
                return json.loads(stream.readline())

            welcome = read()
            stream.write((json.dumps({"op": "hello", "name": name}) + "\n").encode())
            stream.flush()
            self.assert_ok(read())
            stream.write((json.dumps(payload) + "\n").encode())
            stream.flush()
            response = read()
            self.assert_ok(response)
            return welcome, response

    @staticmethod
    def assert_ok(response: dict) -> None:
        if not response.get("ok"):
            raise AssertionError(response)


class MarkdownRenderingTest(unittest.TestCase):
    def test_renders_common_markdown_blocks_and_inline_markup(self) -> None:
        rendered = render_markdown(
            "# План\n\n- первый шаг\n- второй **важный** шаг\n\n`go test ./...`"
        )

        self.assertIn("<h1>План</h1>", rendered)
        self.assertIn("<ul><li>первый шаг</li><li>второй <strong>важный</strong> шаг</li></ul>", rendered)
        self.assertIn("<p><code>go test ./...</code></p>", rendered)

    def test_renders_fenced_code_with_escaped_contents(self) -> None:
        rendered = render_markdown("```go\nif x < 3 {\n    fmt.Println(x)\n}\n```")

        self.assertEqual(
            rendered,
            '<pre><code class="language-go">if x &lt; 3 {\n    fmt.Println(x)\n}</code></pre>',
        )

    def test_renders_markdown_tables(self) -> None:
        rendered = render_markdown(
            "| Файл | Изменение |\n| --- | --- |\n| `handler.go` | **30s** |"
        )

        self.assertEqual(
            rendered,
            "<table><thead><tr><th>Файл</th><th>Изменение</th></tr></thead>"
            "<tbody><tr><td><code>handler.go</code></td><td><strong>30s</strong></td></tr></tbody></table>",
        )

    def test_links_are_clickable_without_allowing_raw_html_or_javascript(self) -> None:
        rendered = render_markdown(
            "[issue](https://src.devment.tech/srv/scanner/issues/128) "
            "[bad](javascript:alert(1)) <script>alert(2)</script>"
        )

        self.assertIn(
            '<a href="https://src.devment.tech/srv/scanner/issues/128" target="_blank" rel="noopener noreferrer">issue</a>',
            rendered,
        )
        self.assertNotIn('href="javascript:', rendered)
        self.assertNotIn("<script>", rendered)
        self.assertIn("&lt;script&gt;alert(2)&lt;/script&gt;", rendered)

    def test_renders_ordered_lists(self) -> None:
        rendered = render_markdown("1. проверить код\n2. добавить тест")

        self.assertEqual(rendered, "<ol><li>проверить код</li><li>добавить тест</li></ol>")

    def test_does_not_activate_markdown_links_inside_code_spans(self) -> None:
        rendered = render_markdown("`[issue](https://example.com/1)`")

        self.assertEqual(rendered, "<p><code>[issue](https://example.com/1)</code></p>")


class ChatIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.service = RunningService(Path(self.temp.name))

    def tearDown(self) -> None:
        self.service.close()
        self.temp.cleanup()

    def test_welcome_explains_protocol_and_history_is_durable(self) -> None:
        welcome, created = self.service.request("codex-one", {"op": "create", "chat": "review"})
        self.assertEqual(welcome["protocol"], "agent-chat/1")
        self.assertIn("receive", welcome["commands"])
        self.assertEqual(created["chat"]["name"], "review")

        _, sent = self.service.request(
            "codex-one", {"op": "send", "chat": "review", "text": "First proposal"}
        )
        self.assertEqual(sent["message"]["id"], 2)

        # A fresh Store instance sees the same SQLite history.
        reopened = Store(Path(self.temp.name) / "chat.sqlite3")
        history = reopened.history("review")
        self.assertEqual([message["body"] for message in history], [
            "codex-one created chat review",
            "First proposal",
        ])

    def test_waiting_agent_is_woken_by_human_message(self) -> None:
        self.service.request("codex-one", {"op": "create", "chat": "review"})
        # Consume the creation event first.
        self.service.request("claude-one", {"op": "receive", "chat": "review", "wait": 0})
        result: dict = {}

        def wait_for_message() -> None:
            _, response = self.service.request(
                "claude-one", {"op": "receive", "chat": "review", "wait": 2}
            )
            result.update(response)

        waiter = threading.Thread(target=wait_for_message)
        waiter.start()
        time.sleep(0.05)

        data = json.dumps({"sender": "human", "text": "Please compare the options"}).encode()
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.service.http_port}/api/chats/review/messages",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            self.assertEqual(response.status, 201)

        waiter.join(timeout=3)
        self.assertFalse(waiter.is_alive())
        self.assertEqual(result["messages"][0]["sender"], "human")
        self.assertEqual(result["messages"][0]["body"], "Please compare the options")

    def test_web_api_lists_full_transcript(self) -> None:
        self.service.request("codex-one", {"op": "create", "chat": "review"})
        self.service.request("claude-one", {"op": "send", "chat": "review", "text": "Reply"})
        url = f"http://127.0.0.1:{self.service.http_port}/api/chats/review/messages?after=0"
        with urllib.request.urlopen(url, timeout=2) as response:
            payload = json.load(response)
        self.assertEqual(payload["messages"][-1]["sender"], "claude-one")
        self.assertEqual(payload["messages"][-1]["body"], "Reply")
        self.assertEqual(payload["messages"][-1]["body_html"], "<p>Reply</p>")

    def test_web_ui_uses_server_rendered_markdown(self) -> None:
        with urllib.request.urlopen(f"http://127.0.0.1:{self.service.http_port}/", timeout=2) as response:
            page = response.read().decode()

        self.assertIn("body.innerHTML=m.body_html", page)
        self.assertIn(".body pre", page)
        self.assertIn(".body table", page)

    def test_agent_history_advances_its_unread_cursor(self) -> None:
        self.service.request("codex-one", {"op": "create", "chat": "review"})
        self.service.request("codex-one", {"op": "send", "chat": "review", "text": "Proposal"})
        _, history = self.service.request("claude-one", {"op": "history", "chat": "review"})
        self.assertEqual(history["messages"][-1]["body"], "Proposal")
        _, unread = self.service.request(
            "claude-one", {"op": "receive", "chat": "review", "wait": 0}
        )
        self.assertEqual(unread["messages"], [])


if __name__ == "__main__":
    unittest.main()
