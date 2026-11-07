# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-ACTION-VALIDATE-BEFORE-GATE: a call that cannot execute never consumes a human
decision.

The live defect (2026-08-03, demo.example.host): typed action-input validation ran
INSIDE ``build_action_binding``'s post-governance handler — after guardrails, after
the PDP, and after the human approval gate. A call missing a required argument
therefore reached the approvals queue, consumed a human decision, and only THEN
raised ``ActionValidationError``.

This module drives the REAL ``DurableApprovalGate`` over a real SQLite-backed
``SqlAlchemyApprovalStore`` (mirroring ``test_middleware_with_durable_gate.py``'s
``_wire``) so the load-bearing assertion is not "an error was raised" (that passes on
the broken code too — it also raises, just far too late) but WHERE: zero rows in
``ne_approval_requests`` for the refused call, no ``approval.requested`` audit event,
and no human decision consumed.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, cast

import pytest
import sqlalchemy as sa
from pydantic import Field

from ne.contracts.integration import ActionResult
from ne.contracts.runtime import ToolSpec
from ne.contracts.types import ApprovalTier
from ne.govern import create_guardrail_engine
from ne.govern.config import (
    ArgSpec,
    GovernSettings,
    GuardrailSettings,
    RateLimitRule,
    RateLimitSettings,
    ToolArgKind,
    ToolArgPolicy,
)
from ne.runtime.action_tools import (
    ActionDedupStore,
    ActionDefinition,
    ActionInput,
    build_action_binding,
    ensure_action_dedup_schema,
)
from ne.runtime.approval_store import (
    SqlAlchemyApprovalStore,
    ensure_approval_schema,
    ne_approval_requests,
)
from ne.runtime.approvals import DurableApprovalGate
from ne.runtime.correlation import action_scope
from ne.runtime.errors import ActionValidationError, RuntimeAuditError
from ne.runtime.middleware import GovernedToolExecutor
from ne.runtime.runner import build_governed_tool
from ne.runtime.tools import ToolBinding

from .conftest import (
    AppendCountFailingSink,
    RecordingAuditSink,
    ToolLog,
    approval_settings,
    make_pdp,
    make_principal,
)

_TOOL = "write.internal.crm.note"
_RECORD_ID = "001000000000001AAA"


class _NoteInput(ActionInput):
    record_id: str = Field(pattern=r"^[a-zA-Z0-9]{15,18}$")
    body: str = Field(min_length=1, max_length=100)


class _RecordingPerform:
    """A stand-in ``perform`` recording each call (never touches a real connector)."""

    def __init__(self, source_ref: str = "REF-1") -> None:
        self.calls: list[_NoteInput] = []
        self._source_ref = source_ref

    async def __call__(self, i: _NoteInput) -> ActionResult:
        self.calls.append(i)
        return ActionResult(succeeded=True, detail="created", source_ref=self._source_ref)


def _spec() -> ToolSpec:
    return ToolSpec(name=_TOOL, description="Note.", tier=ApprovalTier.TIER_2, input_schema={})


def _definition(perform: Any) -> ActionDefinition:
    return ActionDefinition(
        spec=_spec(), source_system="salesforce", input_model=_NoteInput, perform=perform
    )


def _dedup() -> ActionDedupStore:
    engine = sa.create_engine("sqlite://")
    ensure_action_dedup_schema(engine)
    return ActionDedupStore(engine)


def _guardrails(sink: Any, *, extra_key: bool = False) -> Any:
    args = {
        "request_id": ArgSpec(kind=ToolArgKind.text),
        "record_id": ArgSpec(kind=ToolArgKind.text),
        "body": ArgSpec(kind=ToolArgKind.text, max_length=100),
    }
    if extra_key:
        args["extra"] = ArgSpec(kind=ToolArgKind.text)
    return create_guardrail_engine(
        GovernSettings(
            guardrails=GuardrailSettings(
                rate=RateLimitSettings(default=RateLimitRule(max_calls=10000, window_s=3600.0)),
                tools={_TOOL: ToolArgPolicy(args=args)},
            )
        ),
        sink=sink,
    )


_Wired = tuple[
    sa.Engine, SqlAlchemyApprovalStore, DurableApprovalGate, _RecordingPerform, GovernedToolExecutor
]


