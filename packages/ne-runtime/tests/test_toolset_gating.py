# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ToolsetScopedExecutor — per-agent/per-job toolset gating, ENFORCED not advisory (NE-034)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest

from ne.contracts.governance import PolicyDecision, PolicyRequest
from ne.runtime.errors import (
    RuntimeAuditError,
    ToolsetConfigurationError,
    ToolsetViolationError,
)
from ne.runtime.middleware import GovernedToolExecutor, scoped_tool_executor
from ne.runtime.run_runner import ReceiptGatedToolExecutor
from ne.runtime.toolset import ToolsetScopedExecutor
from ne.schema import AuditOutcome

from .conftest import (
    FailingAuditSink,
    NamedApproverGate,
    RecordingAuditSink,
    ToolLog,
    make_guardrails,
    make_pdp,
    make_principal,
    standard_bindings,
)


class _RecordingPdp:
    """Wraps a real GovernedPdp and records every PolicyRequest (context assertions)."""

    def __init__(self, inner: object) -> None:
        self._inner = inner
        self.requests: list[PolicyRequest] = []

    async def decide(self, request: PolicyRequest) -> PolicyDecision:
        self.requests.append(request)
        return await self._inner.decide(request)  # type: ignore[attr-defined,no-any-return]


def _pieces(log: ToolLog, sink: RecordingAuditSink) -> tuple[GovernedToolExecutor, _RecordingPdp]:
    pdp = _RecordingPdp(make_pdp(sink))
    executor = GovernedToolExecutor(
        standard_bindings(log),
        pdp=pdp,  # type: ignore[arg-type]
        guardrails=make_guardrails(sink),
        approval_gate=NamedApproverGate("alice"),
        sink=sink,
    )
    return executor, pdp


def test_allowed_tool_executes_through_full_governed_pipeline() -> None:
    log = ToolLog()
    sink = RecordingAuditSink()
    executor, pdp = _pieces(log, sink)
    scoped = ToolsetScopedExecutor(
        executor,
        granted=("read.docs.search",),
        sink=sink,
        scope="cron:x",
        pdp=pdp,  # type: ignore[arg-type]
    )

    out = asyncio.run(scoped.execute("read.docs.search", {"q": "hi"}, principal=make_principal()))

    assert out == "docs: 3 results"
    assert log.names() == ["read.docs.search"]  # the handler ran
    assert sink.with_action("pdp.decide")  # governed PDP consulted
    executes = sink.with_action("tool.execute")
    assert len(executes) == 1
    assert executes[0].outcome == AuditOutcome.success.value
    assert sink.with_action("tool.toolset_denied") == []


def test_receipt_gated_executor_retains_authority_when_agent_scope_is_applied() -> None:
    """Regression: Run receipt gating must not drop the sink/PDP scoping authority."""
    log = ToolLog()
    sink = RecordingAuditSink()
    executor, pdp = _pieces(log, sink)
    receipt_gated = ReceiptGatedToolExecutor(
        executor,
        audit=cast(Any, object()),
        run=cast(Any, SimpleNamespace()),
        target=cast(Any, SimpleNamespace()),
        owner_principal_id="user:ada",
        step_id="run-tool",
    )

    scoped = scoped_tool_executor(
        receipt_gated,
        granted=("read.docs.search",),
        scope="agent-run:run-1:attempt-1",
    )

    assert {spec.name for spec in scoped.tool_specs} == {"read.docs.search"}
    with pytest.raises(ToolsetViolationError):
        asyncio.run(
            scoped.execute("write.internal.notes.append", {"note": "x"}, principal=make_principal())
        )
    assert log.calls == []
    assert sink.with_action("tool.toolset_denied")
    assert pdp.requests[-1].context.get("toolset_gated") is True


def test_out_of_set_tool_denied_audited_and_pdp_consulted_never_executes() -> None:
    log = ToolLog()
    sink = RecordingAuditSink()
    executor, pdp = _pieces(log, sink)
    scoped = ToolsetScopedExecutor(
        executor,
        granted=("read.docs.search",),
        sink=sink,
        scope="cron:x",
        pdp=pdp,  # type: ignore[arg-type]
    )

    with pytest.raises(ToolsetViolationError):
        asyncio.run(
            scoped.execute("write.internal.notes.append", {"note": "x"}, principal=make_principal())
        )

    # Audited fail-closed as toolset_denied, PDP consulted WITH the toolset_gated
    # context, and the handler never ran.
    denied = sink.with_action("tool.toolset_denied")
    assert len(denied) == 1
    assert denied[0].outcome == AuditOutcome.denied.value
    assert denied[0].resource == "tool:write.internal.notes.append"
    assert pdp.requests and pdp.requests[-1].context.get("toolset_gated") is True
    assert sink.with_action("pdp.decide")
    assert log.calls == []
    assert sink.with_action("tool.execute") == []


def test_tool_specs_filtered_to_granted() -> None:
    log = ToolLog()
    sink = RecordingAuditSink()
    executor, pdp = _pieces(log, sink)
    scoped = ToolsetScopedExecutor(
        executor,
        granted=("read.docs.search", "finance.payment.initiate"),
        sink=sink,
        scope="s",
        pdp=pdp,  # type: ignore[arg-type]
    )
    names = {s.name for s in scoped.tool_specs}
    assert names == {"read.docs.search", "finance.payment.initiate"}
    assert all(not hasattr(s, "handler") for s in scoped.tool_specs)


def test_grant_of_unregistered_name_rejected_at_construction() -> None:
    log = ToolLog()
    sink = RecordingAuditSink()
    executor, pdp = _pieces(log, sink)
    with pytest.raises(ToolsetConfigurationError):
        ToolsetScopedExecutor(
            executor,
            granted=("read.docs.search", "no.such.tool"),
            sink=sink,
            scope="s",
            pdp=pdp,  # type: ignore[arg-type]
        )


def test_empty_grant_means_zero_tools_and_all_calls_denied() -> None:
    log = ToolLog()
    sink = RecordingAuditSink()
    executor, pdp = _pieces(log, sink)
    scoped = ToolsetScopedExecutor(executor, granted=(), sink=sink, scope="s", pdp=pdp)  # type: ignore[arg-type]

    assert scoped.tool_specs == ()
    with pytest.raises(ToolsetViolationError):
        asyncio.run(scoped.execute("read.docs.search", {"q": "x"}, principal=make_principal()))
    assert log.calls == []
    assert sink.with_action("tool.toolset_denied")


def test_toolset_denial_audit_failure_fails_closed() -> None:
    """A FailingAuditSink on the toolset_denied emit raises RuntimeAuditError BEFORE the PDP."""
    log = ToolLog()
    audit_sink = RecordingAuditSink()  # inner executor + its pdp audit here
    executor, pdp = _pieces(log, audit_sink)
    failing = FailingAuditSink()
    scoped = ToolsetScopedExecutor(
        executor,
        granted=("read.docs.search",),
        sink=failing,
        scope="s",
        pdp=pdp,  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeAuditError):
        asyncio.run(
            scoped.execute("write.internal.notes.append", {"note": "x"}, principal=make_principal())
        )

    # The audit for the denial failed, so the PDP was never consulted (fail closed).
    assert pdp.requests == []
    assert log.calls == []
