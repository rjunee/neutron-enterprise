# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""T1 — grep guardian: the worker composes NOTHING governed itself (C6 over 2 procs).

The worker gets EVERYTHING from ``build_platform``; it must construct no executor,
sink, gate, ledger, or guardrail state of its own. Mirrors
``packages/ne-api/tests/test_no_bypass.py`` — the ne-api spec §4.1 no-second-executor
invariant, now quantified over the worker process.
"""

from __future__ import annotations

from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src" / "ne" / "worker"

#: Constructions that would compose a SECOND ungoverned stack — forbidden in the
#: worker src (everything comes from build_platform).
_FORBIDDEN = (
    "GovernedToolExecutor(",
    "create_tool_executor(",
    "create_audit_sink(",
    "DurableApprovalGate(",
    "create_approval_gate(",
    "create_shared_guardrail_state(",
)


def test_worker_constructs_no_second_governed_stack() -> None:
    py_files = sorted(_SRC.rglob("*.py"))
    assert py_files
    for path in py_files:
        text = path.read_text(encoding="utf-8")
        for needle in _FORBIDDEN:
            assert needle not in text, f"{path} constructs a second governed component: {needle}"


def test_worker_never_imports_the_runtime_middleware_directly() -> None:
    # The turn path reaches tools ONLY through platform.executor; no worker module
    # imports the runtime middleware or the gateway package directly.
    for path in sorted(_SRC.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert "import ne.runtime.middleware" not in text, path
        assert "import ne.gateway" not in text, path
