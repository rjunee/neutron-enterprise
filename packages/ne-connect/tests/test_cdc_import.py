# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Import-hygiene guardian for ne.connect.cdc (NE-084).

The CDC sink is consumed by ne-api at runtime, so it MUST NOT drag ``dlt`` (the
connector-framework ETL runtime) into the process — the SPEC row's dlt-free
requirement. This mirrors ``test_dlt_confinement.py``:

* RUNTIME: importing ``ne.connect.cdc`` in a fresh interpreter never pulls
  ``dlt`` into ``sys.modules``.
* SOURCE: no module under ``cdc/`` imports ``dlt`` or an outbound-HTTP client
  (``httpx``/``requests``/…) — this keeps the C2 egress guardian
  (``test_c2_egress_guardian.py``) allowlist untouched: CDC adds zero egress
  surface. (Note: importing the ``ne.connect`` PARENT package already pulls
  ``httpx`` via the m365/slack clients, so httpx-freedom is asserted at the CDC
  SOURCE level — the guardable, meaningful boundary — not via ``sys.modules``.)
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

_CDC_SRC = Path(__file__).resolve().parents[1] / "src" / "ne" / "connect" / "cdc"

_FORBIDDEN = {"dlt", "httpx", "requests", "aiohttp", "urllib3", "socket"}


def test_import_cdc_does_not_drag_dlt_at_runtime() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import ne.connect.cdc, sys; "
            "assert 'dlt' not in sys.modules, 'ne.connect.cdc dragged dlt'; "
            "print('ok')",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


def test_cdc_source_imports_no_dlt_or_http_client() -> None:
    offenders: list[str] = []
    for path in _CDC_SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in _FORBIDDEN:
                        offenders.append(f"{path.name}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                root = (node.module or "").split(".")[0]
                if root in _FORBIDDEN:
                    offenders.append(f"{path.name}: from {node.module} import ...")
    assert not offenders, f"CDC source imports a forbidden module: {offenders}"
