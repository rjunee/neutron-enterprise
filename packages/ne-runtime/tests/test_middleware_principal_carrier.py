# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""D-1 (MERGE-BLOCKING): the ``_run_handler`` tool-principal carrier.

The carrier is set ONLY from the VERIFIED ``principal`` argument, reset in a
``finally`` — so it is None outside ``execute()``, reset on the exception path, and
nested-execute isolated (a sub-agent executing as a DISTINCT principal Q inside
parent P's handler never lets Q's handler observe P, and P is restored after Q
returns). A model-supplied arg value never influences the carrier.
"""

from __future__ import annotations

import asyncio

import pytest

from ne.contracts.runtime import ToolSpec
from ne.contracts.types import ApprovalTier, Principal
from ne.runtime.errors import ToolExecutionError
from ne.runtime.middleware import GovernedToolExecutor, current_tool_principal
from ne.runtime.tools import ToolBinding

from .conftest import (
    RecordingAuditSink,
    UnusedGate,
    make_executor,
    make_guardrails,
    make_pdp,
    make_principal,
)


def _binding(name: str, handler, tier: ApprovalTier = ApprovalTier.TIER_0) -> ToolBinding:
    return ToolBinding(
        spec=ToolSpec(
            name=name,
            description="carrier probe",
            tier=tier,
            input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
        ),
        handler=handler,
    )


def test_carrier_visible_in_handler_and_reset_after() -> None:
    seen: list[Principal | None] = []

    async def _handler(args) -> str:
        seen.append(current_tool_principal())
        return "ok"

    sink = RecordingAuditSink()
    executor = make_executor(
        [_binding("read.docs.search", _handler)], sink, approval_gate=UnusedGate()
    )
    principal = make_principal("user:ada")

    assert current_tool_principal() is None
    asyncio.run(executor.execute("read.docs.search", {"q": "x"}, principal=principal))
    assert seen == [principal]
    assert current_tool_principal() is None


def test_exception_path_resets_carrier() -> None:
    async def _boom(args) -> str:
        assert current_tool_principal() is not None
        raise RuntimeError("handler failure")

    sink = RecordingAuditSink()
    executor = make_executor(
        [_binding("read.docs.search", _boom)], sink, approval_gate=UnusedGate()
    )
    with pytest.raises(ToolExecutionError):
        asyncio.run(executor.execute("read.docs.search", {"q": "x"}, principal=make_principal()))
    assert current_tool_principal() is None


def test_nested_execute_isolation() -> None:
    observed_inner: list[str | None] = []
    observed_outer_after: list[str | None] = []

    P = make_principal("user:parent")
    Q = make_principal("user:child")
    holder: dict[str, GovernedToolExecutor] = {}

    async def _inner(args) -> str:
        cur = current_tool_principal()
        observed_inner.append(cur.id if cur else None)
        return "inner-ok"

    async def _outer(args) -> str:
        cur_before = current_tool_principal()
        assert cur_before is not None and cur_before.id == "user:parent"
        # Parent P's handler dispatches a sub-agent execute as a DISTINCT principal Q.
        await holder["executor"].execute("write.internal.notes.append", {"note": "n"}, principal=Q)
        cur_after = current_tool_principal()
        observed_outer_after.append(cur_after.id if cur_after else None)
        return "outer-ok"

    sink = RecordingAuditSink()
    executor = GovernedToolExecutor(
        [
            _binding("read.docs.search", _outer),
            _binding("write.internal.notes.append", _inner, tier=ApprovalTier.TIER_1),
        ],
        pdp=make_pdp(sink),
        approval_gate=UnusedGate(),
        guardrails=make_guardrails(sink),
        sink=sink,
    )
    holder["executor"] = executor

    asyncio.run(executor.execute("read.docs.search", {"q": "x"}, principal=P))
    # Q was observed inside the inner handler; P restored after the inner returns.
    assert observed_inner == ["user:child"]
    assert observed_outer_after == ["user:parent"]
    assert current_tool_principal() is None


def test_carrier_taken_from_verified_arg_not_model_args() -> None:
    seen: list[str | None] = []

    async def _handler(args) -> str:
        # Even though the model-supplied arg VALUE names another principal, the
        # carrier reflects the VERIFIED principal= argument only.
        cur = current_tool_principal()
        seen.append(cur.id if cur else None)
        return "ok"

    sink = RecordingAuditSink()
    executor = make_executor(
        [_binding("read.docs.search", _handler)], sink, approval_gate=UnusedGate()
    )
    asyncio.run(
        executor.execute(
            "read.docs.search",
            {"q": "user:attacker"},
            principal=make_principal("user:ada"),
        )
    )
    assert seen == ["user:ada"]
