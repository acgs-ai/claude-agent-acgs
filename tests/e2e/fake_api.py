# SPDX-FileCopyrightText: 2026-present ACGS Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""A scripted fake of the Anthropic Messages API on 127.0.0.1, so the real Claude Code CLI runs without a network.

Each request that offers tools (the agent loop, including sub-agents) gets the next scripted turn: a tool call or
text. Other requests (titles, summaries) get a short text answer.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class Script:
    """The turns to play, in order, and the requests received."""

    def __init__(self, turns: list[dict[str, Any]]) -> None:
        self.turns = list(turns)
        self.requests: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def next_turn(self, body: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self.requests.append(body)
            if self.turns and body.get("tools"):
                return self.turns.pop(0)
            return {"text": "ok"}


def _content(turn: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    if "tool" in turn:
        block = {"type": "tool_use", "id": turn.get("id", "toolu_01"), "name": turn["tool"], "input": turn["input"]}
        return [block], "tool_use"
    return [{"type": "text", "text": turn["text"]}], "end_turn"


def _handler(script: Script) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            pass

        def do_GET(self) -> None:
            self.send_response(404)
            self.end_headers()

        def do_POST(self) -> None:
            length = int(self.headers.get("content-length", 0))
            body: dict[str, Any] = json.loads(self.rfile.read(length) or b"{}")
            if "count_tokens" in self.path:
                self._json({"input_tokens": 10})
                return
            if not self.path.startswith("/v1/messages"):
                self.send_response(404)
                self.end_headers()
                return
            content, stop = _content(script.next_turn(body))
            message: dict[str, Any] = {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": body.get("model", "claude"),
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 1},
            }
            if not body.get("stream"):
                self._json({**message, "content": content, "stop_reason": stop})
                return
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            self._event("message_start", {"message": message})
            for index, block in enumerate(content):
                if block["type"] == "tool_use":
                    start = {**block, "input": {}}
                    delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
                else:
                    start = {"type": "text", "text": ""}
                    delta = {"type": "text_delta", "text": block["text"]}
                self._event("content_block_start", {"index": index, "content_block": start})
                self._event("content_block_delta", {"index": index, "delta": delta})
                self._event("content_block_stop", {"index": index})
            self._event(
                "message_delta", {"delta": {"stop_reason": stop, "stop_sequence": None}, "usage": {"output_tokens": 5}}
            )
            self._event("message_stop", {})

        def _event(self, kind: str, data: dict[str, Any]) -> None:
            self.wfile.write(f"event: {kind}\ndata: {json.dumps({'type': kind, **data})}\n\n".encode())
            self.wfile.flush()

        def _json(self, data: dict[str, Any]) -> None:
            payload = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return Handler


@contextmanager
def fake_api(turns: list[dict[str, Any]]) -> Iterator[tuple[str, Script]]:
    """Serve ``turns`` on 127.0.0.1 and yield the base URL and the script."""
    script = Script(turns)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(script))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", script
    finally:
        server.shutdown()
        server.server_close()
