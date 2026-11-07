# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""GroundingGuardrail (C6/C12 / NE-GA-G2): verified-or-refuse grounding output guard."""

from __future__ import annotations

import asyncio
from typing import cast

import pytest

from ne.govern.config import GovernSettings, GuardrailSettings, ToolArgPolicy
from ne.govern.errors import AuditEmissionError, GuardrailViolation
from ne.govern.factory import create_guardrail_engine
from ne.govern.guardrails import GroundingGuardrail, GuardrailEngine, build_tool_call_payload
from ne.schema import AuditEvent, AuditOutcome

from .conftest import FailingAuditSink, RecordingAuditSink, make_principal


def test_check_gate_ungrounded_fails() -> None:
    verdict = GroundingGuardrail().check({"grounded_gate": True, "grounded": False})
    assert not verdict.passed
    assert verdict.rule == "grounding.ungrounded-claim"
    assert (
        verdict.detail
        == "drafted answer contained content not grounded in this turn's verified data"
    )


def test_check_gate_grounded_passes() -> None:
    verdict = GroundingGuardrail().check({"grounded_gate": True, "grounded": True})
    assert verdict.passed
    assert verdict.rule == "grounding.grounded"


def test_check_empty_payload_passes() -> None:
    verdict = GroundingGuardrail().check({})
    assert verdict.passed
    assert verdict.rule == "grounding.not-applicable"


def test_check_normal_tool_call_payload_passes() -> None:
    payload = build_tool_call_payload(
        principal=make_principal(),
        tool="send_email",
        args={"to": "bob@corp.example", "body": "hi"},
        recipients=["bob@corp.example"],
        egress_urls=[],
        estimated_cost_usd="1.00",
    )
    verdict = GroundingGuardrail().check(payload)
    assert verdict.passed


def test_check_missing_grounded_key_fails() -> None:
    verdict = GroundingGuardrail().check({"grounded_gate": True})
    assert not verdict.passed
    assert verdict.rule == "grounding.ungrounded-claim"


@pytest.mark.parametrize("flag", [False, None, 0, ""])
def test_check_falsy_grounded_gate_passes(flag: object) -> None:
    verdict = GroundingGuardrail().check({"grounded_gate": flag, "grounded": False})
    assert verdict.passed


@pytest.mark.parametrize(
    "payload",
    [
        build_tool_call_payload(principal=make_principal(), tool="send"),
        build_tool_call_payload(
            principal=make_principal(),
            tool="send_email",
            args={"to": "bob@corp.example"},
            recipients=["bob@corp.example"],
            egress_urls=["https://corp.example/report"],
        ),
        build_tool_call_payload(
            principal=make_principal(),
            tool="chat.turn",
            args={"grounded": False, "note": "grounded_gate"},
        ),
    ],
)
def test_check_is_inert_across_existing_payload_shapes(payload: dict[str, object]) -> None:
    verdict = GroundingGuardrail().check(payload)
    assert verdict.passed


def test_engine_blocks_ungrounded_with_one_audit_event() -> None:
    sink = RecordingAuditSink()
    engine = GuardrailEngine([GroundingGuardrail()], sink)
    payload = {
        **build_tool_call_payload(principal=make_principal(), tool="grounding.gate"),
        "grounded_gate": True,
        "grounded": False,
    }
    with pytest.raises(GuardrailViolation) as exc:
        asyncio.run(engine.enforce(payload))
    assert [v.rule for v in exc.value.verdicts] == ["grounding.ungrounded-claim"]
    assert len(sink.events) == 1
    event = cast(AuditEvent, sink.events[0])
    assert event.action == "guardrail.block"
    assert event.outcome == AuditOutcome.denied
    assert "grounding.ungrounded-claim" in (event.detail or "")


def test_engine_passes_grounded_payload_without_audit() -> None:
    sink = RecordingAuditSink()
    engine = GuardrailEngine([GroundingGuardrail()], sink)
    payload = {
        **build_tool_call_payload(principal=make_principal(), tool="grounding.gate"),
        "grounded_gate": True,
        "grounded": True,
    }
    verdicts = asyncio.run(engine.enforce(payload))
    assert all(v.passed for v in verdicts)
    assert sink.events == []


def test_engine_normal_payload_is_inert() -> None:
    sink = RecordingAuditSink()
    engine = GuardrailEngine([GroundingGuardrail()], sink)
    payload = build_tool_call_payload(principal=make_principal(), tool="send")
    verdicts = asyncio.run(engine.enforce(payload))
    assert all(v.passed for v in verdicts)
    assert sink.events == []


def test_engine_block_audit_failure_raises_audit_emission_error() -> None:
    engine = GuardrailEngine([GroundingGuardrail()], FailingAuditSink())
    payload = {
        **build_tool_call_payload(principal=make_principal(), tool="grounding.gate"),
        "grounded_gate": True,
        "grounded": False,
    }
    with pytest.raises(AuditEmissionError):
        asyncio.run(engine.enforce(payload))


def test_block_audit_detail_carries_no_payload_values() -> None:
    sink = RecordingAuditSink()
    engine = GuardrailEngine([GroundingGuardrail()], sink)
    payload = {
        **build_tool_call_payload(
            principal=make_principal(), tool="grounding.gate", args={"query": "SECRET-QUERY-TEXT"}
        ),
        "grounded_gate": True,
        "grounded": False,
        "reasons": ("numeral-ungrounded",),
        "finding_count": 3,
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
    engine = create_guardrail_engine(settings, sink=sink, extra_guardrails=(GroundingGuardrail(),))
    payload = build_tool_call_payload(principal=make_principal(), tool="read.docs.search")
    verdicts = asyncio.run(engine.enforce(payload))
    assert len(verdicts) == 5
    assert all(v.passed for v in verdicts)
    assert sink.events == []
