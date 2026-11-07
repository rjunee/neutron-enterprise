# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures for ne-serve tests.

The CI serving path is exercised for real over localhost HTTP against an
in-process **fake OpenAI-compatible server** (stdlib ``http.server``, no GPU) —
this is the orchestrator-sanctioned stand-in while real-GPU throughput is
live-parked. The fake:

* ``GET /health`` -> 200
* ``GET /v1/models`` -> ``{"data": [{"id": <served_name>}]}``
* ``POST /v1/chat/completions`` -> if the body carries a ``json_schema``
  response format, replies with content that is ``json.dumps`` of an instance
  built from the schema's ``properties`` / ``required``; otherwise a plain
  string. It **rejects (400) any body containing the removed ``guided_json``
  key** — locking in the v0.12.0 API-drift fix. Every request body is recorded
  for assertions.

Async seams are exercised via ``asyncio.run`` — pytest-asyncio is NOT a
dependency and must not be added (repo rule; see ne-govern/tests/conftest.py).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

_SERVED_NAME = "qwen3-0.6b"


def _instance_for_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Build a minimal conforming instance from a JSON-schema's properties."""
    props: dict[str, Any] = schema.get("properties", {})
    required = schema.get("required", list(props))
    out: dict[str, Any] = {}
    for name in required:
        spec = props.get(name, {})
        json_type = spec.get("type", "string")
        if json_type == "string":
            out[name] = f"stub-{name}"
        elif json_type == "integer":
            out[name] = 1
        elif json_type == "number":
            out[name] = 1.0
        elif json_type == "boolean":
            out[name] = True
        elif json_type == "array":
            out[name] = []
        elif json_type == "object":
            out[name] = {}
        else:
            out[name] = None
    return out


class _Recorder:
    """Collects request bodies seen by the fake server (thread-safe enough)."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []


def _make_handler(recorder: _Recorder) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def _json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == "/health":
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if self.path == "/v1/models":
                self._json(200, {"data": [{"id": _SERVED_NAME}]})
                return
            self._json(404, {"error": "not found"})

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b"{}"
            body = json.loads(raw)
            recorder.requests.append(body)

            # Lock in the drift fix: the legacy guided_json field is rejected.
            if "guided_json" in body:
                self._json(400, {"error": "guided_json was removed in vLLM v0.12.0"})
                return

            if self.path != "/v1/chat/completions":
                self._json(404, {"error": "not found"})
                return

            response_format = body.get("response_format")
            if isinstance(response_format, dict) and response_format.get("type") == "json_schema":
                schema = response_format["json_schema"]["schema"]
                content = json.dumps(_instance_for_schema(schema))
            else:
                content = "stub completion"

            self._json(
                200,
                {
                    "choices": [{"message": {"role": "assistant", "content": content}}],
                    "model": body.get("model", _SERVED_NAME),
                },
            )

    return Handler


class FakeServer:
    """A running fake OpenAI-compatible server with a request recorder."""

    def __init__(self, base_url: str, recorder: _Recorder, served_name: str) -> None:
        self.base_url = base_url
        self.recorder = recorder
        self.served_name = served_name


@pytest.fixture
def fake_openai_server() -> Iterator[FakeServer]:
    recorder = _Recorder()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(recorder))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[0], server.server_address[1]
    try:
        yield FakeServer(f"http://{host}:{port}", recorder, _SERVED_NAME)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