def _wire(sink: Any, *, extra_key: bool = False) -> _Wired:
    """Compose the REAL C6 seam: guardrails + PDP + a real durable gate over sqlite,
    one governed CRM-note action binding — mirrors
    ``test_middleware_with_durable_gate.py``'s ``_wire``."""
    engine = sa.create_engine("sqlite://")
    ensure_approval_schema(engine)
    store = SqlAlchemyApprovalStore(engine)
    gate = DurableApprovalGate(
        store=store, sink=sink, settings=approval_settings(poll_interval_s=0.0)
    )
    perform = _RecordingPerform()
    binding = build_action_binding(_definition(perform), dedup=_dedup())
    executor = GovernedToolExecutor(
        [binding],
        pdp=make_pdp(sink),
        guardrails=_guardrails(sink, extra_key=extra_key),
        approval_gate=gate,
        sink=sink,
    )
    return engine, store, gate, perform, executor


def _wire_raw_binding(
    sink: Any, binding: ToolBinding
) -> tuple[sa.Engine, SqlAlchemyApprovalStore, DurableApprovalGate, GovernedToolExecutor]:
    """Like :func:`_wire` but over a caller-supplied raw binding (for the
    validator-crash test, which is not an ``ActionDefinition`` at all)."""
    engine = sa.create_engine("sqlite://")
    ensure_approval_schema(engine)
    store = SqlAlchemyApprovalStore(engine)
    gate = DurableApprovalGate(
        store=store, sink=sink, settings=approval_settings(poll_interval_s=0.0)
    )
    executor = GovernedToolExecutor(
        [binding],
        pdp=make_pdp(sink),
        guardrails=_guardrails(sink),
        approval_gate=gate,
        sink=sink,
    )
    return engine, store, gate, executor


def _approval_rows(engine: sa.Engine) -> list[sa.Row[Any]]:
    """ALL rows regardless of status — an approved row leaves ``list_pending`` but
    still proves an approval was consumed; the guard must see zero rows, period."""
    with engine.connect() as conn:
        return list(conn.execute(sa.select(ne_approval_requests)))


def _in_order(haystack: list[str], needles: list[str]) -> bool:
    it = iter(haystack)
    return all(n in it for n in needles)


async def _wait_for_pending(store: SqlAlchemyApprovalStore, *, timeout_s: float = 5.0) -> None:
    """Bounded poll for a pending approval row — raises AssertionError on timeout
    instead of spinning forever (Argus round-1 P2: an unbounded ``while not await
    store.list_pending()`` loop hangs the whole file under the refuse-everything
    mutant, since no row is ever created to poll for). Mirrors the bounded-retry
    shape at ``packages/ne-mcp/tests/test_demo_server_composition.py``'s
    ``approver()`` helper."""
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        if await store.list_pending():
            return
        await asyncio.sleep(0)
    raise AssertionError("no pending approval row appeared within the timeout")


# --- THE GUARD ---------------------------------------------------------------------


def test_missing_required_field_refused_before_any_approval_row() -> None:
    """THE GUARD: a call missing ``record_id`` is refused BEFORE any approval row
    exists. Under the moved-back-post-gate mutation, the call instead PARKS on the
    real gate and ``asyncio.wait_for`` times out (wrong exception) while a pending
    row exists — doubly RED, and this test terminates deterministically either way."""
    sink = RecordingAuditSink()
    engine, _store, _gate, perform, executor = _wire(sink)
    principal = make_principal()
    args = {"request_id": "req-12345678", "body": "hi"}  # record_id missing

    async def scenario() -> None:
        await asyncio.wait_for(executor.execute(_TOOL, args, principal=principal), timeout=5)

    with pytest.raises(ActionValidationError) as excinfo:
        asyncio.run(scenario())
    assert "record_id" in str(excinfo.value)

    assert _approval_rows(engine) == []
    assert "approval.requested" not in sink.actions()
    validated = [e for e in sink.events if getattr(e, "action", "") == "tool.validate"]
    assert len(validated) == 1
    assert validated[0].outcome == "denied"
    assert validated[0].resource == f"tool:{_TOOL}"
    assert perform.calls == []
    assert "tool.execute" not in sink.actions()

    # C12 ordering (Argus round-1 P2): guardrails + the PDP still run BEFORE the
    # pre-gate validation refusal — the new step must not reorder or skip the
    # existing audit spine. A hoisted-earlier validate_args (e.g. ahead of
    # guardrails.enforce) would collapse this spine to just ['tool.validate'] and
    # this assertion catches it (verified: passes on the shipped tree, fails when
    # validate_args is moved ahead of guardrails/PDP in middleware.py).
    actions = sink.actions()
    assert "pdp.decide" in actions
    assert actions.index("pdp.decide") < actions.index("tool.validate")


