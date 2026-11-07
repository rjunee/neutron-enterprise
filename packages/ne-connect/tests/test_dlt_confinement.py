# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""C1: dlt telemetry forced off pre-import + _dlt.py is the sole import site."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src" / "ne" / "connect"
_DLT_MODULE = _SRC / "_dlt.py"


def test_fresh_interpreter_has_telemetry_forced_off() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import ne.connect._dlt, os, sys; "
            "assert os.environ['RUNTIME__DLTHUB_TELEMETRY'] == 'false'; "
            "assert 'dlt' in sys.modules",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_env_force_precedes_dlt_import_in_source() -> None:
    # Scan CODE lines (a docstring mentions "import dlt" too) — the env force
    # statement must appear before the `import dlt` statement line.
    lines = _DLT_MODULE.read_text(encoding="utf-8").splitlines()
    env_line = next(
        i
        for i, line in enumerate(lines)
        if line.strip() == 'os.environ["RUNTIME__DLTHUB_TELEMETRY"] = "false"'
    )
    import_line = next(i for i, line in enumerate(lines) if line.strip() == "import dlt")
    assert env_line < import_line


def test_dlt_module_is_sole_import_site() -> None:
    offenders: list[str] = []
    for path in _SRC.rglob("*.py"):
        if path == _DLT_MODULE:
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("import dlt") or stripped.startswith("from dlt"):
                offenders.append(f"{path}: {stripped}")
    assert not offenders, f"dlt imported outside _dlt.py: {offenders}"
