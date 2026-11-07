# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""OpenBao HTTP client never honors env proxies and refuses cross-host redirects.

Both properties protect the ``X-Vault-Token`` (and secret payloads) from being
sent in cleartext to an unsanctioned proxy or replayed to a foreign host (C2/C12).

Fail-on-revert mechanism: each test resets ``urllib.request._opener = None`` and
sets ``HTTP(S)_PROXY``/``ALL_PROXY`` to a recording proxy. If the fix were
reverted to bare ``urllib.request.urlopen``, urlopen would (re)build the global
opener from the patched env and route through the recorder — the recorder would
then see a request and the assertion below would fail.
"""

from __future__ import annotations

import asyncio
import json
import threading
import urllib.request
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from ne.secrets.errors import SecretsBackendError
from ne.secrets.openbao import OpenBaoSecretStore

_KV2_BODY = json.dumps(
    {
        "data": {
            "data": {"k": "v"},
            "metadata": {"version": 1, "created_time": "2026-07-03T10:20:30Z"},
        }
    }
).encode("utf-8")


def _make_handler(records: list[str], responder: Callable[[BaseHTTPRequestHandler], None]):
    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # silence stderr noise
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
    host, port = server.server_address[0], server.server_address[1]
    return f"http://{host}:{port}"


@pytest.fixture
def _reset_global_opener(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # A reverted urlopen would rebuild THIS from the patched proxy env.
    monkeypatch.setattr(urllib.request, "_opener", None, raising=False)
    yield


def _set_proxy_env(monkeypatch: pytest.MonkeyPatch, proxy_url: str) -> None:
    for var in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(var, proxy_url)


def test_read_ignores_env_proxies(
    monkeypatch: pytest.MonkeyPatch, _reset_global_opener: None
) -> None:
    def _bao_ok(h: BaseHTTPRequestHandler) -> None:
        h.send_response(200)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(_KV2_BODY)))
        h.end_headers()
        h.wfile.write(_KV2_BODY)

    def _proxy_502(h: BaseHTTPRequestHandler) -> None:
        h.send_response(502)
        h.end_headers()

    bao, _bao_rec = _serve(_bao_ok)
    proxy, proxy_rec = _serve(_proxy_502)
    try:
        _set_proxy_env(monkeypatch, _base_url(proxy))
        store = OpenBaoSecretStore(url=_base_url(bao), token="tok")
        version = asyncio.run(store.read("app/db"))
        assert version.data == {"k": "v"}
        assert proxy_rec == [], "the token/request must never reach the env proxy"
    finally:
        bao.shutdown()
        proxy.shutdown()


def test_cross_host_redirect_refused(
    monkeypatch: pytest.MonkeyPatch, _reset_global_opener: None
) -> None:
    # The second (attacker) server must NEVER receive the X-Vault-Token request.
    def _sink(h: BaseHTTPRequestHandler) -> None:
        h.send_response(200)
        h.send_header("Content-Length", str(len(_KV2_BODY)))
        h.end_headers()
        h.wfile.write(_KV2_BODY)

    other, other_rec = _serve(_sink)

    def _redirect_to_other(h: BaseHTTPRequestHandler) -> None:
        h.send_response(307)
        h.send_header("Location", f"{_base_url(other)}/v1/secret/data/app/db")
        h.end_headers()

    bao, _bao_rec = _serve(_redirect_to_other)
    try:
        # No proxies here — isolate the redirect behavior.
        store = OpenBaoSecretStore(url=_base_url(bao), token="tok")
        with pytest.raises(SecretsBackendError):
            asyncio.run(store.read("app/db"))
        assert other_rec == [], "cross-host redirect must not forward the token"
    finally:
        bao.shutdown()
        other.shutdown()
