# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-DEMO-PDP-TRACE: the ``pdp.decide`` event lands on ITS OWN run's trace.

The defect these tests close: ``GovernedPdp._append_audit`` never passed a
``trace_id``, so EVERY governance decision was written with ``trace_id: null``
and the decision that authorized a tool call was invisible on the trace that
showed the call.

The two halves are asserted TOGETHER and neither is redundant:

  * INSIDE a run scope the event carries that run's EXACT id — a decision that
    is not on the run's trace is not on the record;
  * OUTSIDE any run scope the event still carries ``None`` — a bare API call, a
    preflight or a health probe genuinely belongs to no trace, and stamping a
    minted or constant id there would write a FALSE association into an
    append-only, hash-chained audit record. Absent must stay absent.

The second half is what makes the first half meaningful: a "fix" that stamped a
constant would satisfy the inside-a-run assertion and fail here.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, cast

import pytest

from ne.contracts.governance import PolicyRequest
from ne.contracts.types import Principal
from ne.events.correlation import current_run_trace_id, run_trace_scope
from ne.govern.cedar.evaluator import CedarEvaluator
from ne.govern.cedar.pdp import CedarGovernedPdp
from ne.govern.errors import PolicyBackendError
from ne.govern.pdp import GovernedPdp
from ne.schema import AuditEvent

from .conftest import RecordingAuditSink, make_opa_client, make_pdp, stub_opa_transport

_ALLOW_DOC = {
    "effect": "allow",
    "tier": 0,
    "reason": "vector: allow",
    "obligations": {},
    "policy_version": "1.0.0",
}


def _request() -> PolicyRequest:
    return PolicyRequest(
        principal=Principal(id="u1"),
        action="read.docs.search",
        resource="tool:read.docs.search",
        context={"tier": 0},
    )


def _only_event(sink: RecordingAuditSink) -> AuditEvent:
    assert len(sink.events) == 1
    return cast(AuditEvent, sink.events[0])


def _decide(pdp: Any, sink: RecordingAuditSink) -> AuditEvent:
    asyncio.run(pdp.decide(_request()))
    return _only_event(sink)


# --- OPA-backed GovernedPdp (the default backend) --------------------------


def test_decide_inside_a_run_carries_that_runs_exact_trace_id() -> None:
    sink = RecordingAuditSink()
    pdp = make_pdp(_ALLOW_DOC, sink)
    run_id = uuid.uuid4().hex
    with run_trace_scope(run_id) as scoped:
        assert scoped == run_id
        event = _decide(pdp, sink)
    # The EXACT id, not merely "some non-null id".
    assert event.trace_id == run_id


def test_decide_outside_any_run_leaves_trace_id_absent() -> None:
    # No run scope: the honest value is None, and it must NOT be invented.
    assert current_run_trace_id() is None
    sink = RecordingAuditSink()
    pdp = make_pdp(_ALLOW_DOC, sink)
    event = _decide(pdp, sink)
    assert event.trace_id is None


def test_two_runs_get_two_different_trace_ids() -> None:
    # A constant stamped by a lazy "fix" would collapse these to one value.
    first_sink, second_sink = RecordingAuditSink(), RecordingAuditSink()
    first_id, second_id = uuid.uuid4().hex, uuid.uuid4().hex
    assert first_id != second_id
    with run_trace_scope(first_id):
        first = _decide(make_pdp(_ALLOW_DOC, first_sink), first_sink)
    with run_trace_scope(second_id):
        second = _decide(make_pdp(_ALLOW_DOC, second_sink), second_sink)
    assert first.trace_id == first_id
    assert second.trace_id == second_id


def test_backend_failure_event_is_also_on_the_run_trace() -> None:
    # The fail-closed error audit is a governance record too: a PDP outage that
    # blocked a run must be visible ON that run's trace.
    sink = RecordingAuditSink()
    failing = GovernedPdp(make_opa_client(stub_opa_transport(None, status=500)), sink)
    run_id = uuid.uuid4().hex
    with run_trace_scope(run_id), pytest.raises(PolicyBackendError):
        asyncio.run(failing.decide(_request()))
    assert _only_event(sink).trace_id == run_id


def test_backend_failure_outside_a_run_leaves_trace_id_absent() -> None:
    sink = RecordingAuditSink()
    failing = GovernedPdp(make_opa_client(stub_opa_transport(None, status=500)), sink)
    with pytest.raises(PolicyBackendError):
        asyncio.run(failing.decide(_request()))
    assert _only_event(sink).trace_id is None


def test_trace_scope_is_reset_after_the_decision() -> None:
    sink = RecordingAuditSink()
    with run_trace_scope(uuid.uuid4().hex):
        _decide(make_pdp(_ALLOW_DOC, sink), sink)
    assert current_run_trace_id() is None


# --- Cedar backend: the SAME two halves, so the alternate PDP cannot drift ---


def test_cedar_decide_inside_a_run_carries_that_runs_exact_trace_id() -> None:
    sink = RecordingAuditSink()
    pdp = CedarGovernedPdp(CedarEvaluator(), sink)
    run_id = uuid.uuid4().hex
    with run_trace_scope(run_id):
        event = _decide(pdp, sink)
    assert event.trace_id == run_id


def test_cedar_decide_outside_any_run_leaves_trace_id_absent() -> None:
    assert current_run_trace_id() is None
    sink = RecordingAuditSink()
    pdp = CedarGovernedPdp(CedarEvaluator(), sink)
    event = _decide(pdp, sink)
    assert event.trace_id is None
