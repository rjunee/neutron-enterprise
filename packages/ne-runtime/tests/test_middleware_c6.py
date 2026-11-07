# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""THE C6 CRUX — every tool call passes guardrails + the PDP + HITL and audits.

Each test drives one path through :class:`GovernedToolExecutor` and asserts BOTH
the behavioural outcome (handler ran / did not run, error raised) AND the audit
trail (the exact events, fail-closed on audit failure, no raw values leaked).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from typing import Any

import pytest

from ne.contracts.governance import ApprovalOutcome, ApprovalStatus, GuardrailVerdict
from ne.govern.errors import (
    AuditEmissionError,
    GuardrailEvaluationError,
    GuardrailViolation,
)
from ne.govern.guardrails.engine import GuardrailEngine
from ne.govern.guardrails.rate import SlidingWindowCounter
from ne.runtime.errors import (
    ApprovalIntegrityError,
    ApprovalNotGrantedError,
    RuntimeAuditError,
    ToolDeniedError,
    ToolExecutionError,
    ToolNotRegisteredError,
)
from ne.runtime.middleware import GovernedToolExecutor
from ne.schema import AuditOutcome

from .conftest import (
    ALLOWED_RECIPIENT,
    AppendCountFailingSink,
    AutoApprovingGate,
    FailingAuditSink,
    NamedApproverGate,
    RecordingAuditSink,
    ScriptedApprovalGate,
    ToolLog,
    UnusedGate,
    make_executor,
    make_guardrails,
    make_pdp,
    make_principal,
    standard_bindings,
)


def _outcomes(sink: RecordingAuditSink) -> list[tuple[str, str]]:
    return [(getattr(e, "action", ""), str(e.outcome)) for e in sink.events]


# --- allow path -----------------------------------------------------------


def test_allow_path_executes_and_audits() -> None:
    log = ToolLog()
    sink = RecordingAuditSink()
    counter = SlidingWindowCounter()
    executor = make_executor(
        standard_bindings(log),
        sink,
        approval_gate=UnusedGate(),
        guardrails=make_guardrails(sink, counter=counter),
    )
    principal = make_principal()

    result = asyncio.run(
        executor.execute("read.docs.search", {"q": "quarterly"}, principal=principal)
    )

    assert result == "docs: 3 results"
    assert log.names() == ["read.docs.search"]
    assert _outcomes(sink) == [
        ("pdp.decide", AuditOutcome.success.value),
        ("tool.execute", AuditOutcome.success.value),
    ]
    # Guardrails ran: the rate counter recorded exactly one call on the all-pass path.
    assert counter.count("user:ada:read.docs.search", 3600.0) == 1


# --- deny path ------------------------------------------------------------


def test_deny_blocks_and_audits() -> None:
    log = ToolLog()
    sink = RecordingAuditSink()
    executor = make_executor(
        standard_bindings(log),
        sink,
        approval_gate=UnusedGate(),
        pdp_overrides={
            "read.docs.search": {
                "effect": "deny",
                "tier": 0,
                "reason": "explicitly denied",
                "obligations": {},
                "policy_version": "1.0.0",
            }
        },
    )

    with pytest.raises(ToolDeniedError):
        asyncio.run(executor.execute("read.docs.search", {"q": "x"}, principal=make_principal()))

    assert log.names() == []
    assert _outcomes(sink) == [("pdp.decide", AuditOutcome.denied.value)]


# --- guardrail block (runs FIRST, before the PDP) -------------------------


def test_guardrail_block_blocks_and_audits() -> None:
    log = ToolLog()
    sink = RecordingAuditSink()
    executor = make_executor(standard_bindings(log), sink, approval_gate=NamedApproverGate())

    with pytest.raises(GuardrailViolation):
        asyncio.run(
            executor.execute(
                "write.external.email.send",
                {"to": "attacker@evil.example", "body": "exfil"},
                principal=make_principal(),
            )
        )

    assert log.names() == []
    # Guardrails run FIRST: a guardrail.block event, and NO pdp.decide event.
    assert _outcomes(sink) == [("guardrail.block", AuditOutcome.denied.value)]


