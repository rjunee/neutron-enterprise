# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""GraphitiSidecar.wait_healthy never honors env proxies (C2).

A set ``HTTP(S)_PROXY``/``ALL_PROXY`` must not divert the loopback health poll
(which carries the sidecar service token) to an unsanctioned proxy. Fail-on-revert:
``urllib.request._opener`` is reset to ``None`` and proxy env is set to a recording
502-proxy; a reverted ``urllib.request.urlopen`` would rebuild the global opener
from that env, route through the recorder (all 502s → unhealthy), and the
``is True`` assertion below would fail while the recorder saw a request.
"""

from __future__ import annotations

import threading
import urllib.request
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from ne.memory_graph.sidecar import GraphitiSidecar


def _make_handler(records: list[str], responder: Callable[[BaseHTTPRequestHandler], None]):
    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            records.append(f"{self.command} {self.path}")
            responder(self)

    return _Handler


def _serve(
    responder: Callable[[BaseHTTPRequestHandler], None],
) -> tuple[ThreadingHTTPServer, list[str]]:
    records: list[str] = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(records, responder))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, records


def _base_url(server: ThreadingHTTPServer) -> str:
    return f"http://{server.server_address[0]}:{server.server_address[1]}"


@pytest.fixture
def _reset_global_opener(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(urllib.request, "_opener", None, raising=False)
    yield


def test_wait_healthy_ignores_env_proxies(
    monkeypatch: pytest.MonkeyPatch, _reset_global_opener: None
) -> None:
    def _healthz_ok(h: BaseHTTPRequestHandler) -> None:
        h.send_response(200)
        h.send_header("Content-Length", "2")
        h.end_headers()
        h.wfile.write(b"ok")

    def _proxy_502(h: BaseHTTPRequestHandler) -> None:
        h.send_response(502)
        h.end_headers()

    health, _health_rec = _serve(_healthz_ok)
    proxy, proxy_rec = _serve(_proxy_502)
    try:
        for var in (
            "HTTP_PROXY",
            "http_proxy",
            "HTTPS_PROXY",
            "https_proxy",
            "ALL_PROXY",
            "all_proxy",
        ):
            monkeypatch.setenv(var, _base_url(proxy))

        assert (
            GraphitiSidecar.wait_healthy(_base_url(health), timeout_s=3.0, interval_s=0.1) is True
        )
        assert proxy_rec == [], "the health poll must never reach the env proxy"
    finally:
        health.shutdown()
        proxy.shutdown()
