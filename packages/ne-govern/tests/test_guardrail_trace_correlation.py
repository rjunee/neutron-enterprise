# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-DEMO-PDP-TRACE: ``guardrail.block`` / ``guardrail.error`` land on the run's trace.

The sibling of the ``pdp.decide`` defect, with the identical root cause:
:class:`~ne.govern.guardrails.engine.GuardrailEngine` never passed a ``trace_id``
to ``new_audit_event``, because the NE-166 carrier used to live in
``ne.runtime.correlation`` and ``ne-runtime`` DEPENDS ON ``ne-govern`` — reading
it from the runtime layer is a circular package dependency. The carrier now lives
in :mod:`ne.events.correlation`, below both.

Why this rail specifically: the engine is step (1) of
``GovernedToolExecutor.execute``, immediately BEFORE the PDP, and the recipient
allowlist (``NE_GOVERN_GUARDRAILS__RECIPIENTS__ALLOWED_RECIPIENTS``) is
deny-by-default. A blocked recipient inside a run is therefore frequently the one
event that explains why an action stopped — and an un-traced block is invisible
on the trace it exists to explain.

Both halves are asserted TOGETHER and neither is redundant:

  * INSIDE a run scope the event carries that run's EXACT id;
  * OUTSIDE any run scope the event still carries ``None`` — a guardrail
    evaluated outside any run (a config preflight, a bare API call) genuinely
    belongs to no trace, and a minted or constant id there would write a FALSE
    association into an append-only, hash-chained audit record.

