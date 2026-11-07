# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The ActionTool framework (NE-050): registration floor, fail-closed input, dedup, audit shape."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import pytest
import sqlalchemy as sa
from pydantic import Field, ValidationError

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
    GovernedAction,
    build_action_binding,
    create_governed_actions,
    ensure_action_dedup_schema,
    ne_action_dedup,
)
from ne.runtime.correlation import action_scope
from ne.runtime.errors import (
    ActionIdempotencyError,
    ActionValidationError,
    ToolExecutionError,
    ToolRegistrationError,
)
from ne.runtime.middleware import GovernedToolExecutor

from .conftest import (
    NamedApproverGate,
    RecordingAuditSink,
    make_pdp,
    make_principal,
)

_TOOL = "write.internal.crm.note"
_SENSITIVE_BODY = "SENSITIVE-BODY-XYZ"
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


def _spec(name: str = _TOOL, tier: ApprovalTier = ApprovalTier.TIER_2) -> ToolSpec:
    return ToolSpec(name=name, description="Note.", tier=tier, input_schema={})


def _definition(
    perform: Any, *, name: str = _TOOL, tier: ApprovalTier = ApprovalTier.TIER_2
) -> ActionDefinition:
    return ActionDefinition(
        spec=_spec(name, tier),
        source_system="salesforce",
        input_model=_NoteInput,
        perform=perform,
    )


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


def _executor(
    definitions: list[ActionDefinition],
    sink: Any,
    *,
    gate: Any,
    dedup: ActionDedupStore,
    overrides: dict[str, dict[str, Any]] | None = None,
    extra_key: bool = False,
) -> GovernedToolExecutor:
    bindings = [build_action_binding(d, dedup=dedup) for d in definitions]
    return GovernedToolExecutor(
        bindings,
        pdp=make_pdp(sink, overrides=overrides),
        guardrails=_guardrails(sink, extra_key=extra_key),
        approval_gate=gate,
        sink=sink,
    )


def _dedup() -> ActionDedupStore:
    engine = sa.create_engine("sqlite://")
    ensure_action_dedup_schema(engine)
    return ActionDedupStore(engine)


# --- registration floor ----------------------------------------------------------


def test_action_definition_rejects_sub_class_tier() -> None:
    """NE-CS-SEND: the floor is the ACTION CLASS tier, one-directional.

    The old absolute Tier-2 floor is gone (owner decision 2026-08-27; see
    ``ActionDefinition.__post_init__``). What remains is stricter where it
    matters: a declared tier below the deny-by-default class tier is refused,
    so ``write.internal.*`` (class 1) refuses Tier-0 and ``write.external.*``
    (class 2) refuses Tier-0/1 — no caller can declare an external write out of
    its human gate.
    """
    perform = _RecordingPerform()
    with pytest.raises(ToolRegistrationError):
        _definition(perform, tier=ApprovalTier.TIER_0)  # class 1 refuses 0
    for tier in (ApprovalTier.TIER_0, ApprovalTier.TIER_1):
        with pytest.raises(ToolRegistrationError):
            _definition(perform, name="write.external.ticket.update", tier=tier)
    # At or above the class tier constructs.
    _definition(perform, tier=ApprovalTier.TIER_1)
    _definition(perform, tier=ApprovalTier.TIER_2)
    _definition(perform, name="write.external.ticket.update", tier=ApprovalTier.TIER_3)


def test_action_definition_rejects_unclassified_or_under_tiered_name() -> None:
    perform = _RecordingPerform()
    with pytest.raises(ToolRegistrationError):
        _definition(perform, name="frobnicate.thing", tier=ApprovalTier.TIER_2)
    # finance.* classifies as Tier-3; declaring Tier-2 is under-tiered.
    with pytest.raises(ToolRegistrationError):
        _definition(perform, name="finance.payout.create", tier=ApprovalTier.TIER_2)