def test_guardrail_error_fails_closed() -> None:
    log = ToolLog()
    sink = RecordingAuditSink()

    class RaisingGuardrail:
        def check(self, payload: Mapping[str, Any]) -> GuardrailVerdict:
            raise RuntimeError("guardrail boom")

    guardrails = GuardrailEngine([RaisingGuardrail()], sink)
    executor = GovernedToolExecutor(
        standard_bindings(log),
        pdp=make_pdp(sink),
        guardrails=guardrails,
        approval_gate=UnusedGate(),
        sink=sink,
    )

    with pytest.raises(GuardrailEvaluationError):
        asyncio.run(executor.execute("read.docs.search", {"q": "x"}, principal=make_principal()))

    assert log.names() == []
    assert _outcomes(sink) == [("guardrail.error", AuditOutcome.error.value)]


# --- require-approval (HITL) ----------------------------------------------


def test_tier2_requires_approval_then_executes() -> None:
    log = ToolLog()
    sink = RecordingAuditSink()
    gate = NamedApproverGate("alice")
    executor = make_executor(standard_bindings(log), sink, approval_gate=gate)

    result = asyncio.run(
        executor.execute(
            "write.external.email.send",
            {"to": ALLOWED_RECIPIENT, "body": "hi"},
            principal=make_principal(),
        )
    )

    assert result == "sent"
    assert log.names() == ["write.external.email.send"]
    assert _outcomes(sink) == [
        ("pdp.decide", AuditOutcome.success.value),
        ("tool.approval", AuditOutcome.success.value),
        ("tool.execute", AuditOutcome.success.value),
    ]
    assert gate.requests[0].tier == 2


@pytest.mark.parametrize(
    "status",
    [ApprovalStatus.REJECTED, ApprovalStatus.EXPIRED, ApprovalStatus.ESCALATED],
)
def test_tier2_not_approved_does_not_execute(status: ApprovalStatus) -> None:
    log = ToolLog()
    sink = RecordingAuditSink()
    gate = ScriptedApprovalGate([ApprovalOutcome(status=status)])
    executor = make_executor(standard_bindings(log), sink, approval_gate=gate)

    with pytest.raises(ApprovalNotGrantedError):
        asyncio.run(
            executor.execute(
                "write.external.email.send",
                {"to": ALLOWED_RECIPIENT, "body": "hi"},
                principal=make_principal(),
            )
        )

    assert log.names() == []
    assert _outcomes(sink) == [
        ("pdp.decide", AuditOutcome.success.value),
        ("tool.approval", AuditOutcome.denied.value),
    ]


@pytest.mark.parametrize("tool", ["write.external.email.send", "finance.payment.initiate"])
def test_never_auto_approves_without_named_approver(tool: str) -> None:
    log = ToolLog()
    sink = RecordingAuditSink()
    # APPROVED but no named approver — must be treated as an integrity violation.
    executor = make_executor(standard_bindings(log), sink, approval_gate=AutoApprovingGate())
    args = (
        {"to": ALLOWED_RECIPIENT, "body": "hi"}
        if tool.startswith("write.external")
        else {"amount": 10}
    )

    with pytest.raises(ApprovalIntegrityError):
        asyncio.run(executor.execute(tool, args, principal=make_principal()))

    assert log.names() == []
    assert ("tool.approval", AuditOutcome.denied.value) in _outcomes(sink)
    assert ("tool.execute", AuditOutcome.success.value) not in _outcomes(sink)


# --- handler exception ----------------------------------------------------


def test_handler_exception_audited() -> None:
    log = ToolLog()
    sink = RecordingAuditSink()
    bindings = standard_bindings(log)
    # Replace the read handler with a raising one.
    from ne.runtime.tools import ToolBinding

    bindings[0] = ToolBinding(
        spec=bindings[0].spec,
        handler=log.raising_handler("read.docs.search", ValueError("kaboom")),
    )
    executor = make_executor(bindings, sink, approval_gate=UnusedGate())

    with pytest.raises(ToolExecutionError):
        asyncio.run(executor.execute("read.docs.search", {"q": "x"}, principal=make_principal()))

    error_events = [e for e in sink.events if getattr(e, "action", "") == "tool.execute"]
    assert len(error_events) == 1
    assert error_events[0].outcome == AuditOutcome.error.value
    detail = json.loads(error_events[0].detail)
    # error_class keeps its exact former meaning and stays first; the raise site
    # is the additive field that makes the row a diagnosis instead of a shrug.
    assert next(iter(detail)) == "error_class"
    assert detail["error_class"] == "ValueError"
    site = detail["error_site"]
    assert "conftest:ToolLog.raising_handler.<locals>._run:" in site
    assert site.rsplit(":", 1)[1].isdigit()