# --- THE CONTROL: valid path is byte-identical ---------------------------------------


def test_valid_call_reaches_gate_and_executes_on_approval() -> None:
    """THE CONTROL: a well-formed call is UNCHANGED — reaches the real gate, creates
    exactly one approval row, and executes exactly once on named approval."""
    sink = RecordingAuditSink()
    engine, store, gate, perform, executor = _wire(sink)
    principal = make_principal()
    args = {"request_id": "req-12345678", "record_id": _RECORD_ID, "body": "hi"}

    async def scenario() -> str:
        task = asyncio.create_task(executor.execute(_TOOL, dict(args), principal=principal))
        await _wait_for_pending(store)
        assert perform.calls == []  # nothing executed while pending
        pending = await store.list_pending()
        await gate.approve(pending[0].request_id, approver="carol")
        return await asyncio.wait_for(task, timeout=10)

    result = asyncio.run(scenario())
    parsed = ActionResult.model_validate_json(result)
    assert parsed.detail == "created"

    rows = _approval_rows(engine)
    assert len(rows) == 1
    assert rows[0].action == _TOOL

    actions = sink.actions()
    assert _in_order(actions, ["approval.requested", "tool.approval", "tool.execute"])
    # No new event on the valid path: tool.validate fires ONLY on refusal.
    assert "tool.validate" not in actions
    assert len(perform.calls) == 1


# --- undeclared key + missing identity: two more pre-gate refusal legs -------------


def test_undeclared_key_refused_before_gate() -> None:
    """``extra='forbid'`` sibling: guardrails allowlist ``extra`` (proving the
    ToolArgPolicy name-only-allowlist gap the defect exploited), so the refusal must
    come from the typed model, pre-gate."""
    sink = RecordingAuditSink()
    engine, _store, _gate, perform, executor = _wire(sink, extra_key=True)
    principal = make_principal()
    args = {"request_id": "req-12345678", "record_id": _RECORD_ID, "body": "hi", "extra": "y"}

    async def scenario() -> None:
        await asyncio.wait_for(executor.execute(_TOOL, args, principal=principal), timeout=5)

    with pytest.raises(ActionValidationError) as excinfo:
        asyncio.run(scenario())
    message = str(excinfo.value)
    # C12: an UNDECLARED key is caller/model text, not schema — pydantic reports the
    # SUPPLIED key in ``loc``, and this message is rendered straight back into model
    # context (``runner._BLOCKING_ERRORS`` → ``BLOCKED: {msg}``) and to MCP clients.
    # The refusal states a COUNT; it never quotes the key. (This assertion previously
    # read ``"extra" in message`` — it pinned the echo this discipline forbids.)
    assert "undeclared" in message
    assert "extra" not in message
    # The DECLARED-field refusal path is unaffected: a bad declared field is still
    # named (control — the message did not simply go quiet).
    bad_declared = {"request_id": "req-12345678", "record_id": "too-short", "body": "hi"}

    async def declared_scenario() -> None:
        await asyncio.wait_for(
            executor.execute(_TOOL, bad_declared, principal=principal), timeout=5
        )

    with pytest.raises(ActionValidationError) as declared_exc:
        asyncio.run(declared_scenario())
    assert "record_id" in str(declared_exc.value)

    assert _approval_rows(engine) == []
    assert "approval.requested" not in sink.actions()
    assert perform.calls == []


def test_missing_idempotency_identity_refused_before_gate() -> None:
    """No ``request_id`` AND no ambient ``action_scope`` refuses PRE-gate (the
    identity leg of the same ``_dedup_key_for`` helper the guard test exercises for
    the field leg)."""
    sink = RecordingAuditSink()
    engine, _store, _gate, perform, executor = _wire(sink)
    principal = make_principal()
    args = {"record_id": _RECORD_ID, "body": "hi"}  # no request_id, no scope

    async def scenario() -> None:
        await asyncio.wait_for(executor.execute(_TOOL, args, principal=principal), timeout=5)

    with pytest.raises(ActionValidationError):
        asyncio.run(scenario())

    assert _approval_rows(engine) == []
    validated = [e for e in sink.events if getattr(e, "action", "") == "tool.validate"]
    assert len(validated) == 1
    assert validated[0].outcome == "denied"
    assert perform.calls == []


