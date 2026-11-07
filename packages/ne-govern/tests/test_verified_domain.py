# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""VerifiedDomainGuardrail (C12 / NE-VAF-U2): verified-or-refuse output guard."""

from __future__ import annotations

import asyncio
from typing import cast

import pytest

from ne.govern.config import GovernSettings, GuardrailSettings, ToolArgPolicy
from ne.govern.errors import AuditEmissionError, GuardrailViolation
from ne.govern.factory import create_guardrail_engine
from ne.govern.guardrails import GuardrailEngine, VerifiedDomainGuardrail, build_tool_call_payload
from ne.schema import AuditEvent, AuditOutcome

from .conftest import FailingAuditSink, RecordingAuditSink, make_principal


def test_check_verified_domain_unrouted_fails() -> None:
    verdict = VerifiedDomainGuardrail().check({"verified_domain": True, "routed": False})
    assert not verdict.passed
    assert verdict.rule == "verified.unrouted"
    assert verdict.detail == "query in a verified domain did not resolve to a verified template"


def test_check_verified_domain_routed_passes() -> None:
    verdict = VerifiedDomainGuardrail().check({"verified_domain": True, "routed": True})
    assert verdict.passed
    assert verdict.rule == "verified.routed"


def test_check_empty_payload_passes() -> None:
    verdict = VerifiedDomainGuardrail().check({})
    assert verdict.passed
    assert verdict.rule == "verified.not-applicable"


def test_check_normal_tool_call_payload_passes() -> None:
    payload = build_tool_call_payload(
        principal=make_principal(),
        tool="send_email",
        args={"to": "bob@corp.example", "body": "hi"},
        recipients=["bob@corp.example"],
        egress_urls=[],
        estimated_cost_usd="1.00",
    )
    verdict = VerifiedDomainGuardrail().check(payload)
    assert verdict.passed


def test_check_missing_routed_key_fails() -> None:
    verdict = VerifiedDomainGuardrail().check({"verified_domain": True})
    assert not verdict.passed
    assert verdict.rule == "verified.unrouted"


@pytest.mark.parametrize("flag", [False, None, 0, ""])
def test_check_falsy_verified_domain_passes(flag: object) -> None:
    verdict = VerifiedDomainGuardrail().check({"verified_domain": flag, "routed": False})
    assert verdict.passed


def test_engine_blocks_unrouted_with_one_audit_event() -> None:
    sink = RecordingAuditSink()
    engine = GuardrailEngine([VerifiedDomainGuardrail()], sink)
    payload = {
        **build_tool_call_payload(principal=make_principal(), tool="chat.turn"),
        "verified_domain": True,
        "routed": False,
    }
    with pytest.raises(GuardrailViolation) as exc:
        asyncio.run(engine.enforce(payload))
    assert [v.rule for v in exc.value.verdicts] == ["verified.unrouted"]
    assert len(sink.events) == 1
    event = cast(AuditEvent, sink.events[0])
    assert event.action == "guardrail.block"
    assert event.outcome == AuditOutcome.denied
    assert "verified.unrouted" in (event.detail or "")


def test_engine_passes_routed_payload_without_audit() -> None:
    sink = RecordingAuditSink()
    engine = GuardrailEngine([VerifiedDomainGuardrail()], sink)
    payload = {
        **build_tool_call_payload(principal=make_principal(), tool="chat.turn"),
        "verified_domain": True,
        "routed": True,
    }
    verdicts = asyncio.run(engine.enforce(payload))
    assert all(v.passed for v in verdicts)
    assert sink.events == []


def test_engine_normal_payload_is_inert() -> None:
    sink = RecordingAuditSink()
    engine = GuardrailEngine([VerifiedDomainGuardrail()], sink)
    payload = build_tool_call_payload(principal=make_principal(), tool="send")
    verdicts = asyncio.run(engine.enforce(payload))
    assert all(v.passed for v in verdicts)
    assert sink.events == []


def test_engine_block_audit_failure_raises_audit_emission_error() -> None:
    engine = GuardrailEngine([VerifiedDomainGuardrail()], FailingAuditSink())
    payload = {
        **build_tool_call_payload(principal=make_principal(), tool="chat.turn"),
        "verified_domain": True,
        "routed": False,
    }
    with pytest.raises(AuditEmissionError):
        asyncio.run(engine.enforce(payload))


def test_block_audit_detail_carries_no_payload_values() -> None:
    sink = RecordingAuditSink()
    engine = GuardrailEngine([VerifiedDomainGuardrail()], sink)
    payload = {
        **build_tool_call_payload(
            principal=make_principal(), tool="chat.turn", args={"query": "SECRET-QUERY-TEXT"}
        ),
        "verified_domain": True,
        "routed": False,
    }
    with pytest.raises(GuardrailViolation):
        asyncio.run(engine.enforce(payload))
    event = cast(AuditEvent, sink.events[0])
    assert "SECRET-QUERY-TEXT" not in event.model_dump_json()


def test_registered_in_shared_engine_is_inert_for_normal_traffic() -> None:
    sink = RecordingAuditSink()
    settings = GovernSettings(
        guardrails=GuardrailSettings(tools={"read.docs.search": ToolArgPolicy()})
    )
    engine = create_guardrail_engine(
        settings, sink=sink, extra_guardrails=(VerifiedDomainGuardrail(),)
    )
    payload = build_tool_call_payload(principal=make_principal(), tool="read.docs.search")
    verdicts = asyncio.run(engine.enforce(payload))
    assert len(verdicts) == 5
    assert all(v.passed for v in verdicts)
    assert sink.events == []