# --- fail-closed invalid input ---------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        {"request_id": "req-12345678", "record_id": "not-a-valid-id", "body": "x"},
        {"request_id": "req-12345678", "record_id": _RECORD_ID, "body": "x", "extra": "y"},
        # no request_id + no ambient scope => fail-loud derived-identity path
        {"record_id": _RECORD_ID, "body": "x"},
    ],
)
def test_invalid_input_fails_closed_and_audits(args: dict[str, Any]) -> None:
    """NE-ACTION-VALIDATE-BEFORE-GATE: all three cases are now PRE-gate refusals —
    ``ActionValidationError`` unwrapped straight out of ``executor.execute`` (never
    reaching the NamedApproverGate), audited as exactly one ``tool.validate[denied]``
    event, with ZERO ``tool.execute`` events of any outcome."""
    sink = RecordingAuditSink()
    perform = _RecordingPerform()
    gate = NamedApproverGate()
    executor = _executor([_definition(perform)], sink, gate=gate, dedup=_dedup(), extra_key=True)

    with pytest.raises(ActionValidationError):
        asyncio.run(executor.execute(_TOOL, args, principal=make_principal()))

    assert perform.calls == []  # never reached the source-system perform
    assert gate.requests == []  # never reached the approval gate at all
    validated = [e for e in sink.events if getattr(e, "action", "") == "tool.validate"]
    assert len(validated) == 1
    assert validated[0].outcome == "denied"
    assert [e for e in sink.events if getattr(e, "action", "") == "tool.execute"] == []


# --- idempotency / dedup ---------------------------------------------------------


def test_idempotent_retry_dedups_and_skips_client() -> None:
    sink = RecordingAuditSink()
    perform = _RecordingPerform()
    dedup = _dedup()
    executor = _executor([_definition(perform)], sink, gate=NamedApproverGate(), dedup=dedup)
    principal = make_principal()
    args = {"request_id": "req-abc12345", "record_id": _RECORD_ID, "body": "hello"}

    async def scenario() -> None:
        first = ActionResult.model_validate_json(
            await executor.execute(_TOOL, dict(args), principal=principal)
        )
        assert first.detail == "created"
        second = ActionResult.model_validate_json(
            await executor.execute(_TOOL, dict(args), principal=principal)
        )
        assert second.detail == "deduplicated"
        assert second.source_ref == first.source_ref
        # Same request_id, DIFFERENT payload => integrity error, perform not re-called.
        with pytest.raises(ToolExecutionError):
            await executor.execute(
                _TOOL,
                {"request_id": "req-abc12345", "record_id": _RECORD_ID, "body": "changed"},
                principal=principal,
            )
        # A different request_id performs a second real write.
        third = ActionResult.model_validate_json(
            await executor.execute(
                _TOOL,
                {"request_id": "req-def67890", "record_id": _RECORD_ID, "body": "hello"},
                principal=principal,
            )
        )
        assert third.detail == "created"

    asyncio.run(scenario())

    assert len(perform.calls) == 2  # two real writes; the retry was dedup'd
    # Both the original and the dedup retry were fully governed (>=4 pdp.decide total).
    pdp = [e for e in sink.events if getattr(e, "action", "") == "pdp.decide"]
    assert len(pdp) == 4


# --- derived turn-scoped identity (NE-ACTION-DETERMINISTIC-REQID) ----------------


def test_step_retry_with_fresh_model_id_dedups() -> None:
    """The finding's exact scenario, closed: a step-retried, re-prompted model that
    mints NO id (the model schema no longer offers one) still dedups within the
    same turn's ``action_scope`` — no model-chosen id is required."""
    sink = RecordingAuditSink()
    perform = _RecordingPerform()
    executor = _executor([_definition(perform)], sink, gate=NamedApproverGate(), dedup=_dedup())
    principal = make_principal()
    args = {"record_id": _RECORD_ID, "body": "hello"}  # no request_id

    async def scenario() -> None:
        with action_scope("turn-1"):
            first = ActionResult.model_validate_json(
                await executor.execute(_TOOL, dict(args), principal=principal)
            )
            # A fresh call under the SAME scope simulates the re-prompted model
            # re-issuing the identical write after a step retry.
            second = ActionResult.model_validate_json(
                await executor.execute(_TOOL, dict(args), principal=principal)
            )
        assert first.detail == "created"
        assert second.detail == "deduplicated"
        assert second.source_ref == first.source_ref

    asyncio.run(scenario())
    assert len(perform.calls) == 1