# --- the failure states its reason (NE tool.execute diagnostic hole) -------
#
# The bug these four tests close: a failed `tool.execute` recorded
# `{"error_class": "LookupError"}` and the containers logged nothing at all, so
# a LookupError on read.knowledge.search named none of the dozen call sites that
# can raise one. They are written as PAIRS — the loud half and the quiet half of
# the same rule — because either one alone can be satisfied by a wrong change.


def _raising_executor(exc: Exception, sink: RecordingAuditSink) -> tuple[Any, ToolLog]:
    from ne.runtime.tools import ToolBinding

    log = ToolLog()
    bindings = standard_bindings(log)
    bindings[0] = ToolBinding(
        spec=bindings[0].spec, handler=log.raising_handler("read.docs.search", exc)
    )
    return make_executor(bindings, sink, approval_gate=UnusedGate()), log


def _execute_detail(sink: RecordingAuditSink) -> dict[str, Any]:
    events = [e for e in sink.events if getattr(e, "action", "") == "tool.execute"]
    assert len(events) == 1
    return dict(json.loads(events[0].detail))


def test_failed_execution_records_the_raise_site_that_the_class_alone_hides() -> None:
    """MUTATION A (loud): two identical classes from different lines are distinguishable.

    This is the whole defect. ``LookupError`` is raised from ~12 places reachable
    from one governed tool; the class is the same string every time and the line
    number is not.
    """
    sink_a = RecordingAuditSink()
    sink_b = RecordingAuditSink()

    def _first() -> Exception:
        try:
            raise LookupError("evidence context unavailable")
        except LookupError as exc:
            return exc

    def _second() -> Exception:
        try:
            raise LookupError("evidence context unavailable")
        except LookupError as exc:
            return exc

    executor_a, _ = _raising_executor(_first(), sink_a)
    executor_b, _ = _raising_executor(_second(), sink_b)
    for executor in (executor_a, executor_b):
        with pytest.raises(ToolExecutionError):
            asyncio.run(
                executor.execute("read.docs.search", {"q": "x"}, principal=make_principal())
            )

    detail_a = _execute_detail(sink_a)
    detail_b = _execute_detail(sink_b)
    assert detail_a["error_class"] == detail_b["error_class"] == "LookupError"
    # Same class, same message, DIFFERENT recorded site — the whole point.
    assert detail_a["error_site"] != detail_b["error_site"]
    assert "._first:" in detail_a["error_site"]
    assert "._second:" in detail_b["error_site"]


def test_successful_execution_detail_records_nothing_new() -> None:
    """MUTATION A (quiet): the success path gains no field. Only failure got louder."""
    log = ToolLog()
    sink = RecordingAuditSink()
    executor = make_executor(standard_bindings(log), sink, approval_gate=UnusedGate())

    asyncio.run(executor.execute("read.docs.search", {"q": "x"}, principal=make_principal()))

    events = [e for e in sink.events if getattr(e, "action", "") == "tool.execute"]
    assert len(events) == 1
    assert events[0].outcome == AuditOutcome.success.value
    # The standard read binding declares no audit-detail extractor, so a success
    # carries NO detail at all — exactly as before. No error_site, no error_class.
    assert events[0].detail is None


def test_failure_reason_never_carries_an_argument_value(caplog: Any) -> None:
    """MUTATION B (quiet): the ledger records the reason WITHOUT the payload.

    The governed ``read.trace`` tool really does raise ``f"trace not found:
    {trace_id}"`` — an exception message here interpolates request data, so the
    message may never enter the durable row. Neither may it reach the operator
    log unredacted when it looks like a credential.
    """
    secret = "hunter2-corpus-value"
    sink = RecordingAuditSink()
    executor, _ = _raising_executor(ValueError(f"no match for password={secret}"), sink)

    with (
        caplog.at_level(logging.ERROR, logger="ne.runtime.middleware"),
        pytest.raises(ToolExecutionError),
    ):
        asyncio.run(executor.execute("read.docs.search", {"q": secret}, principal=make_principal()))

    events = [e for e in sink.events if getattr(e, "action", "") == "tool.execute"]
    assert secret not in str(events[0].detail)
    assert secret not in json.dumps(_execute_detail(sink))
    # ...and the redactor still fires on the operator's side of the pair.
    assert secret not in caplog.text
    assert "[redacted]" in caplog.text


