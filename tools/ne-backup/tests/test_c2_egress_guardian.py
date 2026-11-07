# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""C2 egress structural guardian (mirrors ne-brain's).

Only ``qdrant.py`` may import an outbound-HTTP client, its endpoint is validated
at construction, and a rejected endpoint emits ZERO requests.
"""

from __future__ import annotations

import ast
from pathlib import Path

import httpx
import pytest

from ne.backup.qdrant import Qdrant
from ne.net import UnsafeEndpointError

_SRC = Path(__file__).resolve().parents[1] / "src" / "ne" / "backup"

ALLOWED_HTTP_MODULES = {"qdrant.py"}
_FORBIDDEN_TOP_LEVEL = {"httpx", "requests", "aiohttp", "urllib3", "socket"}
_FORBIDDEN_DOTTED_PREFIXES = ("urllib.request", "http.client")


def _module_imports_http_client(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top in _FORBIDDEN_TOP_LEVEL or alias.name.startswith(_FORBIDDEN_DOTTED_PREFIXES):
                    return True
        elif isinstance(node, ast.ImportFrom):
            if node.level != 0 or node.module is None:
                continue
            top = node.module.split(".")[0]
            if top in _FORBIDDEN_TOP_LEVEL or node.module.startswith(_FORBIDDEN_DOTTED_PREFIXES):
                return True
    return False


def test_only_qdrant_imports_an_http_client() -> None:
    offenders: set[str] = set()
    for path in _SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if _module_imports_http_client(tree):
            offenders.add(path.relative_to(_SRC).as_posix())
    assert offenders == ALLOWED_HTTP_MODULES, f"unexpected HTTP importers: {sorted(offenders)}"


def test_base_url_assignment_goes_through_validator() -> None:
    tree = ast.parse((_SRC / "qdrant.py").read_text(encoding="utf-8"))
    assigns = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Attribute) and t.attr == "_base_url" for t in node.targets)
    ]
    assert assigns, "expected a self._base_url assignment in qdrant.py"
    for assign in assigns:
        call = assign.value
        assert isinstance(call, ast.Call)
        func = call.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        assert name == "validate_in_vpc_endpoint"


@pytest.mark.parametrize(
    "url",
    ["http://169.254.169.254", "https://example.com", "http://metadata.google.internal"],
)
def test_rejected_endpoint_emits_no_request(url: str) -> None:
    recorded: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        return httpx.Response(200, json={})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(UnsafeEndpointError):
        Qdrant(url, client=client)
    assert recorded == []


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1:6333", "http://qdrant:6333", "http://[::1]:6333"]
)
def test_loopback_and_service_endpoints_accepted(url: str) -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    # Construction must not raise for in-VPC / loopback endpoints.
    Qdrant(url, client=client)