def test_no_cross_scope_over_dedup_on_derived_path() -> None:
    """Closes the bonus finding: the SAME payload under a DIFFERENT turn's scope is a
    real second write — scope is part of the derived key, so two turns can no longer
    falsely collide on a model-chosen id (there is no model-chosen id anymore)."""
    sink = RecordingAuditSink()
    perform = _RecordingPerform()
    executor = _executor([_definition(perform)], sink, gate=NamedApproverGate(), dedup=_dedup())
    principal = make_principal()
    args = {"record_id": _RECORD_ID, "body": "hello"}

    async def scenario() -> None:
        with action_scope("turn-1"):
            first = ActionResult.model_validate_json(
                await executor.execute(_TOOL, dict(args), principal=principal)
            )
        with action_scope("turn-2"):
            second = ActionResult.model_validate_json(
                await executor.execute(_TOOL, dict(args), principal=principal)
            )
        assert first.detail == "created"
        assert second.detail == "created"

    asyncio.run(scenario())
    assert len(perform.calls) == 2


def test_no_request_id_outside_scope_fails_loud() -> None:
    """No ``request_id`` AND no ambient ``action_scope`` fails CLOSED PRE-gate —
    refused before any approval request, claim, or connector call — a governed write
    always needs a stable identity."""
    sink = RecordingAuditSink()
    perform = _RecordingPerform()
    gate = NamedApproverGate()
    executor = _executor([_definition(perform)], sink, gate=gate, dedup=_dedup())
    args = {"record_id": _RECORD_ID, "body": "hello"}

    with pytest.raises(ActionValidationError):
        asyncio.run(executor.execute(_TOOL, dict(args), principal=make_principal()))

    assert perform.calls == []
    assert gate.requests == []
    validated = [e for e in sink.events if getattr(e, "action", "") == "tool.validate"]
    assert len(validated) == 1
    assert validated[0].outcome == "denied"


def test_distinct_payloads_same_scope_both_write() -> None:
    """Two DIFFERENT payloads in one ``action_scope``, neither with a ``request_id``,
    both perform real writes: the payload hash is part of the derived key, so
    distinct in-turn writes are never swallowed as duplicates of one another."""
    sink = RecordingAuditSink()
    perform = _RecordingPerform()
    executor = _executor([_definition(perform)], sink, gate=NamedApproverGate(), dedup=_dedup())
    principal = make_principal()

    async def scenario() -> None:
        with action_scope("turn-1"):
            first = ActionResult.model_validate_json(
                await executor.execute(
                    _TOOL, {"record_id": _RECORD_ID, "body": "hello"}, principal=principal
                )
            )
            second = ActionResult.model_validate_json(
                await executor.execute(
                    _TOOL, {"record_id": _RECORD_ID, "body": "goodbye"}, principal=principal
                )
            )
        assert first.detail == "created"
        assert second.detail == "created"

    asyncio.run(scenario())
    assert len(perform.calls) == 2


def test_explicit_request_id_ignores_ambient_scope() -> None:
    """An explicit ``request_id`` is scope-independent (Stripe semantics): the SAME
    id + payload under a DIFFERENT ``action_scope`` still dedups — byte-identical to
    today's contract, unaffected by the new scope seam."""
    sink = RecordingAuditSink()
    perform = _RecordingPerform()
    executor = _executor([_definition(perform)], sink, gate=NamedApproverGate(), dedup=_dedup())
    principal = make_principal()
    args = {"request_id": "req-explicit1", "record_id": _RECORD_ID, "body": "hello"}

    async def scenario() -> None:
        with action_scope("turn-1"):
            first = ActionResult.model_validate_json(
                await executor.execute(_TOOL, dict(args), principal=principal)
            )
        with action_scope("turn-2"):
            second = ActionResult.model_validate_json(
                await executor.execute(_TOOL, dict(args), principal=principal)
            )
        assert first.detail == "created"
        assert second.detail == "deduplicated"

    asyncio.run(scenario())
    assert len(perform.calls) == 1


def test_action_input_request_id_defaults_none() -> None:
    """``request_id`` is optional (default ``None``); a subclass validates WITHOUT
    it, and the bounded-token pattern still rejects an invalid value."""
    assert ActionInput.model_fields["request_id"].default is None
    validated = _NoteInput.model_validate({"record_id": _RECORD_ID, "body": "hi"})
    assert validated.request_id is None
    with pytest.raises(ValidationError):
        _NoteInput.model_validate({"record_id": _RECORD_ID, "body": "hi", "request_id": "x"})


