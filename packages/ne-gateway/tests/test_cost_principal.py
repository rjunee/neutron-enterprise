# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-COST-PRINCIPAL: CostEvent.actor carries the CALLING PRINCIPAL's identifier
(``request.principal_id``), not the gateway's own service identity, taking
precedence over the service-actor default (ADR-0018).

THE LOAD-BEARING GUARD is ``test_two_principals_each_cost_event_carries_its_own``:
two different principals each drive a completion through the SAME gateway
instance, and each emitted CostEvent's actor equals the id of the principal that
caused IT — a value assertion, order-linked to the causing request, never a
presence/membership check.

Gateway AuditEvents deliberately keep the service actor even when a principal is
present (C10-conservative, pinned by ``test_audit_events_keep_service_actor_...``)
— this unit changes an events/outbox payload VALUE, not the audit hash chain.

The existing bidirectional CONTROL — a principal-less direct call still gets the
service actor — lives (untouched) in ``test_cost_events.py::
test_complete_emits_cost_event_to_outbox`` and must stay green throughout.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import pytest

from ne.contracts import ChatMessage, CompletionChunk, CompletionRequest
from ne.gateway import LiteLLMGateway, TaskRoute
from ne.gateway.errors import ModelCallFailedError
from ne.schema import AuditEvent

from .conftest import (
    PASSTHROUGH_REDACTOR,
    RecordingAuditSink,
    RecordingCostSink,
    make_model,
    make_settings,
)

_MSGS = (ChatMessage(role="user", content="hi"),)


def _request(task: str, *, principal_id: str | None = None) -> CompletionRequest:
    return CompletionRequest(messages=_MSGS, task=task, principal_id=principal_id)


def _find_event(events: Sequence[object], action: str) -> AuditEvent:
    """NE-162 adds a `redact.pre_egress` event ahead of every frontier attempt's
    own ne.gateway.complete/stream event; filter by action instead of indexing
    events[0] (mirrors test_trace_id_data.py's helper)."""
    for event in events:
        if isinstance(event, AuditEvent) and event.action == action:
            return event
    raise AssertionError(f"no AuditEvent with action={action!r} in {events!r}")


async def _drain(gen: object) -> list[CompletionChunk]:
    out: list[CompletionChunk] = []
    async for chunk in gen:  # type: ignore[attr-defined]
        out.append(chunk)
    return out


def _gateway(sink: RecordingCostSink, audit_sink: RecordingAuditSink) -> LiteLLMGateway:
    settings = make_settings(
        [make_model("primary", mock_response="ok")],
        {"draft": TaskRoute(model="primary")},
    )
    return LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=PASSTHROUGH_REDACTOR)


def test_two_principals_each_cost_event_carries_its_own(
    sink: RecordingCostSink, audit_sink: RecordingAuditSink, frozen_config: str
) -> None:
    gw = _gateway(sink, audit_sink)

    asyncio.run(gw.complete(_request("draft", principal_id="user:ada")))
    asyncio.run(gw.complete(_request("draft", principal_id="user:bob")))

    assert len(sink.events) == 2
    assert sink.events[0].actor == "user:ada"
    assert sink.events[1].actor == "user:bob"


def test_principal_less_call_after_principal_call_is_service_attributed(
    sink: RecordingCostSink, audit_sink: RecordingAuditSink, frozen_config: str
) -> None:
    gw = _gateway(sink, audit_sink)

    asyncio.run(gw.complete(_request("draft", principal_id="user:ada")))
    asyncio.run(gw.complete(_request("draft", principal_id=None)))

    assert len(sink.events) == 2
    assert sink.events[0].actor == "user:ada"
    assert sink.events[1].actor == "ne-gateway"


def test_empty_string_principal_id_falls_back_to_service(
    sink: RecordingCostSink, audit_sink: RecordingAuditSink, frozen_config: str
) -> None:
    """ADR-0018's decided divergence from trace_id: an EXPLICIT empty string is
    treated as "no principal" (truthiness), NOT preserved as a value — unlike
    trace_id's explicit-empty-wins contract. An empty actor is not a principal
    identifier and must never be attributed as one."""
    gw = _gateway(sink, audit_sink)

    asyncio.run(gw.complete(_request("draft", principal_id="")))

    assert sink.events[0].actor == "ne-gateway"