def test_valid_call_without_request_id_inside_scope_reaches_gate() -> None:
    """Control for the identity leg: the SAME args, inside an ``action_scope``, reach
    the gate exactly like before this unit."""
    sink = RecordingAuditSink()
    engine, store, gate, perform, executor = _wire(sink)
    principal = make_principal()
    args = {"record_id": _RECORD_ID, "body": "hi"}

    async def scenario() -> str:
        with action_scope("turn-1"):
            task = asyncio.create_task(executor.execute(_TOOL, dict(args), principal=principal))
            await _wait_for_pending(store)
            pending = await store.list_pending()
            await gate.approve(pending[0].request_id, approver="carol")
            return await asyncio.wait_for(task, timeout=10)

    result = asyncio.run(scenario())
    parsed = ActionResult.model_validate_json(result)
    assert parsed.detail == "created"
    assert len(_approval_rows(engine)) == 1
    assert len(perform.calls) == 1


# --- fail-closed legs ----------------------------------------------------------------


def test_validator_crash_fails_closed() -> None:
    """A crashing ``validate_args`` (not a typed ``ActionValidationError``) still
    refuses the call — wrapped, fail closed, C12-safe (no ``'boom'`` in the message)."""
    sink = RecordingAuditSink()
    log = ToolLog()

    def _crash(args: Mapping[str, object]) -> None:
        raise RuntimeError("boom")

    binding = ToolBinding(
        spec=_spec(), handler=log.handler(_TOOL, "should-not-run"), validate_args=_crash
    )
    engine, _store, _gate, executor = _wire_raw_binding(sink, binding)
    principal = make_principal()
    args = {"request_id": "req-12345678", "record_id": _RECORD_ID, "body": "hi"}

    async def scenario() -> None:
        await asyncio.wait_for(executor.execute(_TOOL, args, principal=principal), timeout=5)

    with pytest.raises(ActionValidationError) as excinfo:
        asyncio.run(scenario())
    msg = str(excinfo.value)
    assert "could not run" in msg
    assert "RuntimeError" in msg
    assert "boom" not in msg

    assert log.calls == []  # the handler never ran
    assert _approval_rows(engine) == []
    validated = [e for e in sink.events if getattr(e, "action", "") == "tool.validate"]
    assert len(validated) == 1
    assert validated[0].outcome == "denied"
    detail = json.loads(validated[0].detail)
    assert detail["error_class"] == "RuntimeError"


def test_refusal_names_field_never_value() -> None:
    """C12: the refusal message and every audit event name the FIELD, never the
    rejected VALUE."""
    sink = RecordingAuditSink()
    _engine, _store, _gate, perform, executor = _wire(sink)
    principal = make_principal()
    probe = "NE-C12-PROBE-not-a-valid-id"
    args = {"request_id": "req-12345678", "record_id": probe, "body": "hi"}

    async def scenario() -> None:
        await asyncio.wait_for(executor.execute(_TOOL, args, principal=principal), timeout=5)

    with pytest.raises(ActionValidationError) as excinfo:
        asyncio.run(scenario())
    msg = str(excinfo.value)
    assert "record_id" in msg
    assert probe not in msg

    for event in sink.events:
        detail_blob = str(getattr(event, "detail", None))
        prov_blob = json.dumps(getattr(event, "provenance", None), default=str)
        assert probe not in detail_blob
        assert probe not in prov_blob
    assert perform.calls == []


