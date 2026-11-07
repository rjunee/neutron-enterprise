# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""JWKS egress hardening (NE-FIX-IDENTITY P2-3a, C2).

ALL loopback, zero external network. Proves the JWKS fetch:

* ignores ``HTTP_PROXY`` / ``HTTPS_PROXY`` / ``ALL_PROXY`` (the fetch — and any
  token carried on a redirect — can never be routed off-box);
* refuses a cross-origin redirect while following a same-origin one.

A structural backstop asserts the opener carries no ``ProxyHandler`` and does
carry the same-origin redirect handler, so the guarantee holds even if the
behavioral proxy test is skipped on a hostile CI.
"""

from __future__ import annotations

import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from ne.identity import jwks as jwks_module
from ne.identity.config import OidcConfig
from ne.identity.errors import JwksError
from ne.identity.jwks import HttpJwksSource

from .conftest import DEFAULT_KID, TEST_AUDIENCE, run

_LOCAL_ISSUER = "http://localhost:8081/realms/ne-dev"


def _start(handler_cls: type[BaseHTTPRequestHandler]) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _make_json_handler(payload: bytes) -> type[BaseHTTPRequestHandler]:
    class _JsonHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: Any) -> None:
            return

    return _JsonHandler


def _make_recording_proxy(counter: dict[str, int]) -> type[BaseHTTPRequestHandler]:
    class _ProxyHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            counter["n"] += 1
            self.send_response(502)
            self.end_headers()

        do_POST = do_GET
        do_CONNECT = do_GET

        def log_message(self, format: str, *args: Any) -> None:
            return

    return _ProxyHandler


def _config(port: int, path: str = "/certs") -> OidcConfig:
    return OidcConfig(
        issuer=_LOCAL_ISSUER,
        audience=TEST_AUDIENCE,
        allow_insecure_http=True,
        jwks_uri=f"http://127.0.0.1:{port}{path}",
    )


def test_env_proxy_is_bypassed(jwks: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.dumps(jwks).encode()
    jwks_server = _start(_make_json_handler(payload))
    counter = {"n": 0}
    proxy_server = _start(_make_recording_proxy(counter))
    try:
        jwks_port = jwks_server.server_address[1]
        proxy_url = f"http://127.0.0.1:{proxy_server.server_address[1]}"

        for var in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        ):
            monkeypatch.setenv(var, proxy_url)
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)
        # Defeat macOS localhost auto-bypass + force default-opener rebuild, so a
        # REVERTED (urlopen) implementation would deterministically route via the
        # recording proxy.
        monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: False)
        monkeypatch.setattr(urllib.request, "_opener", None)

        source = HttpJwksSource(_config(jwks_port))
        key = run(source.get_signing_key(DEFAULT_KID))
        assert key.key_id == DEFAULT_KID
        assert counter["n"] == 0  # the proxy was never consulted
    finally:
        jwks_server.shutdown()
        jwks_server.server_close()
        proxy_server.shutdown()
        proxy_server.server_close()


def test_opener_never_proxies_structurally(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(var, "http://127.0.0.1:9")
    opener = jwks_module._build_no_proxy_opener()
    handlers: list[Any] = list(opener.handlers)  # type: ignore[attr-defined]
    assert not any(isinstance(h, urllib.request.ProxyHandler) for h in handlers)
    assert any(isinstance(h, jwks_module._SameOriginRedirectHandler) for h in handlers)


def test_cross_host_redirect_refused(jwks: dict[str, Any]) -> None:
    payload = json.dumps(jwks).encode()
    dest_server = _start(_make_json_handler(payload))
    try:
        dest_port = dest_server.server_address[1]
        location = f"http://127.0.0.1:{dest_port}/certs"

        class _RedirectHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(302)
                self.send_header("Location", location)
                self.end_headers()

            def log_message(self, format: str, *args: Any) -> None:
                return

        redirect_server = _start(_RedirectHandler)
        try:
            source = HttpJwksSource(_config(redirect_server.server_address[1]))
            with pytest.raises(JwksError):
                run(source.get_signing_key(DEFAULT_KID))
        finally:
            redirect_server.shutdown()
            redirect_server.server_close()
    finally:
        dest_server.shutdown()
        dest_server.server_close()


def test_same_origin_redirect_followed(jwks: dict[str, Any]) -> None:
    payload = json.dumps(jwks).encode()

    class _SameOriginHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/certs":
                self.send_response(302)
                self.send_header("Location", "/certs2")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = _start(_SameOriginHandler)
    try:
        source = HttpJwksSource(_config(server.server_address[1]))
        key = run(source.get_signing_key(DEFAULT_KID))
        assert key.key_id == DEFAULT_KID
    finally:
        server.shutdown()
        server.server_close()
