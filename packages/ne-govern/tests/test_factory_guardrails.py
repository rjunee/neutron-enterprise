# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Factory seams: create_guardrail_engine + create_redactor (the NE-033 wiring)."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Mapping, Sequence
from typing import Any, cast

import pytest

from ne.contracts.governance import GuardrailVerdict, RedactionResult
from ne.contracts.plugins import PluginRegistry
from ne.govern import (
    GovernSettings,
    GuardrailEngine,
    PresidioRedactor,
    RedactionStage,
    RegexRedactor,
    build_tool_call_payload,
    create_guardrail_engine,
    create_redactor,
)
from ne.govern.errors import GuardrailViolation
from ne.govern.guardrails.rate import RateLimitGuardrail, SlidingWindowCounter
from ne.govern.guardrails.recipients import RecipientAllowlistGuardrail
from ne.govern.guardrails.spend import InMemorySpendLedger, SpendCapGuardrail
from ne.govern.guardrails.toolargs import ToolArgGuardrail
from ne.schema import AuditEvent

from .conftest import RecordingAuditSink, make_principal


def test_create_guardrail_engine_requires_sink() -> None:
    sig = inspect.signature(create_guardrail_engine)
    assert sig.parameters["sink"].default is inspect.Parameter.empty
    with pytest.raises(TypeError):
        create_guardrail_engine()  # type: ignore[call-arg]


def test_create_guardrail_engine_assembles_all_four_guardrails() -> None:
    engine = create_guardrail_engine(GovernSettings(), sink=RecordingAuditSink())
    assert isinstance(engine, GuardrailEngine)
    kinds = {type(g) for g in engine._guardrails}
    assert kinds == {
        SpendCapGuardrail,
        RateLimitGuardrail,
        RecipientAllowlistGuardrail,
        ToolArgGuardrail,
    }


def test_create_redactor_default_is_presidio() -> None:
    assert GovernSettings().redactor == "presidio"
    stage = create_redactor(GovernSettings())
    assert isinstance(stage, RedactionStage)
    assert isinstance(stage._backend, PresidioRedactor)


def test_create_redactor_resolves_regex() -> None:
    stage = create_redactor(GovernSettings(redactor="regex"))
    assert isinstance(stage, RedactionStage)
    assert isinstance(stage._backend, RegexRedactor)


class _StubGuardrail:
    """A guardrail returning a fixed verdict (extra_guardrails composition double)."""

    def __init__(self, verdict: GuardrailVerdict) -> None:
        self._verdict = verdict

    def check(self, payload: Mapping[str, Any]) -> GuardrailVerdict:
        return self._verdict


def test_extra_guardrails_appended_after_the_four_base_rails() -> None:
    rail = _StubGuardrail(GuardrailVerdict(passed=True, rule="extra.ok", detail=""))
    engine = create_guardrail_engine(
        GovernSettings(), sink=RecordingAuditSink(), extra_guardrails=(rail,)
    )
    assert [type(g) for g in engine._guardrails[:4]] == [
        SpendCapGuardrail,
        RateLimitGuardrail,
        RecipientAllowlistGuardrail,
        ToolArgGuardrail,
    ]
    assert engine._guardrails[4] is rail


def test_extra_guardrails_default_is_empty() -> None:
    engine = create_guardrail_engine(GovernSettings(), sink=RecordingAuditSink())
    assert [type(g) for g in engine._guardrails] == [
        SpendCapGuardrail,
        RateLimitGuardrail,
        RecipientAllowlistGuardrail,
        ToolArgGuardrail,
    ]


def test_extra_guardrail_block_is_audited_through_the_shared_sink() -> None:
    sink = RecordingAuditSink()
    rail = _StubGuardrail(
        GuardrailVerdict(passed=False, rule="extra.block", detail="injected rail blocked")
    )
    engine = create_guardrail_engine(GovernSettings(), sink=sink, extra_guardrails=(rail,))
    payload = build_tool_call_payload(principal=make_principal(), tool="read.docs.search")

    with pytest.raises(GuardrailViolation):
        asyncio.run(engine.enforce(payload))

    blocks = [e for e in sink.events if getattr(e, "action", "") == "guardrail.block"]
    assert len(blocks) == 1
    assert "extra.block" in (cast(AuditEvent, blocks[0]).detail or "")


def test_injected_ledger_and_counter_are_the_engines_instances() -> None:
    ledger = InMemorySpendLedger()
    counter = SlidingWindowCounter()
    engine = create_guardrail_engine(
        GovernSettings(), sink=RecordingAuditSink(), ledger=ledger, counter=counter
    )
    assert engine._spend_ledger is ledger
    assert engine._rate_counter is counter


def test_programmatic_registry_override_wins() -> None:
    class FakeRedactor:
        def redact(self, text: str, *, categories: Sequence[str] | None = None) -> RedactionResult:
            return RedactionResult(text="fake", redactions=())

    registry = PluginRegistry()
    registry.register("redactor", "presidio", FakeRedactor)
    stage = create_redactor(GovernSettings(), registry=registry)
    assert isinstance(stage, RedactionStage)
    assert isinstance(stage._backend, FakeRedactor)