def test_failure_is_still_loud_on_the_operator_channel(caplog: Any) -> None:
    """MUTATION B (loud): the message survives — on the log — and is not suppressed."""
    sink = RecordingAuditSink()
    executor, _ = _raising_executor(LookupError("knowledge source evidence unavailable"), sink)

    with (
        caplog.at_level(logging.ERROR, logger="ne.runtime.middleware"),
        pytest.raises(ToolExecutionError),
    ):
        asyncio.run(executor.execute("read.docs.search", {"q": "x"}, principal=make_principal()))

    assert "knowledge source evidence unavailable" in caplog.text
    assert "read.docs.search" in caplog.text
    # The log is an ADDITION, never a substitution: the ledger row is still there
    # and the caller still sees the failure. Quieting the error was never on.
    assert _execute_detail(sink)["error_class"] == "LookupError"


# --- unknown tool ---------------------------------------------------------


def test_unknown_tool_audited_and_refused() -> None:
    log = ToolLog()
    sink = RecordingAuditSink()
    executor = make_executor(standard_bindings(log), sink, approval_gate=UnusedGate())

    with pytest.raises(ToolNotRegisteredError):
        asyncio.run(executor.execute("nope.unknown", {}, principal=make_principal()))

    assert log.names() == []
    # Full governance record, in order: the unknown-tool audit, then the guardrail
    # block (the unknown tool is not in the arg-policy allowlist), then a PDP DENY
    # (unclassified action) — before the terminal ToolNotRegisteredError.
    assert _outcomes(sink) == [
        ("tool.unknown", AuditOutcome.denied.value),
        ("guardrail.block", AuditOutcome.denied.value),
        ("pdp.decide", AuditOutcome.denied.value),
    ]
    block = next(e for e in sink.events if getattr(e, "action", "") == "guardrail.block")
    assert "toolargs.unknown-tool" in json.loads(block.detail)["rules"]
    pdp = next(e for e in sink.events if getattr(e, "action", "") == "pdp.decide")
    assert pdp.resource == "tool:nope.unknown"


def test_unknown_tool_audit_failure_fails_closed() -> None:
    log = ToolLog()
    sink = AppendCountFailingSink(fail_on_action="tool.unknown")
    executor = make_executor(standard_bindings(log), sink, approval_gate=UnusedGate())

    with pytest.raises(RuntimeAuditError):
        asyncio.run(executor.execute("nope.unknown", {}, principal=make_principal()))

    assert log.names() == []
    # The failed first append aborts the branch before guardrails/PDP run: no
    # pdp.decide event is ever appended (nothing escapes the un-recordable call).
    assert all(getattr(e, "action", "") != "pdp.decide" for e in sink.events)


# --- audit-append failure withholds the result (fail closed) --------------


def test_audit_sink_failure_on_execute_withholds_result() -> None:
    log = ToolLog()
    sink = AppendCountFailingSink(fail_on_action="tool.execute")
    executor = make_executor(standard_bindings(log), sink, approval_gate=UnusedGate())

    with pytest.raises(RuntimeAuditError):
        asyncio.run(executor.execute("read.docs.search", {"q": "x"}, principal=make_principal()))


def test_pdp_append_failure_fails_closed() -> None:
    log = ToolLog()
    sink = FailingAuditSink()
    executor = make_executor(standard_bindings(log), sink, approval_gate=UnusedGate())

    # On the allow path guardrails all-pass (no append), then the PDP's mandatory
    # audit append fails first => AuditEmissionError (C6 fail closed).
    with pytest.raises(AuditEmissionError):
        asyncio.run(executor.execute("read.docs.search", {"q": "x"}, principal=make_principal()))
    assert log.names() == []


# --- redaction discipline -------------------------------------------------


def test_no_raw_values_in_audit_detail() -> None:
    log = ToolLog()
    sink = RecordingAuditSink()
    gate = NamedApproverGate("alice")
    executor = make_executor(standard_bindings(log), sink, approval_gate=gate)
    sentinel = "SUPERSECRETVALUE"

    asyncio.run(
        executor.execute(
            "write.external.email.send",
            {"to": ALLOWED_RECIPIENT, "body": sentinel},
            principal=make_principal(),
        )
    )

    for event in sink.events:
        blob = json.dumps(
            {"detail": getattr(event, "detail", None), "provenance": dict(event.provenance)}
        )
        assert sentinel not in blob
        assert ALLOWED_RECIPIENT not in blob
