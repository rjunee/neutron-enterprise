# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""C3 model-independence guardrails: the litellm import chokepoint.

Until NE-012's import-linter contract lands, these grep/attribute tests are the
enforcement that ``_litellm.py`` is the single repo-wide litellm import and that
telemetry is provably off.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_IMPORT_RE = re.compile(r"^\s*(import litellm|from litellm)")

# tests/ -> ne-gateway/ -> packages/ -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[3]
_CHOKEPOINT = Path("packages/ne-gateway/src/ne/gateway/_litellm.py")


def test_only_litellm_module_imports_litellm() -> None:
    hits: list[Path] = []
    for path in sorted((_REPO_ROOT / "packages").glob("*/src/**/*.py")):
        text = path.read_text(encoding="utf-8")
        if any(_IMPORT_RE.match(line) for line in text.splitlines()):
            hits.append(path.relative_to(_REPO_ROOT))
    assert hits == [_CHOKEPOINT], f"unexpected litellm importers: {hits}"


def test_telemetry_forced_off() -> None:
    from ne.gateway import _litellm

    assert _litellm.litellm.telemetry is False
    assert _litellm.litellm.suppress_debug_info is True
    assert _litellm.litellm.success_callback == []
    assert _litellm.litellm.failure_callback == []
    assert _litellm.litellm.callbacks == []
    assert os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] == "True"
    assert os.environ["LITELLM_TELEMETRY"] == "False"