def test_stream_cost_event_carries_calling_principal(
    sink: RecordingCostSink, audit_sink: RecordingAuditSink, frozen_config: str
) -> None:
    settings = make_settings(
        [make_model("primary", mock_response="hello world")],
        {"draft": TaskRoute(model="primary")},
    )
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=PASSTHROUGH_REDACTOR)

    asyncio.run(_drain(gw.stream(_request("draft", principal_id="user:bob"))))
    assert len(sink.events) == 1
    assert sink.events[0].actor == "user:bob"

    asyncio.run(_drain(gw.stream(_request("draft", principal_id=None))))
    assert len(sink.events) == 2
    assert sink.events[1].actor == "ne-gateway"


def test_audit_events_keep_service_actor_when_principal_present(
    sink: RecordingCostSink, audit_sink: RecordingAuditSink, frozen_config: str
) -> None:
    """Pins the deliberate C10-conservative non-change: even with a calling
    principal present, the gateway's own ne.gateway.complete AuditEvent (and the
    NE-162 redact.pre_egress event ahead of it) stay attributed to the gateway's
    service identity — they record the gateway's own egress action, not the
    principal's action, and are joinable to the principal via trace_id."""
    gw = _gateway(sink, audit_sink)

    asyncio.run(gw.complete(_request("draft", principal_id="user:ada")))

    assert sink.events[0].actor == "user:ada"
    complete_event = _find_event(audit_sink.events, "ne.gateway.complete")
    assert complete_event.actor == "ne-gateway"
    redact_event = _find_event(audit_sink.events, "redact.pre_egress")
    assert redact_event.actor == "ne-gateway"


def test_stream_audit_event_keeps_service_actor_when_principal_present(
    sink: RecordingCostSink, audit_sink: RecordingAuditSink, frozen_config: str
) -> None:
    """The STREAMING half of the same C10-conservative pin.

    Review found the claim was only half-guarded: the test above covers the two
    emit sites on the complete()-success path, and NO test anywhere asserted
    `.actor` on any `ne.gateway.stream` event. Threading the principal into
    `_finalize_stream`'s AuditEvent therefore passed the entire suite — so a
    later reader "finishing the job" would have started attributing the
    gateway's own egress action to a user, on the tamper-evident chain's actor
    field, with every test green.

    That is the shape a normative claim in prose invites: the rule is stated in
    the README and the `_cost_actor` docstring, and review checks the half the
    test covers. The cost event still takes the principal — that is the point of
    the unit — and only the audit event stays service-attributed.
    """
    settings = make_settings(
        [make_model("primary", mock_response="hello world")],
        {"draft": TaskRoute(model="primary")},
    )
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=PASSTHROUGH_REDACTOR)

    asyncio.run(_drain(gw.stream(_request("draft", principal_id="user:ada"))))

    assert sink.events[0].actor == "user:ada"
    stream_event = _find_event(audit_sink.events, "ne.gateway.stream")
    assert stream_event.actor == "ne-gateway"


def _failing_gateway(sink: RecordingCostSink, audit_sink: RecordingAuditSink) -> LiteLLMGateway:
    settings = make_settings(
        [
            make_model("primary", mock_response=Exception("boom-1")),
            make_model("backup", mock_response=Exception("boom-2")),
        ],
        {"draft": TaskRoute(model="primary", fallbacks=("backup",))},
        num_retries=0,
    )
    return LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=PASSTHROUGH_REDACTOR)


def test_failure_audit_events_keep_service_actor_when_principal_present(
    sink: RecordingCostSink, audit_sink: RecordingAuditSink, frozen_config: str
) -> None:
    """The ERROR paths of the same C10-conservative pin, both transports.

    Found while mutation-checking the streaming guard above: of the six gateway
    AuditEvent emit sites, the two all-candidates-failed sites were the last
    still unguarded, so threading the principal into either passed the suite.

    A failed call emits no CostEvent at all, so there is no principal-attributed
    row to contrast against — which is precisely why the audit actor is the only
    thing left to assert here. An error event attributed to a user would read as
    "this principal did something that failed" when what actually happened is
    that the gateway's own egress attempt failed.
    """
    for label, drive in (
        (
            "ne.gateway.complete",
            lambda gw: asyncio.run(gw.complete(_request("draft", principal_id="user:ada"))),
        ),
        (
            "ne.gateway.stream",
            lambda gw: asyncio.run(_drain(gw.stream(_request("draft", principal_id="user:ada")))),
        ),
    ):
        cost_sink, audits = RecordingCostSink(), RecordingAuditSink()
        gw = _failing_gateway(cost_sink, audits)
        with pytest.raises(ModelCallFailedError):
            drive(gw)

        assert cost_sink.events == [], f"{label}: a failed call must emit no cost event"
        event = _find_event(audits.events, label)
        assert event.actor == "ne-gateway", f"{label} error event must stay service-attributed"