def test_concurrent_same_request_id_writes_exactly_once() -> None:
    """Two SIMULTANEOUS same-request_id calls must NOT both reach the source write.

    Regression for the read-then-write TOCTOU: without an atomic claim, both calls pass
    the dedup lookup before either records and both invoke ``perform``. The claim-first
    handler lets exactly one win the unique-PK INSERT; the loser is turned away as
    in-flight BEFORE ``perform`` (fail closed, no double write).
    """
    sink = RecordingAuditSink()
    started = asyncio.Event()
    release = asyncio.Event()

    class _BlockingPerform:
        def __init__(self) -> None:
            self.calls = 0

        async def __call__(self, i: _NoteInput) -> ActionResult:
            self.calls += 1
            started.set()
            await release.wait()  # hold the winner in-flight while the loser races
            return ActionResult(succeeded=True, detail="created", source_ref="REF-1")

    perform = _BlockingPerform()
    executor = _executor([_definition(perform)], sink, gate=NamedApproverGate(), dedup=_dedup())
    principal = make_principal()
    args = {"request_id": "req-concurr1", "record_id": _RECORD_ID, "body": "hello"}

    async def scenario() -> list[Any]:
        t1 = asyncio.create_task(executor.execute(_TOOL, dict(args), principal=principal))
        t2 = asyncio.create_task(executor.execute(_TOOL, dict(args), principal=principal))
        await asyncio.wait_for(started.wait(), timeout=2.0)  # winner reached perform
        release.set()  # let the winner finish
        return list(await asyncio.gather(t1, t2, return_exceptions=True))

    results = asyncio.run(scenario())

    # Exactly one real source write happened despite two concurrent identical calls.
    assert perform.calls == 1
    ok = [r for r in results if isinstance(r, str)]
    failed = [r for r in results if isinstance(r, BaseException)]
    assert len(ok) == 1 and len(failed) == 1
    assert ActionResult.model_validate_json(ok[0]).detail == "created"
    # The loser failed CLOSED through the governed executor (wrapped ToolExecutionError).
    assert isinstance(failed[0], ToolExecutionError)


# --- write-audit shape -----------------------------------------------------------


def test_write_audit_event_carries_shape_and_target(frozen_config_hash: str) -> None:
    sink = RecordingAuditSink()
    perform = _RecordingPerform(source_ref="00Q-TARGET-REF")
    executor = _executor([_definition(perform)], sink, gate=NamedApproverGate(), dedup=_dedup())
    args = {"request_id": "req-shape123", "record_id": _RECORD_ID, "body": _SENSITIVE_BODY}

    asyncio.run(executor.execute(_TOOL, args, principal=make_principal()))

    successes = [
        e
        for e in sink.events
        if getattr(e, "action", "") == "tool.execute" and e.outcome == "success"
    ]
    assert len(successes) == 1
    event = successes[0]
    detail = json.loads(event.detail)
    assert detail["action_kind"] == "write"
    assert detail["source_system"] == "salesforce"
    assert detail["tier"] == 2
    assert detail["fields"] == ["body", "record_id", "request_id"]
    assert detail["target"] == "00Q-TARGET-REF"
    assert detail["deduplicated"] is False
    assert event.config_hash == frozen_config_hash
    # No payload VALUE ever appears in ANY event dump (the note body stays out).
    for e in sink.events:
        assert _SENSITIVE_BODY not in json.dumps(getattr(e, "detail", None))


# --- GovernedAction facade -------------------------------------------------------


def test_governed_action_roundtrips_typed_result() -> None:
    sink = RecordingAuditSink()
    perform = _RecordingPerform(source_ref="REF-ROUNDTRIP")
    definition = _definition(perform)
    executor = _executor([definition], sink, gate=NamedApproverGate(), dedup=_dedup())
    actions = create_governed_actions([definition], executor)
    action = actions[_TOOL]
    assert isinstance(action, GovernedAction)

    result = asyncio.run(
        action.execute(
            {"request_id": "req-roundtr", "record_id": _RECORD_ID, "body": "hi"},
            principal=make_principal(),
        )
    )
    assert isinstance(result, ActionResult)
    assert result.succeeded is True
    assert result.source_ref == "REF-ROUNDTRIP"


# --- P2-12: dedup claim/release race -> NE-050 exactly-once ------------------------


