# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""GuardrailEngine C6/C12 coupling: audit-before-raise, fail-closed, redaction."""

from __future__ import annotations

import asyncio
import inspect
from decimal import Decimal
from typing import cast

import pytest

from ne.contracts.governance import GuardrailVerdict
from ne.govern.config import RateLimitRule, RateLimitSettings, SpendCapSettings
from ne.govern.errors import (
    AuditEmissionError,
    GuardrailEvaluationError,
    GuardrailViolation,
)
from ne.govern.guardrails import (
    GuardrailEngine,
    InMemorySpendLedger,
    RateLimitGuardrail,
    SlidingWindowCounter,
    SpendCapGuardrail,
    build_tool_call_payload,
)
from ne.schema import AuditEvent, AuditOutcome

from .conftest import (
    FailingAuditSink,
    RaisingGuardrail,
    RecordingAuditSink,
    StubGuardrail,
    make_principal,
)

_PASS = GuardrailVerdict(passed=True, rule="stub.ok")
_FAIL = GuardrailVerdict(passed=False, rule="stub.blocked", detail="arg 'x' blocked")


def _payload():
    return build_tool_call_payload(principal=make_principal("user:1"), tool="send")


def test_engine_has_no_sink_default() -> None:
    sig = inspect.signature(GuardrailEngine)
    assert sig.parameters["sink"].default is inspect.Parameter.empty
    with pytest.raises(TypeError):
        GuardrailEngine([StubGuardrail(_PASS)])  # type: ignore[call-arg]


def test_block_appends_one_denied_event_before_raising() -> None:
    sink = RecordingAuditSink()
    engine = GuardrailEngine([StubGuardrail(_PASS), StubGuardrail(_FAIL)], sink)
    with pytest.raises(GuardrailViolation) as exc:
        asyncio.run(engine.enforce(_payload()))
    assert [v.rule for v in exc.value.verdicts] == ["stub.blocked"]
    assert len(sink.events) == 1
    event = cast(AuditEvent, sink.events[0])
    assert event.action == "guardrail.block"
    assert event.outcome == AuditOutcome.denied


def test_block_audit_failure_raises_audit_emission_error() -> None:
    engine = GuardrailEngine([StubGuardrail(_FAIL)], FailingAuditSink())
    with pytest.raises(AuditEmissionError):
        asyncio.run(engine.enforce(_payload()))


def test_guardrail_internal_exception_is_audited_then_raised() -> None:
    sink = RecordingAuditSink()
    engine = GuardrailEngine([RaisingGuardrail()], sink)
    with pytest.raises(GuardrailEvaluationError):
        asyncio.run(engine.enforce(_payload()))
    assert len(sink.events) == 1
    assert cast(AuditEvent, sink.events[0]).action == "guardrail.error"
    assert cast(AuditEvent, sink.events[0]).outcome == AuditOutcome.error


def test_guardrail_exception_plus_audit_failure_raises_audit_emission_error() -> None:
    engine = GuardrailEngine([RaisingGuardrail()], FailingAuditSink())
    with pytest.raises(AuditEmissionError):
        asyncio.run(engine.enforce(_payload()))


def test_all_pass_returns_verdicts_records_rate_and_precharges_spend() -> None:
    sink = RecordingAuditSink()
    counter = SlidingWindowCounter()
    ledger = InMemorySpendLedger()
    guardrails = [
        SpendCapGuardrail(SpendCapSettings(default_cap_usd="100"), ledger),
        RateLimitGuardrail(
            RateLimitSettings(default=RateLimitRule(max_calls=5, window_s=60)), counter
        ),
    ]
    engine = GuardrailEngine(guardrails, sink, rate_counter=counter, spend_ledger=ledger)
    payload = build_tool_call_payload(
        principal=make_principal("user:1"), tool="send", estimated_cost_usd="2.50"
    )

    verdicts = asyncio.run(engine.enforce(payload))
    assert all(v.passed for v in verdicts)
    assert not sink.events  # nothing audited on the all-pass path
    assert counter.count("user:1:send", 60) == 1
    assert ledger.recorded_spend("principal:user:1") == Decimal("2.50")


class _BackendFailCounter(SlidingWindowCounter):
    """A rate counter whose reservation write raises, simulating a transient
    Postgres/SQLAlchemy outage on the all-pass reservation path."""

    def record_if_within(self, key: str, window_s: float, max_calls: int) -> bool:
        raise RuntimeError("rate backend down (simulated Postgres outage)")


