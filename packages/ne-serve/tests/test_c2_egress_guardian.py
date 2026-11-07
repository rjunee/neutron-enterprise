# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""THE ne-serve C2 egress guardian (plan §2.1) — the unit's headline deliverable.

Three structural + behavioral layers close the ne-serve half of the audit
corpus-exfil finding (the ne-brain half is NE-CKC2):

  (a) AST import allowlist: no module under ``src/ne/serve`` except ``client.py``
      may import a raw HTTP client (httpx / requests / aiohttp / urllib3 / socket
      / urllib.request / http.client). ``huggingface_hub`` is DELIBERATELY not in
      the forbidden set — download.py's hub egress is the sanctioned ONLINE
      pre-stage step, disabled at serve time by ``HF_HUB_OFFLINE=1``
      (``offline_env``).
  (b) AST: every ``self._base_url`` assignment in ``client.py`` has a call to
      ``validate_in_vpc_endpoint`` as its value — the base_url can never be set
      by an unvalidated bare rstrip again.
  (c) behavioral red-team: ServeClient construction raises on hostile endpoints,
      accepts in-VPC / allowlisted ones, and a MockTransport-recording client
      proves ZERO requests leave on rejection (exfil-never-POSTs).
"""

from __future__ import annotations

import ast
from pathlib import Path

import httpx
import pytest

from ne.serve.client import ServeClient
from ne.serve.config import ServeSettings
from ne.serve.errors import UnsafeEndpointError

_SRC_DIR = Path(__file__).resolve().parents[1] / "src" / "ne" / "serve"

_FORBIDDEN_TOP_LEVEL = {"httpx", "requests", "aiohttp", "urllib3", "socket"}
_FORBIDDEN_DOTTED_PREFIXES = ("urllib.request", "http.client")
# Only client.py — the OpenAI-compatible transport — may speak raw HTTP.
ALLOWED_HTTP_MODULES = {"client.py"}


def _imports_forbidden_http(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top in _FORBIDDEN_TOP_LEVEL:
                    return True
                if alias.name.startswith(_FORBIDDEN_DOTTED_PREFIXES):
                    return True
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            top = module.split(".")[0]
            if top in _FORBIDDEN_TOP_LEVEL:
                return True
            if module.startswith(_FORBIDDEN_DOTTED_PREFIXES):
                return True
    return False


def test_only_client_imports_a_raw_http_client() -> None:
    offenders = set()
    for path in sorted(_SRC_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if _imports_forbidden_http(tree):
            offenders.add(path.name)
    assert offenders == ALLOWED_HTTP_MODULES, (
        "A module under ne/serve imports a raw HTTP client. Only client.py may "
        "(the sanctioned OpenAI-compatible transport). If you truly need another "
        "HTTP seam, extend ALLOWED_HTTP_MODULES CONSCIOUSLY — every such module is "
        f"a potential exfil path (C2). offenders={offenders}"
    )


def test_base_url_is_always_validate_call() -> None:
    tree = ast.parse((_SRC_DIR / "client.py").read_text(encoding="utf-8"))
    assignments = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        targets_base_url = any(
            isinstance(t, ast.Attribute)
            and t.attr == "_base_url"
            and isinstance(t.value, ast.Name)
            and t.value.id == "self"
            for t in node.targets
        )
        if not targets_base_url:
            continue
        assignments += 1
        value = node.value
        assert isinstance(value, ast.Call), "self._base_url must be set from a call"
        func = value.func
        assert isinstance(func, ast.Name) and func.id == "validate_in_vpc_endpoint", (
            "self._base_url must be assigned the result of validate_in_vpc_endpoint(...) — "
            "never a bare rstrip on an unvalidated config URL (C2)"
        )
    assert assignments >= 1, "no self._base_url assignment found in client.py"


_HOSTILE = [
    "http://169.254.169.254",
    "http://metadata.google.internal",
    "https://evil.example.com",
    "http://2852039166",
]


@pytest.mark.parametrize("endpoint", _HOSTILE)
def test_construction_rejects_hostile_endpoint(endpoint: str) -> None:
    with pytest.raises(UnsafeEndpointError):
        ServeClient(endpoint)


def test_construction_accepts_in_vpc_and_allowlisted() -> None:
    assert ServeClient("http://vllm.local").base_url == "http://vllm.local"
    assert ServeClient("http://localhost:8090").base_url == "http://localhost:8090"
    allowlisted = ServeClient(
        "https://vllm.corp.example.com",
        settings=ServeSettings(allowed_endpoint_hosts=("vllm.corp.example.com",)),
    )
    assert allowlisted.base_url == "https://vllm.corp.example.com"


def test_rejected_endpoint_emits_zero_requests() -> None:
    recorded: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        return httpx.Response(200)

    transport = httpx.MockTransport(handler)
    recording_client = httpx.AsyncClient(transport=transport)
    with pytest.raises(UnsafeEndpointError):
        ServeClient("http://169.254.169.254", client=recording_client)
    assert recorded == []  # exfil never POSTs — refused before any I/O
