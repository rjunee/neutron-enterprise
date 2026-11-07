# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""C2 egress structural guardian for ne-chat (mirrors ne-connect's guardian).

Makes it impossible to reintroduce a second, un-guarded outbound HTTP client
silently: under ``src/ne/chat`` ONLY ``slack/client.py`` may import an
outbound-HTTP client library. The single-egress-site invariant is what lets the
signature-verify / allowlist / air-gap guarantees hold for the whole package.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src" / "ne" / "chat"

#: The ONLY ne.chat modules permitted to import an outbound-HTTP client. NE-080
#: consciously extends this guardian for the Teams client — a new egress module
#: keeps the single-egress-site constraint airtight in the SAME PR that
#: introduces it (quality-policy §2 item 4).
ALLOWED_HTTP_MODULES = {"slack/client.py", "teams/client.py"}

_FORBIDDEN_TOP_LEVEL = {"httpx", "requests", "aiohttp", "urllib3", "socket"}
_FORBIDDEN_DOTTED_PREFIXES = ("urllib.request", "http.client")


def _imports_http_client(tree: ast.AST) -> bool:
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


def test_only_client_module_imports_an_http_client() -> None:
    offenders: set[str] = set()
    for path in _SRC.rglob("*.py"):
        rel = path.relative_to(_SRC).as_posix()
        if _imports_http_client(ast.parse(path.read_text(encoding="utf-8"))):
            offenders.add(rel)
    assert offenders == ALLOWED_HTTP_MODULES, (
        "ne-chat outbound HTTP is C2-guarded to slack/client.py alone; a new egress "
        f"module must consciously extend ALLOWED_HTTP_MODULES. offenders={sorted(offenders)}"
    )