The second half is what makes the first meaningful: a "fix" that stamped a
constant satisfies the inside-a-run assertion and fails here.
"""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal
from typing import cast

import pytest

from ne.contracts.governance import GuardrailVerdict
from ne.events.correlation import current_run_trace_id, run_trace_scope
from ne.govern.config import RecipientSettings, SpendCapSettings
from ne.govern.errors import GuardrailEvaluationError, GuardrailViolation
from ne.govern.guardrails import (
    GuardrailEngine,
    RecipientAllowlistGuardrail,
    SpendCapGuardrail,
    build_tool_call_payload,
)
from ne.govern.guardrails.spend import SpendLedger
from ne.schema import AuditEvent, AuditOutcome

from .conftest import RaisingGuardrail, RecordingAuditSink, StubGuardrail, make_principal

_PASS = GuardrailVerdict(passed=True, rule="stub.ok")


def _payload():
    return build_tool_call_payload(principal=make_principal("user:1"), tool="send")


def _only_event(sink: RecordingAuditSink) -> AuditEvent:
    assert len(sink.events) == 1
    return cast(AuditEvent, sink.events[0])


# --- the beat-3 path: a deny-by-default recipient block ---------------------


def _recipient_engine(sink: RecordingAuditSink) -> GuardrailEngine:
    # allowed_recipients is EMPTY — the deny-by-default posture the demo runs.
    return GuardrailEngine([RecipientAllowlistGuardrail(RecipientSettings())], sink)


def _outbound_payload():
    return build_tool_call_payload(
        principal=make_principal("user:1"),
        tool="write.external.email.send",
        recipients=("nobody@example.test",),
    )


def test_recipient_block_inside_a_run_carries_that_runs_exact_trace_id() -> None:
    sink = RecordingAuditSink()
    run_id = uuid.uuid4().hex
    with run_trace_scope(run_id), pytest.raises(GuardrailViolation):
        asyncio.run(_recipient_engine(sink).enforce(_outbound_payload()))
    event = _only_event(sink)
    assert event.action == "guardrail.block"
    assert event.outcome == AuditOutcome.denied
    # The EXACT id, not merely "some non-null id".
    assert event.trace_id == run_id


def test_recipient_block_outside_any_run_leaves_trace_id_absent() -> None:
    assert current_run_trace_id() is None
    sink = RecordingAuditSink()
    with pytest.raises(GuardrailViolation):
        asyncio.run(_recipient_engine(sink).enforce(_outbound_payload()))
    assert _only_event(sink).trace_id is None


# --- guardrail.block, generic rail -----------------------------------------


def test_block_inside_a_run_carries_that_runs_exact_trace_id() -> None:
    sink = RecordingAuditSink()
    fail = GuardrailVerdict(passed=False, rule="stub.blocked", detail="arg 'x' blocked")
    engine = GuardrailEngine([StubGuardrail(_PASS), StubGuardrail(fail)], sink)
    run_id = uuid.uuid4().hex
    with run_trace_scope(run_id), pytest.raises(GuardrailViolation):
        asyncio.run(engine.enforce(_payload()))
    assert _only_event(sink).trace_id == run_id


def test_block_outside_any_run_leaves_trace_id_absent() -> None:
    assert current_run_trace_id() is None
    sink = RecordingAuditSink()
    fail = GuardrailVerdict(passed=False, rule="stub.blocked", detail="arg 'x' blocked")
    engine = GuardrailEngine([StubGuardrail(fail)], sink)
    with pytest.raises(GuardrailViolation):
        asyncio.run(engine.enforce(_payload()))
    assert _only_event(sink).trace_id is None


def test_two_runs_get_two_different_trace_ids() -> None:
    # A constant stamped by a lazy "fix" would collapse these to one value.
    fail = GuardrailVerdict(passed=False, rule="stub.blocked")
    first_sink, second_sink = RecordingAuditSink(), RecordingAuditSink()
    first_id, second_id = uuid.uuid4().hex, uuid.uuid4().hex
    assert first_id != second_id
    with run_trace_scope(first_id), pytest.raises(GuardrailViolation):
        asyncio.run(GuardrailEngine([StubGuardrail(fail)], first_sink).enforce(_payload()))
    with run_trace_scope(second_id), pytest.raises(GuardrailViolation):
        asyncio.run(GuardrailEngine([StubGuardrail(fail)], second_sink).enforce(_payload()))
    assert _only_event(first_sink).trace_id == first_id
    assert _only_event(second_sink).trace_id == second_id


# --- guardrail.error (the fail-closed evaluation path) ---------------------


def test_error_inside_a_run_carries_that_runs_exact_trace_id() -> None:
    sink = RecordingAuditSink()
    engine = GuardrailEngine([RaisingGuardrail()], sink)
    run_id = uuid.uuid4().hex
    with run_trace_scope(run_id), pytest.raises(GuardrailEvaluationError):
        asyncio.run(engine.enforce(_payload()))
    event = _only_event(sink)
    assert event.action == "guardrail.error"
    assert event.outcome == AuditOutcome.error
    assert event.trace_id == run_id


def test_error_outside_any_run_leaves_trace_id_absent() -> None:
    assert current_run_trace_id() is None
    sink = RecordingAuditSink()
    engine = GuardrailEngine([RaisingGuardrail()], sink)
    with pytest.raises(GuardrailEvaluationError):
        asyncio.run(engine.enforce(_payload()))
    assert _only_event(sink).trace_id is None


# --- the NE-070H reservation-loss deny point (a SECOND _append_block site) --


class _DenyingLedger(SpendLedger):
    """A ledger whose atomic charge always LOSES (concurrent-overshoot deny)."""

    def recorded_spend(self, scope: str) -> Decimal:
        return Decimal(0)  # check() passes; the reservation is where the cap bites

    def record(self, scope: str, amount: Decimal) -> None:  # pragma: no cover - unused
        raise AssertionError("record must not be called on the reservation path")

    def charge(self, charges) -> bool:  # type: ignore[no-untyped-def]
        return False


def _spend_payload():
    return build_tool_call_payload(
        principal=make_principal("user:1"), tool="send", estimated_cost_usd="1.00"
    )


def _spend_engine(sink: RecordingAuditSink) -> GuardrailEngine:
    ledger = _DenyingLedger()
    return GuardrailEngine(
        [SpendCapGuardrail(SpendCapSettings(default_cap_usd="100"), ledger)],
        sink,
        spend_ledger=ledger,
    )


def test_reservation_loss_block_inside_a_run_carries_that_runs_exact_trace_id() -> None:
    sink = RecordingAuditSink()
    run_id = uuid.uuid4().hex
    with run_trace_scope(run_id), pytest.raises(GuardrailViolation):
        asyncio.run(_spend_engine(sink).enforce(_spend_payload()))
    assert _only_event(sink).trace_id == run_id


def test_reservation_loss_block_outside_any_run_leaves_trace_id_absent() -> None:
    assert current_run_trace_id() is None
    sink = RecordingAuditSink()
    with pytest.raises(GuardrailViolation):
        asyncio.run(_spend_engine(sink).enforce(_spend_payload()))
    assert _only_event(sink).trace_id is None


def test_trace_scope_is_reset_after_the_block() -> None:
    sink = RecordingAuditSink()
    fail = GuardrailVerdict(passed=False, rule="stub.blocked")
    with run_trace_scope(uuid.uuid4().hex), pytest.raises(GuardrailViolation):
        asyncio.run(GuardrailEngine([StubGuardrail(fail)], sink).enforce(_payload()))
    assert current_run_trace_id() is None