def _seed_pending(engine: sa.Engine, dedup_key: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            ne_action_dedup.insert().values(
                dedup_key=dedup_key,
                action=_TOOL,
                payload_hash="seed",
                result_json="",
                created_at=datetime.now(UTC),
            )
        )


class _VanishOnReadEngine:
    """A real sqlite engine whose ``connect()`` (claim's re-read path) FIRST deletes
    the dedup row — deterministically reproducing the loser-INSERT → concurrent
    release() → vanished re-read interleave from P2-12."""

    def __init__(self, real: sa.Engine, dedup_key: str) -> None:
        self._real = real
        self._dedup_key = dedup_key
        self.connect_calls = 0

    def begin(self) -> Any:
        return self._real.begin()

    def connect(self) -> Any:
        self.connect_calls += 1
        with self._real.begin() as conn:
            conn.execute(
                sa.delete(ne_action_dedup).where(ne_action_dedup.c.dedup_key == self._dedup_key)
            )
        return self._real.connect()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


class _AlwaysLoseEngine:
    """``begin()`` re-inserts a FOREIGN pending row so every claim INSERT loses, and
    ``connect()`` deletes it so every re-read finds nothing — perpetual
    claim/release contention that must exhaust the bounded retries."""

    def __init__(self, real: sa.Engine, dedup_key: str) -> None:
        self._real = real
        self._dedup_key = dedup_key

    def begin(self) -> Any:
        with self._real.begin() as conn:
            conn.execute(
                ne_action_dedup.insert().values(
                    dedup_key=self._dedup_key,
                    action=_TOOL,
                    payload_hash="foreign",
                    result_json="",
                    created_at=datetime.now(UTC),
                )
            )
        return self._real.begin()

    def connect(self) -> Any:
        with self._real.begin() as conn:
            conn.execute(
                sa.delete(ne_action_dedup).where(ne_action_dedup.c.dedup_key == self._dedup_key)
            )
        return self._real.connect()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def test_claim_retries_insert_when_raced_row_vanishes() -> None:
    real = sa.create_engine("sqlite://")
    ensure_action_dedup_schema(real)
    dedup_key = "a" * 64
    _seed_pending(real, dedup_key)  # our first INSERT loses the PK race
    wrapper = _VanishOnReadEngine(real, dedup_key)
    store = ActionDedupStore(wrapper, offload_db_io=False)  # type: ignore[arg-type]

    result = asyncio.run(store.claim(dedup_key, _TOOL, "payload-hash"))

    # We now OWN the claim (the retry INSERT won after the vanished re-read) ...
    assert result is None
    assert wrapper.connect_calls == 1
    # ... and there IS a persisted PENDING row — never false ownership without a row.
    with real.begin() as conn:
        row = conn.execute(
            sa.select(ne_action_dedup).where(ne_action_dedup.c.dedup_key == dedup_key)
        ).first()
    assert row is not None
    assert ActionDedupStore.is_pending(row)

    # finalize then matches exactly one row — exactly-once holds.
    settled = ActionResult(succeeded=True, detail="created", source_ref="REF")
    asyncio.run(store.finalize(dedup_key, settled.model_dump_json()))
    with real.begin() as conn:
        row2 = conn.execute(
            sa.select(ne_action_dedup).where(ne_action_dedup.c.dedup_key == dedup_key)
        ).first()
    assert row2 is not None
    assert not ActionDedupStore.is_pending(row2)
    real.dispose()


def test_claim_bounded_retries_exhaustion_raises() -> None:
    real = sa.create_engine("sqlite://")
    ensure_action_dedup_schema(real)
    dedup_key = "b" * 64
    wrapper = _AlwaysLoseEngine(real, dedup_key)
    store = ActionDedupStore(wrapper, offload_db_io=False)  # type: ignore[arg-type]

    with pytest.raises(ActionIdempotencyError, match="claim/release contention"):
        asyncio.run(store.claim(dedup_key, _TOOL, "payload-hash"))
    real.dispose()


def test_finalize_raises_when_no_pending_row() -> None:
    dedup = _dedup()
    # finalize on an absent dedup_key matches 0 rows → refuse to record success.
    with pytest.raises(ActionIdempotencyError, match="refusing to record success"):
        asyncio.run(dedup.finalize("c" * 64, "{}"))
