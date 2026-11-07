# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Executor-level proof that a guardrail block lands on the run's trace.

The unit proof lives in ``ne-govern``'s ``test_guardrail_trace_correlation.py``;
this asserts the same invariant through the REAL
:class:`~ne.runtime.middleware.GovernedToolExecutor`, on the path the demo
actually walks: :class:`~ne.govern.guardrails.engine.GuardrailEngine` is step (1)
of ``execute``, BEFORE the PDP, and the recipient allowlist is deny-by-default,
so a non-allowlisted recipient is blocked there and the call never reaches the
PDP at all. That makes ``guardrail.block`` the ONLY event explaining why the
action stopped — and before this fix it carried ``trace_id: null``, i.e. it was
invisible on the trace it exists to explain.

Both halves, as with ``pdp.decide``: inside a run the event carries that run's
EXACT id; outside any run it still carries ``None``, which is the honest value
and must never be minted or defaulted.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from ne.events.correlation import current_run_trace_id, run_trace_scope
from ne.govern.errors import GuardrailViolation

from .conftest import (
    RecordingAuditSink,
    ToolLog,
    UnusedGate,
    make_executor,
    make_principal,
    standard_bindings,
)

_BLOCKED_RECIPIENT = "attacker@evil.example"


def _block_and_collect(sink: RecordingAuditSink) -> list[object]:
    log = ToolLog()
    executor = make_executor(standard_bindings(log), sink, approval_gate=UnusedGate())

    async def scenario() -> None:
        with pytest.raises(GuardrailViolation):
            await executor.execute(
                "write.external.email.send",
                {"to": _BLOCKED_RECIPIENT, "body": "x"},
                principal=make_principal(),
            )

    asyncio.run(scenario())
    assert log.calls == [], "the blocked call must never reach a handler"
    blocks = [e for e in sink.events if getattr(e, "action", "") == "guardrail.block"]
    assert len(blocks) == 1
    # The block really did pre-empt the PDP, so guardrail.block is the sole record.
    assert [e for e in sink.events if getattr(e, "action", "") == "pdp.decide"] == []
    return blocks


def test_blocked_recipient_inside_a_run_carries_that_runs_exact_trace_id() -> None:
    sink = RecordingAuditSink()
    run_id = uuid.uuid4().hex
    with run_trace_scope(run_id):
        (block,) = _block_and_collect(sink)
    assert block.trace_id == run_id  # type: ignore[attr-defined]
    assert current_run_trace_id() is None


def test_blocked_recipient_outside_any_run_leaves_trace_id_absent() -> None:
    assert current_run_trace_id() is None
    sink = RecordingAuditSink()
    (block,) = _block_and_collect(sink)
    assert block.trace_id is None  # type: ignore[attr-defined]
