# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""THE §6.3 GUARDIAN (design §6.3, A-P1-5, NE-T2X-5): a Tier-1 write.internal.*
action authorized on PROVISIONAL (unconfirmed, LLM-extracted) context in
``read_flagged`` mode CANNOT execute without a granted, named human approval.

Runs through the REAL in-process Cedar engine
(``CedarGovernedPdp(CedarEvaluator(), sink)``) — ``cedarpy`` is imported
DIRECTLY, no ``importorskip``, matching ``test_cedar_evaluator.py``'s declared
no-silent-skip stance. A stubbed PDP here would be vacuous: the stub would
DECIDE the escalation, not the policy — this file exists to prove the REAL
bundle escalates, not that ``GovernedToolExecutor`` correctly obeys whatever a
double tells it.

A thin passthrough ``SpyPdp`` wraps the real ``CedarGovernedPdp`` so tests can
assert on the EXACT ``PolicyRequest`` the real PDP received, without changing
what it decides (derive, don't fake — the delegation is unchanged).
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import cedarpy  # noqa: F401 - imported directly on purpose, see module docstring

from ne.contracts.governance import (
    ApprovalOutcome,
    ApprovalRequest,
    ApprovalStatus,
    PolicyDecision,
    PolicyRequest,
)
from ne.govern.cedar.evaluator import CedarEvaluator
from ne.govern.cedar.pdp import CedarGovernedPdp
from ne.runtime.errors import ApprovalNotGrantedError, ToolNotRegisteredError
from ne.runtime.middleware import GovernedToolExecutor
from ne.runtime.provisional import provisional_context_scope

from .conftest import (
    RecordingAuditSink,
    ToolLog,
    make_guardrails,
    make_principal,
    standard_bindings,
)

# --- test doubles ------------------------------------------------------------


class SpyPdp:
    """A passthrough PDP: delegates EVERY decision unchanged, captures every request."""

    def __init__(self, inner: CedarGovernedPdp) -> None:
        self._inner = inner
        self.requests: list[PolicyRequest] = []

    async def decide(self, request: PolicyRequest) -> PolicyDecision:
        self.requests.append(request)
        return await self._inner.decide(request)


class RefusingApprovalGate:
    """Records every request; NEVER grants (the "cannot execute" arm)."""

    def __init__(self) -> None:
        self.requests: list[ApprovalRequest] = []

    async def request_approval(self, request: ApprovalRequest) -> ApprovalOutcome:
        self.requests.append(request)
        return ApprovalOutcome(status=ApprovalStatus.REJECTED, approver=None)


class GrantingApprovalGate:
    """Records every request; ALWAYS grants with a named approver."""

    def __init__(self, approver: str = "steward-1") -> None:
        self.requests: list[ApprovalRequest] = []
        self._approver = approver

    async def request_approval(self, request: ApprovalRequest) -> ApprovalOutcome:
        self.requests.append(request)
        return ApprovalOutcome(status=ApprovalStatus.APPROVED, approver=self._approver)


# The two canonical bindings this guardian needs (tier-0 read, tier-1 write)
# ride the SAME `standard_bindings`/`make_guardrails` fixtures every other
# middleware test uses (real GuardrailEngine, real allowlisted tool policy) —
# only the PDP is swapped for the REAL Cedar backend (spied, not faked).
_WRITE_TOOL = "write.internal.notes.append"
_READ_TOOL = "read.docs.search"

_REFS = ("llm:aaaaaaaaaaaaaaaa:e1", "llm:bbbbbbbbbbbbbbbb:e2")


def _make_executor(
    log: ToolLog, *, approval_gate: Any, sink: RecordingAuditSink
) -> tuple[GovernedToolExecutor, SpyPdp]:
    real_pdp = CedarGovernedPdp(CedarEvaluator(), sink)
    spy = SpyPdp(real_pdp)
    executor = GovernedToolExecutor(
        standard_bindings(log),
        pdp=spy,
        guardrails=make_guardrails(sink),
        approval_gate=approval_gate,
        sink=sink,
    )
    return executor, spy


# --- (1) cannot execute without approval ------------------------------------


def test_tier1_write_with_provisional_context_cannot_execute_without_approval() -> None:
    log = ToolLog()
    sink = RecordingAuditSink()
    gate = RefusingApprovalGate()
    executor, _spy = _make_executor(log, approval_gate=gate, sink=sink)
    principal = make_principal()

    async def scenario() -> None:
        with provisional_context_scope() as acc:
            acc.record(
                provisional_source_count=2,
                provisional_source_refs=_REFS,
                extraction_mode="read_flagged",
            )
            await executor.execute(_WRITE_TOOL, {"note": "x"}, principal=principal)

    try:
        asyncio.run(scenario())
        raised = False
    except ApprovalNotGrantedError:
        raised = True

    assert raised, "expected ApprovalNotGrantedError — the write must not execute"
    assert log.names() == [], "handler must NEVER run without a granted human approval"
    assert len(gate.requests) == 1
    assert int(gate.requests[0].tier) == 2  # escalated from tier 1 -> tier 2


# --- (2) bidirectional control: no provisional context -> autonomous -------


def test_same_action_without_provisional_context_executes_autonomously() -> None:
    log = ToolLog()
    sink = RecordingAuditSink()
    gate = RefusingApprovalGate()  # must NEVER be consulted on this path
    executor, _spy = _make_executor(log, approval_gate=gate, sink=sink)
    principal = make_principal()

    result = asyncio.run(executor.execute(_WRITE_TOOL, {"note": "x"}, principal=principal))

    assert result == "noted"
    assert log.names() == [_WRITE_TOOL]
    assert gate.requests == []  # zero gate interactions — the control


# --- (3) autonomous mode threads the flag but does not escalate -------------


def test_autonomous_mode_threads_flag_but_does_not_escalate() -> None:
    log = ToolLog()
    sink = RecordingAuditSink()
    gate = RefusingApprovalGate()  # must NEVER be consulted — no escalation
    executor, spy = _make_executor(log, approval_gate=gate, sink=sink)
    principal = make_principal()

    async def scenario() -> str:
        with provisional_context_scope() as acc:
            acc.record(
                provisional_source_count=1,
                provisional_source_refs=_REFS[:1],
                extraction_mode="autonomous",
            )
            return await executor.execute(_WRITE_TOOL, {"note": "x"}, principal=principal)

    result = asyncio.run(scenario())

    assert result == "noted"
    assert log.names() == [_WRITE_TOOL]
    assert gate.requests == []  # no approval consult: YOLO does not escalate

    # VALUE assertions on the captured PolicyRequest (not mere presence): the
    # flag genuinely threaded even though it did not escalate.
    assert len(spy.requests) == 1
    context = spy.requests[0].context
    assert context["context_provisional"] is True
    assert context["extraction_mode"] == "autonomous"


# --- (4) unknown mode fails TOWARD escalation -------------------------------


def test_unknown_mode_fails_toward_escalation() -> None:
    log = ToolLog()
    sink = RecordingAuditSink()
    gate = RefusingApprovalGate()
    executor, _spy = _make_executor(log, approval_gate=gate, sink=sink)
    principal = make_principal()

    async def scenario() -> None:
        with provisional_context_scope() as acc:
            acc.record(
                provisional_source_count=1,
                provisional_source_refs=_REFS[:1],
                extraction_mode="weird",
            )
            await executor.execute(_WRITE_TOOL, {"note": "x"}, principal=principal)

    try:
        asyncio.run(scenario())
        raised = False
    except ApprovalNotGrantedError:
        raised = True

    assert raised
    assert log.names() == []
    assert len(gate.requests) == 1


# --- (5) granted approval allows execution; payload lists refs (exact) -----


def test_granted_approval_allows_execution_and_payload_lists_refs() -> None:
    log = ToolLog()
    sink = RecordingAuditSink()
    gate = GrantingApprovalGate()
    executor, _spy = _make_executor(log, approval_gate=gate, sink=sink)
    principal = make_principal()

    async def scenario() -> str:
        with provisional_context_scope() as acc:
            acc.record(
                provisional_source_count=2,
                provisional_source_refs=_REFS,
                extraction_mode="read_flagged",
            )
            return await executor.execute(_WRITE_TOOL, {"note": "x"}, principal=principal)

    result = asyncio.run(scenario())

    assert result == "noted"
    assert log.names() == [_WRITE_TOOL]
    assert len(gate.requests) == 1
    # ApprovalRequest.payload is a FrozenDict (AnyMap) — nested lists freeze to
    # tuples, so the refs round-trip as a tuple, not a list.
    #
    # This WAS an exact-equality assertion on the whole payload, and its purpose
    # was to catch payload DRIFT — an unnoticed key appearing is exactly what it
    # existed to fail on. NE-APPROVAL-PAYLOAD adds a legitimate `disclosure` key
    # (the material a named approver is shown), so exact equality against the old
    # three-key dict can no longer hold.
    #
    # It is widened by ENUMERATING the permitted key set rather than by relaxing
    # to a subset check. A subset check would silently accept the next unexpected
    # key, which is the property this test was written to prevent — the assertion
    # below still fails on drift, it just knows about one more key now.
    payload = dict(gate.requests[0].payload)
    assert set(payload) == {
        "tool",
        "provisional_source_count",
        "provisional_source_refs",
        "disclosure",
    }
    assert payload["tool"] == _WRITE_TOOL
    assert payload["provisional_source_count"] == 2
    assert payload["provisional_source_refs"] == tuple(_REFS)
    # The disclosure is shaped, not merely present — a key holding None or a
    # string would satisfy presence and prove nothing about what an approver
    # would be shown.
    disclosure = payload["disclosure"]
    assert isinstance(disclosure, Mapping), disclosure
    assert "target_resource" in disclosure and "reversibility" in disclosure, disclosure


# --- (6) a read tool with provisional context is NOT escalated --------------


def test_read_tool_with_provisional_context_not_escalated() -> None:
    log = ToolLog()
    sink = RecordingAuditSink()
    gate = RefusingApprovalGate()
    executor, _spy = _make_executor(log, approval_gate=gate, sink=sink)
    principal = make_principal()

    async def scenario() -> str:
        with provisional_context_scope() as acc:
            acc.record(
                provisional_source_count=3,
                provisional_source_refs=_REFS,
                extraction_mode="read_flagged",
            )
            return await executor.execute(_READ_TOOL, {"q": "x"}, principal=principal)

    result = asyncio.run(scenario())

    assert result == "docs: 3 results"
    assert log.names() == [_READ_TOOL]
    assert gate.requests == []


# --- (7) no active scope -> PDP context is byte-identical to today ----------


def test_no_scope_means_byte_identical_pdp_context() -> None:
    log = ToolLog()
    sink = RecordingAuditSink()
    gate = RefusingApprovalGate()
    executor, spy = _make_executor(log, approval_gate=gate, sink=sink)
    principal = make_principal()

    result = asyncio.run(executor.execute(_READ_TOOL, {"q": "x"}, principal=principal))

    assert result == "docs: 3 results"
    assert len(spy.requests) == 1
    assert spy.requests[0].context == {"tier": 0}


# --- sanity: the executor never fell back to a fake/unregistered tool ------


def test_wrong_tool_name_is_never_registered_as_a_bypass() -> None:
    log = ToolLog()
    sink = RecordingAuditSink()
    gate = RefusingApprovalGate()
    executor, _spy = _make_executor(log, approval_gate=gate, sink=sink)
    principal = make_principal()

    try:
        asyncio.run(
            executor.execute("write.internal.notes.NOT-REGISTERED", {}, principal=principal)
        )
        raised = False
    except ToolNotRegisteredError:
        raised = True
    assert raised