def test_refusal_formatted_exception_chain_never_carries_the_rejected_value() -> None:
    """C12 chain sweep on the WRITE lane, over BOTH pre-gate refusal raise sites.

    ``test_refusal_names_field_never_value`` above sweeps the MESSAGE and the audit
    rows; neither is the whole surface. pydantic renders the rejected value as
    ``input_value=...``, so while either site chained its cause the value survived in
    ``traceback.format_exception`` output — exactly the text a ``logger.exception`` /
    Sentry / OTel ``record_exception`` would ship, and this lane's values are
    WRITE-action payloads (recipients, note bodies), not a read filter.
    ``raise ... from None`` at both sites is the mechanism; this sweep is the pin, and
    the ``__cause__``/``__suppress_context__`` asserts make each severed link
    individually mutation-visible.

    CONTROL (should-fire): the raw pydantic error's own formatted chain DOES carry the
    sentinel under the identical assertion shape, so a green sweep here is evidence of
    absence rather than a blind sweep.
    """
    import traceback

    from pydantic import ValidationError

    def formatted_chain(exc: BaseException) -> str:
        return "".join(traceback.format_exception(exc))

    sentinel = "SENTINEL-c12-write-payload-5a1c"  # obviously synthetic; not a real id

    # CONTROL: plant the sentinel where it SHOULD appear (pydantic's own error).
    with pytest.raises(ValidationError) as control:
        _NoteInput.model_validate(
            {"request_id": "req-12345678", "record_id": sentinel, "body": "hi"}
        )
    assert sentinel in formatted_chain(control.value)  # the sweep can see a leak

    # Site 1 — the typed pre-gate validator (``action_tools._validate_input``).
    sink = RecordingAuditSink()
    _engine, _store, _gate, perform, executor = _wire(sink)
    principal = make_principal()
    args = {"request_id": "req-12345678", "record_id": sentinel, "body": "hi"}

    async def scenario() -> None:
        await asyncio.wait_for(executor.execute(_TOOL, args, principal=principal), timeout=5)

    with pytest.raises(ActionValidationError) as excinfo:
        asyncio.run(scenario())
    assert excinfo.value.__cause__ is None  # from None: the chain is severed
    assert excinfo.value.__suppress_context__
    assert sentinel not in formatted_chain(excinfo.value)
    assert perform.calls == []

    # Site 2 — the crashing-validator seam (``middleware``'s fail-closed wrap). A
    # buggy validator is handed the raw args, so its own message can carry them.
    crash_sink = RecordingAuditSink()
    log = ToolLog()

    def _crash(_args: Mapping[str, object]) -> None:
        raise RuntimeError(f"validator blew up on {sentinel}")

    binding = ToolBinding(
        spec=_spec(), handler=log.handler(_TOOL, "should-not-run"), validate_args=_crash
    )
    _engine2, _store2, _gate2, crash_executor = _wire_raw_binding(crash_sink, binding)

    async def crash_scenario() -> None:
        await asyncio.wait_for(crash_executor.execute(_TOOL, args, principal=principal), timeout=5)

    with pytest.raises(ActionValidationError) as excinfo2:
        asyncio.run(crash_scenario())
    assert excinfo2.value.__cause__ is None  # from None: the chain is severed
    assert excinfo2.value.__suppress_context__
    assert "RuntimeError" in str(excinfo2.value)  # the CLASS is still named (triage)
    assert sentinel not in formatted_chain(excinfo2.value)
    assert log.calls == []


def test_refusal_audit_failure_withholds_refusal() -> None:
    """C6 fail-closed coupling: if the ``tool.validate[denied]`` append itself fails,
    ``RuntimeAuditError`` propagates INSTEAD OF ``ActionValidationError`` — the
    refusal that could not be recorded still refuses, and still creates no row."""
    sink = AppendCountFailingSink(fail_on_action="tool.validate")
    engine, _store, _gate, perform, executor = _wire(sink)
    principal = make_principal()
    args = {"request_id": "req-12345678", "body": "hi"}  # record_id missing

    async def scenario() -> None:
        await asyncio.wait_for(executor.execute(_TOOL, args, principal=principal), timeout=5)

    with pytest.raises(RuntimeAuditError):
        asyncio.run(scenario())

    assert _approval_rows(engine) == []
    assert perform.calls == []


def test_agent_loop_converts_refusal_to_blocked_result() -> None:
    """Pins ``runner._BLOCKING_ERRORS`` membership: the agent loop converts the
    pre-gate refusal into a ``BLOCKED:`` tool result (not a run-killing exception),
    naming the offending field."""
    sink = RecordingAuditSink()
    engine, _store, _gate, perform, executor = _wire(sink)
    principal = make_principal()
    tool = build_governed_tool(executor, _spec(), principal)
    call = cast(Callable[..., Awaitable[str]], tool.function)

    async def scenario() -> str:
        return await asyncio.wait_for(
            call(**{"request_id": "req-12345678", "body": "hi"}), timeout=5
        )

    result = asyncio.run(scenario())
    assert result.startswith("BLOCKED: ActionValidationError")
    assert "record_id" in result
    assert _approval_rows(engine) == []
    assert perform.calls == []