def test_reservation_backend_failure_is_audited_then_raised() -> None:
    """Codex NE-070H P2: a transient reservation-backend failure on the all-pass
    path must fail closed WITH a guardrail.error audit, not escape unaudited.

    The guardrail's own check() uses a healthy counter and passes; only the
    engine's reservation counter (record_if_within) raises, so this exercises the
    pass-path reservation seam specifically — which runs AFTER enforce()'s
    check-loop try/except and would otherwise let the exception escape unaudited.
    """
    sink = RecordingAuditSink()
    guardrails = [
        RateLimitGuardrail(
            RateLimitSettings(default=RateLimitRule(max_calls=5, window_s=60)),
            SlidingWindowCounter(),
        ),
    ]
    engine = GuardrailEngine(guardrails, sink, rate_counter=_BackendFailCounter())
    with pytest.raises(GuardrailEvaluationError):
        asyncio.run(engine.enforce(_payload()))
    assert len(sink.events) == 1
    assert cast(AuditEvent, sink.events[0]).action == "guardrail.error"
    assert cast(AuditEvent, sink.events[0]).outcome == AuditOutcome.error


def test_audit_detail_carries_no_raw_arg_or_recipient_values() -> None:
    sink = RecordingAuditSink()
    secret_recipient = "exfil-target@attacker.example"
    secret_arg = "TOP-SECRET-PAYLOAD-VALUE"
    fail = GuardrailVerdict(
        passed=False, rule="recipients.not-allowlisted", detail="recipient not allowed"
    )
    engine = GuardrailEngine([StubGuardrail(fail)], sink)
    payload = build_tool_call_payload(
        principal=make_principal("user:1"),
        tool="send",
        args={"body": secret_arg},
        recipients=[secret_recipient],
    )
    with pytest.raises(GuardrailViolation):
        asyncio.run(engine.enforce(payload))

    event = cast(AuditEvent, sink.events[0])
    serialized = event.model_dump_json()
    assert secret_recipient not in serialized
    assert secret_arg not in serialized


# --- NE-070H: atomic reservation-loss deny point (concurrent overshoot closed) ---

from ne.govern.guardrails.spend import SpendLedger as _SpendLedger  # noqa: E402


class _DenyingLedger(_SpendLedger):
    """A ledger whose atomic charge always LOSES (simulates a concurrent worker
    consuming the last of the cap between check() and the reservation)."""

    def recorded_spend(self, scope: str) -> Decimal:
        return Decimal(0)  # check() passes; the reservation is where the cap bites

    def record(self, scope: str, amount: Decimal) -> None:  # pragma: no cover - unused
        raise AssertionError("record must not be called on the reservation path")

    def charge(self, charges) -> bool:  # type: ignore[no-untyped-def]
        return False


class _DenyingCounter(SlidingWindowCounter):
    """A rate counter whose atomic record_if_within always LOSES."""

    def count(self, key: str, window_s: float) -> int:
        return 0  # check() passes; the reservation is where the limit bites

    def record_if_within(self, key: str, window_s: float, max_calls: int) -> bool:
        return False


def _spend_payload():
    return build_tool_call_payload(
        principal=make_principal("user:1"), tool="send", estimated_cost_usd="1.00"
    )


def test_spend_reservation_loss_blocks_with_one_audit() -> None:
    sink = RecordingAuditSink()
    ledger = _DenyingLedger()
    engine = GuardrailEngine(
        [SpendCapGuardrail(SpendCapSettings(default_cap_usd="100"), ledger)],
        sink,
        spend_ledger=ledger,
    )
    with pytest.raises(GuardrailViolation) as exc:
        asyncio.run(engine.enforce(_spend_payload()))
    assert [v.rule for v in exc.value.verdicts] == ["spend.cap-exceeded"]
    assert len(sink.events) == 1
    event = cast(AuditEvent, sink.events[0])
    assert event.action == "guardrail.block"
    assert event.outcome == AuditOutcome.denied
    assert "spend.cap-exceeded" in (event.detail or "")


def test_rate_reservation_loss_blocks_with_one_audit() -> None:
    sink = RecordingAuditSink()
    counter = _DenyingCounter()
    engine = GuardrailEngine(
        [
            RateLimitGuardrail(
                RateLimitSettings(default=RateLimitRule(max_calls=5, window_s=60)), counter
            )
        ],
        sink,
        rate_counter=counter,
    )
    with pytest.raises(GuardrailViolation) as exc:
        asyncio.run(engine.enforce(_payload()))
    assert [v.rule for v in exc.value.verdicts] == ["rate.limit-exceeded"]
    assert len(sink.events) == 1
    event = cast(AuditEvent, sink.events[0])
    assert event.action == "guardrail.block"
    assert "rate.limit-exceeded" in (event.detail or "")


def test_reservation_loss_audit_failure_raises_audit_emission_error() -> None:
    # The new deny point preserves C12 fail-closed: if the block itself can't be
    # audited, AuditEmissionError propagates and no decision escapes un-audited.
    ledger = _DenyingLedger()
    engine = GuardrailEngine(
        [SpendCapGuardrail(SpendCapSettings(default_cap_usd="100"), ledger)],
        FailingAuditSink(),
        spend_ledger=ledger,
    )
    with pytest.raises(AuditEmissionError):
        asyncio.run(engine.enforce(_spend_payload()))
