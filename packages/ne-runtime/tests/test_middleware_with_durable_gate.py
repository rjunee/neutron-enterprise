# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""C6 composed seam: a real DurableApprovalGate inside the GovernedToolExecutor.

No mocking past the seam — the conftest Rego-faithful PDP + the real guardrail
engine + the real durable gate. A REQUIRE_APPROVAL decision must create a durable
request and BLOCK the tool until a named human approves.
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import re
import tomllib
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa

from ne.contracts.governance import ApprovalStatus
from ne.contracts.identity import AuthenticatedRequestContext
from ne.contracts.source_authority import (
    CurrentSourceAuthorityReceipt,
    M365SourcePoint,
    SourceAuthorityStamp,
    SourceAuthorityState,
    SourceBoundToolResultRef,
    SourceBoundToolResultSetRef,
    SourceContentArtifactIdentity,
    SourceContractIdentity,
    SourceIdentity,
    canonical_source_authority_stamps,
)
from ne.runtime.approval_store import SqlAlchemyApprovalStore, ensure_approval_schema
from ne.runtime.approvals import DurableApprovalGate
from ne.runtime.correlation import (
    ConversationAttemptBinding,
    action_scope,
    conversation_attempt_scope,
)
from ne.runtime.errors import (
    ApprovalIntegrityError,
    ApprovalNotGrantedError,
    ApprovalRequestNotFoundError,
    ApprovalWindowUnhonourableError,
)
from ne.runtime.middleware import _evidence_set_receipt, _run_source_references
from ne.runtime.private_payloads import RunPrivatePayloadCipher, StaticPrivatePayloadKeys
from ne.runtime.run_audit_receipts import (
    ExistingRunEvidenceProvider,
    RunToolCallContext,
    run_tool_call_scope,
)
from ne.runtime.run_checkpoint import (
    RunApprovalCheckpointStore,
    RunApprovalJoin,
    RunApprovalWaiting,
    ne_agent_run_continuation_checkpoints,
)
from ne.runtime.run_store import (
    AgentRun,
    AgentRunStore,
    RunAuditDecisionKind,
    RunAuditReceipt,
    RunState,
    ensure_run_schema,
    ne_agent_runs,
)
from ne.runtime.run_workflow import RunWorkflowStateStore, ensure_run_workflow_schema
from ne.runtime.source_results import (
    authenticated_request_scope,
    encode_protected_reference,
    protected_tool_evidence_scope,
)
from ne.runtime.store import (
    SqlAlchemySessionStore,
    conversation_request_fingerprint,
    ensure_runtime_schema,
    ne_agent_conversation_turns,
)

from .conftest import (
    ALLOWED_RECIPIENT,
    FakeClock,
    RecordingAuditSink,
    ToolLog,
    approval_settings,
    make_executor,
    make_principal,
    standard_bindings,
)

# The action grammar is READ from `tiers.rego`, never retyped here — see
# `_names_a_governed_action`. Single-sourced with the derivation module that
# already parses both the prefix and the infix arms of that grammar.
from .test_approval_window_is_honourable import action_classification_rules

#: The checkout root, from this file at `packages/ne-runtime/tests/`.
_REPO_ROOT = Path(__file__).resolve().parents[3]


def _in_order(haystack: list[str], needles: list[str]) -> bool:
    it = iter(haystack)
    return all(n in it for n in needles)


def _wire(
    sink: Any, settings: Any, clock: Any = None, *, overrides: dict[str, Any] | None = None
) -> tuple[SqlAlchemyApprovalStore, DurableApprovalGate, ToolLog, Any]:
    engine = sa.create_engine("sqlite://")
    ensure_approval_schema(engine)
    store = SqlAlchemyApprovalStore(engine)
    kwargs = {"clock": clock} if clock is not None else {}
    gate = DurableApprovalGate(store=store, sink=sink, settings=settings, **kwargs)
    log = ToolLog()
    executor = make_executor(
        standard_bindings(log), sink, approval_gate=gate, pdp_overrides=overrides
    )
    return store, gate, log, executor


def _source_stamp(item_id: str = "item-a") -> SourceAuthorityStamp:
    source = SourceIdentity(
        deployment_id="deployment-a",
        source_kind="m365-sharepoint",
        connector_instance_id="connector-a",
        point=M365SourcePoint(
            graph_tenant_id="tenant-a", site_id="", drive_id="drive-a", item_id=item_id
        ),
        generation=3,
    )
    contract = SourceContractIdentity(
        source_kind="m365-sharepoint", descriptor_schema_version=1, descriptor_hash="a" * 64
    )
    artifact = SourceContentArtifactIdentity(
        source_identity=source,
        source_snapshot_hash="b" * 64,
        content_snapshot_hash="c" * 64,
        acl_snapshot_hash="d" * 64,
        source_contract=contract,
        artifact_schema_version=1,
    )
    receipt = CurrentSourceAuthorityReceipt(
        receipt_id="receipt-a",
        source_identity=source,
        artifact_identity=artifact,
        source_revision="etag-a",
        source_contract=contract,
        contract_epoch=4,
        acl_snapshot_hash=artifact.acl_snapshot_hash,
        state=SourceAuthorityState.PRESENT,
        admitted_at=datetime(2026, 8, 21, 12, tzinfo=UTC),
        authority_as_of=datetime(2026, 8, 21, 11, 59, tzinfo=UTC),
        audit_event_id="audit-a",
        lineage_id="lineage-a",
        freshness_sla_seconds=Decimal("900"),
    )
    return SourceAuthorityStamp(
        source_identity=source,
        artifact_identity=artifact,
        selected_head_revision=receipt.source_revision,
        acl_snapshot_hash=receipt.acl_snapshot_hash,
        source_contract=contract,
        contract_epoch=receipt.contract_epoch,
        authority_receipt_id=receipt.receipt_id,
        authority_receipt_hash=receipt.receipt_hash,
    )


def _run_tool_context(*refs: str, owner_principal_id: str) -> RunToolCallContext:
    now = datetime(2026, 8, 23, tzinfo=UTC)
    run = AgentRun(
        run_id="run-canonical-approval",
        conversation_id="conversation-canonical-approval",
        owner_principal_id=owner_principal_id,
        department="engineering",
        conversation_agent_name="agent-a",
        agent_id="agent-a",
        agent_version=1,
        strategy="agentic",
        config_hash="1" * 64,
        capability_snapshot_hash="2" * 64,
        capability_snapshot_receipt_id="capability-receipt-a",
        sequence=1,
        display_label="Run 1",
        status=RunState.RUNNING,
        created_at=now,
        updated_at=now,
        terminal_at=None,
    )
    receipt = RunAuditReceipt(
        run_audit_receipt_id="run-audit-receipt-a",
        run_id=run.run_id,
        attempt_id="attempt-a",
        run_event_id="run-event-a",
        step_id="run-tool",
        tool_call_id="run-tool:tool:0",
        approval_id=None,
        decision_kind=RunAuditDecisionKind.STATE_TRANSITION,
        audit_event_id="audit-event-a",
        audit_sequence=1,
        audit_record_hash="3" * 64,
        config_hash=run.config_hash,
        created_at=now,
    )
    return RunToolCallContext(
        run=run,
        receipt=receipt,
        conversation_id=run.conversation_id,
        turn_id="turn-a",
        turn_sequence=1,
        attachment_id="attachment-a",
        evidence=ExistingRunEvidenceProvider.from_existing(run=run, receipt=receipt),
        served_protected_refs=refs,
    )


def test_require_approval_creates_durable_request_and_tool_does_not_execute() -> None:
    sink = RecordingAuditSink()
    store, gate, log, executor = _wire(sink, approval_settings(poll_interval_s=0.0))
    principal = make_principal()

    async def scenario() -> None:
        task = asyncio.create_task(
            executor.execute(
                "write.external.email.send",
                {"to": ALLOWED_RECIPIENT, "body": "hi"},
                principal=principal,
            )
        )
        while not await store.list_pending():
            await asyncio.sleep(0)
        # The tool handler has NOT run while the request is pending.
        assert log.calls == []
        pending = await store.list_pending()
        await gate.approve(pending[0].request_id, approver="carol")
        result = await asyncio.wait_for(task, timeout=10)
        assert result == "sent"
        assert log.names() == ["write.external.email.send"]

    asyncio.run(scenario())

    actions = sink.actions()
    assert _in_order(
        actions,
        ["approval.requested", "approval.granted", "tool.approval", "tool.execute"],
    )
    execs = [e for e in sink.events if getattr(e, "action", "") == "tool.execute"]
    assert execs and execs[0].outcome == "success"


def test_canonical_run_approval_uses_atomic_checkpoint_and_parks_without_failure() -> None:
    sink = RecordingAuditSink()
    _store, gate, log, executor = _wire(sink, approval_settings(poll_interval_s=0.0))
    principal = make_principal()
    protected = encode_protected_reference(
        SourceBoundToolResultRef(
            reference_id="protected-search-result-a",
            stamps=(_source_stamp(),),
            result_hash=hashlib.sha256(b"protected search result").hexdigest(),
        )
    )
    joined = RunApprovalJoin(
        approval_id="approval-run-a",
        run_id="run-canonical-approval",
        attempt_id="attempt-a",
        checkpoint_id="checkpoint-a",
        action_hash="0" * 64,
        principal_id="alice",
        policy_hash="0" * 64,
        protected_invocation_id="invocation-a",
        protected_invocation_hash="0" * 64,
        tier=2,
        action="external.write",
    )

    class _Checkpoints:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []
            self.status = "pending"

        async def find_resumed_join(self, **_kwargs: Any) -> Any:
            # No prior RESUMED continuation for this exact call identity, so the
            # gate parks a fresh wait — the branch this test drives.
            return None

        async def create_wait(self, **kwargs: Any) -> RunApprovalJoin:
            self.calls.append(kwargs)
            return replace(
                joined,
                action_hash=str(kwargs["action_hash"]),
                policy_hash=str(kwargs["policy_hash"]),
                principal_id=str(kwargs["principal_id"]),
            )

        async def load_checkpoint(self, *_args: Any, **_kwargs: Any) -> Any:
            return SimpleNamespace(status=self.status)

    checkpoints = _Checkpoints()
    gate._run_approval_checkpoints = checkpoints  # type: ignore[attr-defined]

    async def legacy_gate_is_forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("canonical Run incorrectly used the legacy approval gate")

    gate.request_approval = legacy_gate_is_forbidden  # type: ignore[method-assign]

    async def scenario() -> None:
        with (
            run_tool_call_scope(_run_tool_context(protected, owner_principal_id=principal.id)),
            protected_tool_evidence_scope((protected,)),
            pytest.raises(RunApprovalWaiting) as raised,
        ):
            await executor.execute(
                "write.external.email.send",
                {"to": ALLOWED_RECIPIENT, "body": "hi"},
                principal=principal,
            )
        assert raised.value.joined.run_id == "run-canonical-approval"

        class _ApprovalRows:
            async def get(self, _request_id: str) -> Any:
                return SimpleNamespace(
                    status="approved",
                    first_approver="approver:carol",
                    second_approver=None,
                    turn_attempt_id=None,
                )

        checkpoints.status = "resumed"
        gate._store = _ApprovalRows()  # type: ignore[assignment]
        with (
            run_tool_call_scope(_run_tool_context(protected, owner_principal_id=principal.id)),
            protected_tool_evidence_scope((protected,)),
        ):
            assert (
                await executor.execute(
                    "write.external.email.send",
                    {"to": ALLOWED_RECIPIENT, "body": "hi"},
                    principal=principal,
                )
                == "sent"
            )

    asyncio.run(scenario())
    assert log.names() == ["write.external.email.send"]
    assert len(checkpoints.calls) == 2
    call = checkpoints.calls[0]
    assert call["run_id"] == "run-canonical-approval"
    assert call["attempt_id"] == "attempt-a"
    assert call["step_id"] == "run-tool"
    assert call["tool_call_id"] == "run-tool:tool:0"
    assert call["principal_id"] == principal.id
    assert call["stamp_receipt_id"] == "protected-search-result-a"
    assert call["source_stamps"] == (_source_stamp(),)
    assert protected.encode() not in call["invocation_plaintext"]
    assert list(asyncio.run(_store.list_pending())) == []


def test_canonical_run_approval_refuses_absent_or_unserved_ambient_evidence() -> None:
    protected = encode_protected_reference(
        SourceBoundToolResultRef(
            reference_id="protected-search-result-a",
            stamps=(_source_stamp(),),
            result_hash=hashlib.sha256(b"protected search result").hexdigest(),
        )
    )
    context = _run_tool_context(protected, owner_principal_id="alice")
    # U3-F4: the two empty-carrier refusals are DISTINGUISHABLE. An empty
    # carrier while the executor HAS served protected references means the
    # governed caller failed to bind the served evidence around this escalated
    # call (the permanently-un-parkable signature of live run 7f782fe0) — a
    # server-side binding bug, not an evidence-less run.
    with pytest.raises(ApprovalIntegrityError, match="did not bind the served evidence"):
        _run_source_references(context)

    # A genuinely evidence-less run (nothing served, nothing bound) still
    # refuses — and names THAT, so audit never confuses the two.
    evidence_less = _run_tool_context(owner_principal_id="alice")
    with pytest.raises(ApprovalIntegrityError, match="served no protected source evidence"):
        _run_source_references(evidence_less)

    other = encode_protected_reference(
        SourceBoundToolResultRef(
            reference_id="protected-search-result-b",
            stamps=(_source_stamp(),),
            result_hash=hashlib.sha256(b"other protected search result").hexdigest(),
        )
    )
    with (
        protected_tool_evidence_scope((other,)),
        pytest.raises(ApprovalIntegrityError, match="served by this Run Tool executor"),
    ):
        _run_source_references(context)


def test_canonical_run_approval_refuses_an_incomplete_ambient_carrier() -> None:
    """U3/F1: a carrier that DROPS a served reference is refused, not silently bound.

    The gate holds the authoritative server-observed set
    (``context.served_protected_refs``, accumulated by
    ``ReceiptGatedToolExecutor``) and used it for membership only: it proved every
    SELECTED ref was served and never the converse. Completeness therefore rested
    entirely on the out-of-band carrier population in ``AgenticPlanner``
    (``runner.py``), asserted in a docstring and pinned by no test at all.

    A one-line narrowing there — bind only the refs this write's own
    ``evidence_refs`` names — would have parked a perfectly well-formed approval
    over a PROPER SUBSET of the admitted evidence: the ``source_stamps`` union
    would omit the dropped sources, so authority revoked on one of them between
    park and approval would no longer withhold the effect. That is verbatim the
    weaker guarantee the unit claims to avoid, and nothing would have gone red.

    Every proper subset of a four-read ladder is refused here, including the ones
    that keep the first reference (a ``references[0]``-shaped narrowing) and the
    ones that drop only a middle member.
    """

    references = _ladder_references()
    encoded = tuple(encode_protected_reference(reference) for reference in references)
    context = _run_tool_context(*encoded, owner_principal_id="alice")

    # The COMPLETE carrier is admitted and binds all four.
    with protected_tool_evidence_scope(encoded):
        assert _run_source_references(context) == tuple(
            sorted(references, key=lambda reference: reference.reference_id)
        )

    for size in range(1, len(encoded)):
        for subset in itertools.combinations(encoded, size):
            with (
                protected_tool_evidence_scope(subset),
                pytest.raises(ApprovalIntegrityError, match="every protected source reference"),
            ):
                _run_source_references(context)


def test_canonical_run_approval_binds_exactly_the_served_set() -> None:
    """Soundness + completeness together pin the carrier to EXACTLY the served set.

    The pre-existing membership loop refuses any carrier member the executor did
    not serve (carrier subset of served); the completeness check refuses any
    served reference the carrier dropped (served subset of carrier). Neither
    direction is slack, so the bound evidence set IS the server-observed one —
    which is what makes "the COMPLETE admitted evidence set" a property of this
    gate rather than a promise about its single caller.
    """

    references = _ladder_references()
    encoded = tuple(encode_protected_reference(reference) for reference in references)
    unserved = encode_protected_reference(
        SourceBoundToolResultRef(
            reference_id="protected-read-never-served",
            stamps=(_source_stamp("item-never-served"),),
            result_hash=hashlib.sha256(b"never served").hexdigest(),
        )
    )
    context = _run_tool_context(*encoded, owner_principal_id="alice")

    # Superset: a carrier member the executor never served is refused (soundness).
    with (
        protected_tool_evidence_scope((*encoded, unserved)),
        pytest.raises(ApprovalIntegrityError, match="served by this Run Tool executor"),
    ):
        _run_source_references(context)

    # Subset: a served reference the carrier dropped is refused (completeness).
    with (
        protected_tool_evidence_scope(encoded[:-1]),
        pytest.raises(ApprovalIntegrityError, match="every protected source reference"),
    ):
        _run_source_references(context)

    # Exactly equal: admitted.
    with protected_tool_evidence_scope(encoded):
        assert len(_run_source_references(context)) == len(references)


def test_canonical_run_approval_completeness_covers_result_set_references() -> None:
    """A served result-SET reference carries the same completeness obligation.

    ``SourceBoundToolResultSetRef`` SUBCLASSES ``SourceBoundToolResultRef``, so a
    set-shaped read is bindable evidence: the carrier's selector
    (``_protected_tool_evidence_refs``) admits it and the soundness loop accepts
    it. The completeness check therefore uses the SAME predicate — a narrower one
    would leave the dropped-reference hole open for precisely the set-shaped
    reads. Sibling kinds (``ProtectedSourceOutputRef``) are bound by neither side.
    """

    tool_ref = encode_protected_reference(
        SourceBoundToolResultRef(
            reference_id="protected-search-result-a",
            stamps=(_source_stamp(),),
            result_hash=hashlib.sha256(b"protected search result").hexdigest(),
        )
    )
    set_reference = SourceBoundToolResultSetRef(
        reference_id="protected-search-result-set-a",
        stamps=(_source_stamp("item-set"),),
        result_hash=hashlib.sha256(b"protected search result set").hexdigest(),
        evidence_ids=("evidence-set-member-a", "evidence-set-member-b"),
    )
    set_ref = encode_protected_reference(set_reference)
    context = _run_tool_context(tool_ref, set_ref, owner_principal_id="alice")

    # Dropping the served SET reference is a completeness failure.
    with (
        protected_tool_evidence_scope((tool_ref,)),
        pytest.raises(ApprovalIntegrityError, match="every protected source reference"),
    ):
        _run_source_references(context)

    # Carrying both binds both.
    with protected_tool_evidence_scope((tool_ref, set_ref)):
        assert len(_run_source_references(context)) == 2


def _ladder_references() -> tuple[SourceBoundToolResultRef, ...]:
    """Four stamped Tool-result references, one per governed read of the ladder."""

    return tuple(
        SourceBoundToolResultRef(
            reference_id=f"protected-read-{name}",
            stamps=(_source_stamp(f"item-{name}"),),
            result_hash=hashlib.sha256(f"protected {name} result".encode()).hexdigest(),
        )
        for name in ("graph-path", "loan-row", "servicing-note", "disaster-decl")
    )


def test_canonical_run_approval_parks_after_a_multi_read_ladder() -> None:
    """S5 regression: a Tier-2 send that follows SEVERAL governed reads still parks.

    The live 2026-08-28 S5 run (graph path -> loan row -> servicing notes ->
    disaster declarations -> CRM contact -> draft) reached
    ``write.external.email.send`` carrying FOUR admitted protected Tool-result
    references and was refused with "exactly one admitted protected source
    reference" -- no approval row was ever parked. The approval must instead bind
    the COMPLETE admitted evidence set: the union of every reference's authority
    stamps (so disclosure/resume re-checks authority over ALL the sources the
    effect rests on) and a deterministic evidence-set receipt binding over the
    exact ``(reference_id, result_hash)`` members.
    """

    sink = RecordingAuditSink()
    _store, gate, log, executor = _wire(sink, approval_settings(poll_interval_s=0.0))
    principal = make_principal()
    references = _ladder_references()
    encoded = tuple(encode_protected_reference(reference) for reference in references)

    class _Checkpoints:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def find_resumed_join(self, **_kwargs: Any) -> Any:
            # No prior RESUMED continuation for this exact call identity, so the
            # gate parks a fresh wait — the branch this test drives.
            return None

        async def create_wait(self, **kwargs: Any) -> RunApprovalJoin:
            self.calls.append(kwargs)
            return RunApprovalJoin(
                approval_id="approval-run-ladder",
                run_id="run-canonical-approval",
                attempt_id="attempt-a",
                checkpoint_id="checkpoint-ladder",
                action_hash=str(kwargs["action_hash"]),
                principal_id=str(kwargs["principal_id"]),
                policy_hash=str(kwargs["policy_hash"]),
                protected_invocation_id="invocation-ladder",
                protected_invocation_hash="0" * 64,
                tier=int(kwargs["tier"]),
                action=str(kwargs["action"]),
            )

        async def load_checkpoint(self, *_args: Any, **_kwargs: Any) -> Any:
            return SimpleNamespace(status="pending")

    checkpoints = _Checkpoints()
    gate._run_approval_checkpoints = checkpoints  # type: ignore[attr-defined]

    async def scenario() -> None:
        with (
            run_tool_call_scope(_run_tool_context(*encoded, owner_principal_id=principal.id)),
            protected_tool_evidence_scope(encoded),
            pytest.raises(RunApprovalWaiting) as raised,
        ):
            await executor.execute(
                "write.external.email.send",
                {"to": ALLOWED_RECIPIENT, "body": "hi"},
                principal=principal,
            )
        assert raised.value.joined.run_id == "run-canonical-approval"

    asyncio.run(scenario())
    assert log.calls == []  # parked, never executed
    assert len(checkpoints.calls) == 1
    call = checkpoints.calls[0]
    ordered = tuple(sorted(references, key=lambda reference: reference.reference_id))
    assert call["source_stamps"] == canonical_source_authority_stamps(
        tuple(stamp for reference in references for stamp in reference.stamps)
    )
    assert len(call["source_stamps"]) == len(references)  # the UNION, not one read's stamps
    assert (call["stamp_receipt_id"], call["stamp_receipt_hash"]) == _evidence_set_receipt(ordered)
    assert call["stamp_receipt_id"].startswith("evidence-set:")
    for reference in references:
        assert encode_protected_reference(reference).encode() not in call["invocation_plaintext"]


def test_single_read_ladder_keeps_the_historical_receipt_binding() -> None:
    """One admitted reference still binds as itself: its id and result hash."""

    reference = SourceBoundToolResultRef(
        reference_id="protected-search-result-a",
        stamps=(_source_stamp(),),
        result_hash=hashlib.sha256(b"protected search result").hexdigest(),
    )
    assert _evidence_set_receipt((reference,)) == (
        reference.reference_id,
        reference.result_hash,
    )


def test_multi_read_ladder_parks_a_durable_approval_row(tmp_path: Path) -> None:
    """The demo-beat assertion, on the REAL stores: the ladder ends in a park.

    Multi-read evidence -> Tier-2 send -> ``RunApprovalWaiting`` with a pending
    run-bound ``ne_approval_requests`` row, a pending continuation checkpoint
    carrying the evidence-set binding, and the run atomically WAITING_APPROVAL.
    """

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'ladder.db'}")
    ensure_runtime_schema(engine)
    ensure_run_schema(engine)
    ensure_run_workflow_schema(engine)
    ensure_approval_schema(engine)
    runs = AgentRunStore(
        engine,
        RunPrivatePayloadCipher(
            StaticPrivatePayloadKeys(current_key_id="k1", keys={"k1": b"k" * 32})
        ),
    )
    sessions = SqlAlchemySessionStore(engine)
    approvals = SqlAlchemyApprovalStore(engine)
    checkpoints = RunApprovalCheckpointStore(runs, approvals)
    sink = RecordingAuditSink()
    gate = DurableApprovalGate(store=approvals, sink=sink, settings=approval_settings())
    gate._run_approval_checkpoints = checkpoints  # type: ignore[attr-defined]
    log = ToolLog()
    executor = make_executor(standard_bindings(log), sink, approval_gate=gate)
    principal = make_principal("alice")
    references = _ladder_references()
    encoded = tuple(encode_protected_reference(reference) for reference in references)

    async def scenario() -> tuple[Any, Any]:
        conversation = await sessions.create_conversation("agent-a", "finance", principal.id)
        accepted = await runs.admit_start(
            SimpleNamespace(
                idempotency_key=f"start-{uuid.uuid4()}",
                request_hash=uuid.uuid4().hex * 2,
                expected_version=0,
                input_plaintext=b"protected prompt",
                conversation_id=conversation.conversation_id,
                owner_principal_id=principal.id,
                department="finance",
                conversation_agent_name="agent-a",
                agent_id="agent-a",
                agent_version=1,
                strategy="agentic",
                config_hash="1" * 64,
                capability_snapshot_hash="2" * 64,
                capability_snapshot_receipt_id="snapshot-receipt",
                authenticated_context=AuthenticatedRequestContext(
                    principal_id=principal.id,
                    authorization_groups=(),
                    department="finance",
                    issuer="https://issuer.example.test",
                    expires_at=4_102_444_800,
                ),
            )
        )
        RunWorkflowStateStore(engine, run_store=runs).claim(
            dispatch_id=accepted.enqueue_id,
            run_id=accepted.run_id,
            owner_principal_id=principal.id,
            config_hash="1" * 64,
        )
        run = await runs.get_run(run_id=accepted.run_id, owner_principal_id=principal.id)
        assert run is not None
        now = datetime.now(UTC)
        receipt = RunAuditReceipt(
            run_audit_receipt_id="run-audit-receipt-ladder",
            run_id=run.run_id,
            attempt_id=accepted.attempt_id,
            run_event_id="run-event-ladder",
            step_id="run-tool",
            tool_call_id="run-tool:tool:7",
            approval_id=None,
            decision_kind=RunAuditDecisionKind.STATE_TRANSITION,
            audit_event_id="audit-event-ladder",
            audit_sequence=1,
            audit_record_hash="3" * 64,
            config_hash=run.config_hash,
            created_at=now,
        )
        context = RunToolCallContext(
            run=run,
            receipt=receipt,
            conversation_id=run.conversation_id,
            turn_id="turn-ladder",
            turn_sequence=1,
            attachment_id="attachment-ladder",
            evidence=ExistingRunEvidenceProvider.from_existing(run=run, receipt=receipt),
            served_protected_refs=encoded,
        )
        with (
            run_tool_call_scope(context),
            protected_tool_evidence_scope(encoded),
            pytest.raises(RunApprovalWaiting) as raised,
        ):
            await executor.execute(
                "write.external.email.send",
                {"to": ALLOWED_RECIPIENT, "body": "hi"},
                principal=principal,
            )
        pending = await approvals.list_pending()
        return raised.value.joined, pending

    joined, pending = asyncio.run(scenario())
    assert log.calls == []  # the send handler never ran
    assert [row.request_id for row in pending] == [joined.approval_id]  # THE parked row
    row = pending[0]
    assert row.action == "write.external.email.send"
    assert int(row.tier) == 2
    ordered = tuple(sorted(references, key=lambda reference: reference.reference_id))
    expected_receipt_id, expected_receipt_hash = _evidence_set_receipt(ordered)
    with engine.connect() as conn:
        checkpoint = conn.execute(sa.select(ne_agent_run_continuation_checkpoints)).one()
        run_status = conn.execute(sa.select(ne_agent_runs.c.status)).scalar_one()
    assert checkpoint.status == "pending"
    assert checkpoint.stamp_receipt_id == expected_receipt_id
    assert checkpoint.stamp_receipt_hash == expected_receipt_hash
    assert run_status == RunState.WAITING_APPROVAL.value
    engine.dispose()


def test_attempt_takeover_fences_old_approval_continuation_before_handler() -> None:
    sink = RecordingAuditSink()
    store, gate, log, executor = _wire(sink, approval_settings(poll_interval_s=0.0))
    ensure_runtime_schema(store.engine)
    session_store = SqlAlchemySessionStore(store.engine)
    principal = make_principal()

    async def scenario() -> None:
        conversation = await session_store.create_conversation(
            "console-chat", "engineering", principal.id
        )
        turn_id = "turn-approval-continuation"
        trace_id = "0123456789abcdef0123456789abcdef"
        fingerprint = conversation_request_fingerprint(
            conversation.conversation_id,
            principal.id,
            "engineering",
            "console-chat",
            "send",
            None,
            None,
            trace_id,
        )
        first = await session_store.claim_conversation_attachment(
            conversation.conversation_id,
            principal.id,
            "engineering",
            "console-chat",
            turn_id,
            fingerprint,
            trace_id,
        )
        assert first.attempt_id is not None
        first_binding = ConversationAttemptBinding(
            conversation.conversation_id,
            principal.id,
            "engineering",
            "console-chat",
            turn_id,
            first.attempt_id,
            trace_id,
        )
        with action_scope(turn_id), conversation_attempt_scope(first_binding):
            old_task = asyncio.create_task(
                executor.execute(
                    "write.external.email.send",
                    {"to": ALLOWED_RECIPIENT, "body": "old-attempt"},
                    principal=principal,
                )
            )
        while not await gate.list_pending():
            await asyncio.sleep(0)
        old_request = (await gate.list_pending())[0].request_id
        with session_store.engine.begin() as conn:
            conn.execute(
                ne_agent_conversation_turns.update()
                .where(ne_agent_conversation_turns.c.attachment_id == turn_id)
                .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
        second = await session_store.claim_conversation_attachment(
            conversation.conversation_id,
            principal.id,
            "engineering",
            "console-chat",
            turn_id,
            fingerprint,
            trace_id,
        )
        assert second.attempt_id is not None and second.attempt_id != first.attempt_id
        with pytest.raises(ApprovalRequestNotFoundError):
            await gate.approve(old_request, approver="carol")
        with pytest.raises(ApprovalRequestNotFoundError):
            await old_task
        assert log.calls == []

        second_binding = ConversationAttemptBinding(
            conversation.conversation_id,
            principal.id,
            "engineering",
            "console-chat",
            turn_id,
            second.attempt_id,
            trace_id,
        )
        with action_scope(turn_id), conversation_attempt_scope(second_binding):
            new_task = asyncio.create_task(
                executor.execute(
                    "write.external.email.send",
                    {"to": ALLOWED_RECIPIENT, "body": "new-attempt"},
                    principal=principal,
                )
            )
        while not await gate.list_pending():
            await asyncio.sleep(0)
        new_request = (await gate.list_pending())[0].request_id
        assert new_request != old_request
        await gate.approve(new_request, approver="carol")
        assert await asyncio.wait_for(new_task, timeout=10) == "sent"
        assert log.names() == ["write.external.email.send"]

    asyncio.run(scenario())


def test_expired_approval_denies_tool_and_audits() -> None:
    sink = RecordingAuditSink()
    clock = FakeClock()
    store, _gate, log, executor = _wire(
        sink, approval_settings(expiry_s=1000.0, poll_interval_s=0.0), clock=clock
    )
    principal = make_principal()

    async def scenario() -> None:
        task = asyncio.create_task(
            executor.execute(
                "write.external.email.send",
                {"to": ALLOWED_RECIPIENT, "body": "hi"},
                principal=principal,
            )
        )
        while not await store.list_pending():
            await asyncio.sleep(0)
        # ADR-0030: the POLICY window (tier-2 floor: 14400 s) governs the row,
        # not the deployment-wide expiry_s=1000. Advancing past the deployment
        # default but inside the policy window leaves the approval answerable —
        # the exact "unusably short" failure this closed.
        clock.advance(2000.0)
        for _ in range(5):
            await asyncio.sleep(0)
        assert await store.list_pending(), "row expired at expiry_s despite the policy window"
        clock.advance(14400.0)  # now past the policy window while still parked
        try:
            await asyncio.wait_for(task, timeout=10)
            raise AssertionError("expected ApprovalNotGrantedError")
        except ApprovalNotGrantedError as exc:
            assert exc.status is ApprovalStatus.EXPIRED

    asyncio.run(scenario())

    assert log.calls == []  # handler NEVER ran
    assert "approval.expired" in sink.actions()
    denied = [
        e
        for e in sink.events
        if getattr(e, "action", "") == "tool.approval" and e.outcome == "denied"
    ]
    assert denied


def test_tier3_action_never_executes_on_single_approval() -> None:
    sink = RecordingAuditSink()
    store, gate, log, executor = _wire(
        sink, approval_settings(tier3_named_approvers=("alice", "bob"), poll_interval_s=0.0)
    )
    principal = make_principal()

    async def scenario() -> None:
        task = asyncio.create_task(
            executor.execute("finance.payment.initiate", {"amount": 100}, principal=principal)
        )
        while not await store.list_pending():
            await asyncio.sleep(0)
        # A single named approval leaves the request pending its second check.
        rid = (await store.list_pending())[0].request_id
        assert await gate.approve(rid, approver="alice") is None
        for _ in range(5):
            await asyncio.sleep(0)
        assert log.calls == []  # still blocked after one approval
        still = await store.list_pending()
        assert still and still[0].tier == 3
        # A distinct named approver releases exactly one execution.
        await gate.approve(still[0].request_id, approver="bob")
        result = await asyncio.wait_for(task, timeout=10)
        assert result == "paid"
        assert log.names() == ["finance.payment.initiate"]

    asyncio.run(scenario())


def test_concurrent_distinct_calls_get_independent_approvals() -> None:
    """End-to-end Argus-P1 regression at the real C6 seam: two GENUINELY DISTINCT
    concurrent governed tool calls (same action/tier/principal, same ambient
    action_scope, DIFFERENT arguments) must get TWO independent pending approval
    rows, each requiring its own human decision — a single approval must never
    release both. Mirrors pydantic-ai's real dispatch shape: a turn's tool calls
    run concurrently via asyncio.create_task with no sequential guard."""
    sink = RecordingAuditSink()
    store, gate, log, executor = _wire(sink, approval_settings(poll_interval_s=0.0))
    principal = make_principal()

    async def scenario() -> None:
        with action_scope("turn-1"):
            task_a = asyncio.create_task(
                executor.execute(
                    "write.external.email.send",
                    {"to": ALLOWED_RECIPIENT, "body": "invoice A"},
                    principal=principal,
                )
            )
            task_b = asyncio.create_task(
                executor.execute(
                    "write.external.email.send",
                    {"to": ALLOWED_RECIPIENT, "body": "invoice B"},
                    principal=principal,
                )
            )
            while len(await store.list_pending()) < 2:
                await asyncio.sleep(0)

            # Nothing executed pre-approval, and the two pending requests are
            # DISTINCT rows — a single approval row would mean one human decision
            # could release BOTH genuinely different external writes.
            assert log.calls == []
            pending = await store.list_pending()
            request_ids = {p.request_id for p in pending}
            assert len(request_ids) == 2

            for rid in request_ids:
                await gate.approve(rid, approver="carol")

            result_a, result_b = await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=10)
            assert result_a == "sent"
            assert result_b == "sent"
            assert log.names() == ["write.external.email.send", "write.external.email.send"]

    asyncio.run(scenario())


def test_submitted_row_carries_disclosure() -> None:
    """The end-to-end persistence half of the owner ruling (2026-07-31): a real
    governed turn's tier-2 tool call persists its RAW args into the row's
    'disclosure.material_parameters', readable via gate.read_payload — and the
    probe value appears in NO audit event anywhere (C12 control)."""
    sink = RecordingAuditSink()
    store, gate, log, executor = _wire(sink, approval_settings(poll_interval_s=0.0))
    principal = make_principal()
    probe = "NE-APPROVAL-PAYLOAD-PROBE-9f1c"

    async def scenario() -> str:
        task = asyncio.create_task(
            executor.execute(
                "write.external.email.send",
                {"to": ALLOWED_RECIPIENT, "body": probe},
                principal=principal,
            )
        )
        while not await store.list_pending():
            await asyncio.sleep(0)
        pending = await store.list_pending()
        rid = pending[0].request_id
        disclosure = await gate.read_payload(rid, approver="carol")
        assert disclosure.material_parameters == {"to": ALLOWED_RECIPIENT, "body": probe}
        await gate.approve(rid, approver="carol")
        await asyncio.wait_for(task, timeout=10)
        return rid

    asyncio.run(scenario())

    # C12 control: the probe value must appear in NO audit event anywhere.
    for event in sink.events:
        blob = str(getattr(event, "detail", None)) + str(getattr(event, "provenance", None))
        assert probe not in blob

    assert log.names() == ["write.external.email.send"]


def test_submitted_row_disclosure_readable_before_decision() -> None:
    """The disclosure is readable WHILE the request is still pending (before any
    decision) — an approver reads what they are approving BEFORE approving it."""
    sink = RecordingAuditSink()
    store, gate, log, executor = _wire(sink, approval_settings(poll_interval_s=0.0))
    principal = make_principal()
    probe = "NE-APPROVAL-PAYLOAD-PROBE-b2d8"

    async def scenario() -> None:
        task = asyncio.create_task(
            executor.execute(
                "write.external.email.send",
                {"to": ALLOWED_RECIPIENT, "body": probe},
                principal=principal,
            )
        )
        while not await store.list_pending():
            await asyncio.sleep(0)
        pending = await store.list_pending()
        rid = pending[0].request_id
        disclosure = await gate.read_payload(rid, approver="carol")
        assert disclosure.material_parameters == {"to": ALLOWED_RECIPIENT, "body": probe}
        assert disclosure.target_resource == "tool:write.external.email.send"
        assert "external-facing" in disclosure.reversibility
        await gate.approve(rid, approver="carol")
        await asyncio.wait_for(task, timeout=10)

    asyncio.run(scenario())
    assert log.names() == ["write.external.email.send"]


# --- U3-F4 round 2: an ACTUALLY-ESCALATED Tier-0 call parks over the carrier --


def test_pdp_escalated_tier0_read_parks_a_canonical_approval_bound_to_served_evidence() -> None:
    """The boundary itself, end to end: a NON-write tool whose SPEC tier is 0 is
    escalated to Tier-2 by the PDP-DECIDED tier — the one arm the runner can
    never predict from the spec it reads — through the REAL `_RecordedExecutor`
    -> `GovernedToolExecutor` -> `_effective_tier` -> canonical-Run approval
    chain. Before the U3-F4 runner fix the call reached
    `_run_source_references` with an EMPTY carrier and died un-parkable
    ("evidence carrier is empty..."); now the runner's unconditional carrier
    population lets the durable wait park BOUND to the run's full served
    evidence set (its stamps re-checked at disclosure/resume), and the tool
    handler never runs before the human decides."""
    from ne.runtime.runner import _RecordedExecutor

    sink = RecordingAuditSink()
    engine = sa.create_engine("sqlite://")
    ensure_approval_schema(engine)
    store = SqlAlchemyApprovalStore(engine)
    gate = DurableApprovalGate(
        store=store, sink=sink, settings=approval_settings(poll_interval_s=0.0)
    )
    log = ToolLog()
    executor = make_executor(
        standard_bindings(log),
        sink,
        approval_gate=gate,
        # The PDP decides Tier-2 for a spec-Tier-0 read. `_effective_tier` is
        # stricter-of, so the DECIDED tier escalates gating; the spec/mirror
        # floor stays 0 and the runner's own protected-effect prediction
        # (`is_write or spec.tier >= TIER_2`) is False for this call.
        pdp_overrides={
            "read.docs.search": {
                "effect": "allow",
                "tier": 2,
                "reason": "decided: sensitive directory read requires approval",
                "obligations": {},
                "policy_version": "1.1.0",
            }
        },
    )
    principal = make_principal()
    protected = encode_protected_reference(
        SourceBoundToolResultRef(
            reference_id="protected-search-result-a",
            stamps=(_source_stamp(),),
            result_hash=hashlib.sha256(b"protected search result").hexdigest(),
        )
    )

    class _Checkpoints:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def find_resumed_join(self, **_kwargs: Any) -> Any:
            return None

        async def create_wait(self, **kwargs: Any) -> RunApprovalJoin:
            self.calls.append(kwargs)
            return RunApprovalJoin(
                approval_id="approval-run-t0",
                run_id="run-canonical-approval",
                attempt_id="attempt-a",
                checkpoint_id="checkpoint-t0",
                # U2 round 3 added the park-time governance columns to the join:
                # the tier and action the human's approval actually covered, taken
                # from the immutable park record rather than the mutable request
                # row. This double mirrors the real create_wait, so it passes them
                # through exactly as the store does.
                tier=int(kwargs["tier"]),
                action=str(kwargs["action"]),
                action_hash=str(kwargs["action_hash"]),
                principal_id=str(kwargs["principal_id"]),
                policy_hash=str(kwargs["policy_hash"]),
                protected_invocation_id="invocation-t0",
                protected_invocation_hash="0" * 64,
            )

        async def load_checkpoint(self, *_args: Any, **_kwargs: Any) -> Any:
            return SimpleNamespace(status="pending")

    checkpoints = _Checkpoints()
    gate._run_approval_checkpoints = checkpoints  # type: ignore[attr-defined]

    # The REAL runner wrapper over the REAL governed executor. `served` holds
    # what an earlier successful read of THIS run delivered; the runner — not
    # the test — must bind it around the escalated call.
    recorded = _RecordedExecutor(
        inner=executor,
        principal=principal,
        sink=sink,
        max_tool_calls=2,
        max_parked_writes=1,
    )
    recorded.served.add(protected)

    async def scenario() -> None:
        with (
            run_tool_call_scope(_run_tool_context(protected, owner_principal_id=principal.id)),
            pytest.raises(RunApprovalWaiting) as raised,
        ):
            await recorded.execute(
                "read.docs.search",
                {"q": "meridian", "step_intent": "Read the directory."},
                principal=principal,
            )
        assert raised.value.joined.run_id == "run-canonical-approval"

    asyncio.run(scenario())

    # Parked, not executed: the handler never ran, and the durable wait binds
    # the run's served evidence set — reference id + authority stamps — so the
    # approval's disclosure/resume re-checks re-derive from THIS evidence.
    assert log.names() == []
    assert len(checkpoints.calls) == 1
    call = checkpoints.calls[0]
    assert call["run_id"] == "run-canonical-approval"
    assert call["stamp_receipt_id"] == "protected-search-result-a"
    assert call["source_stamps"] == (_source_stamp(),)
    assert protected.encode() not in call["invocation_plaintext"]


def test_canonical_run_park_honours_the_policy_window_not_the_deployment_default() -> None:
    """ADR-0030: the canonical Run park's `expires_at` is the POLICY-resolved
    window (tier-2 floor: 14400 s), not `cfg.expiry_s` (1000 s here).

    The conversation park (`_submit_fresh`) honoured the ambient window; the Run
    park at the `create_wait` call site silently kept the deployment default, so
    the two park paths disagreed about how long the same policy promise lasted
    (named in the #944 drop record). The decision arrives through the REAL
    Rego-faithful PDP — the window rides first-class on the decision, never in
    obligations — so this fails if either the middleware read path or the Run
    park regresses.
    """
    sink = RecordingAuditSink()
    _store, gate, _log, executor = _wire(
        sink, approval_settings(expiry_s=1000.0, poll_interval_s=0.0)
    )
    principal = make_principal()
    protected = encode_protected_reference(
        SourceBoundToolResultRef(
            reference_id="protected-search-result-a",
            stamps=(_source_stamp(),),
            result_hash=hashlib.sha256(b"protected search result").hexdigest(),
        )
    )
    joined = RunApprovalJoin(
        approval_id="approval-run-a",
        run_id="run-canonical-approval",
        attempt_id="attempt-a",
        checkpoint_id="checkpoint-a",
        action_hash="0" * 64,
        principal_id="alice",
        policy_hash="0" * 64,
        protected_invocation_id="invocation-a",
        protected_invocation_hash="0" * 64,
        tier=2,
        action="external.write",
    )

    class _Checkpoints:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def find_resumed_join(self, **_kwargs: Any) -> Any:
            return None

        async def create_wait(self, **kwargs: Any) -> RunApprovalJoin:
            self.calls.append(kwargs)
            return joined

        async def load_checkpoint(self, *_args: Any, **_kwargs: Any) -> Any:
            return SimpleNamespace(status="pending")

    checkpoints = _Checkpoints()
    gate._run_approval_checkpoints = checkpoints  # type: ignore[attr-defined]

    async def scenario() -> None:
        with (
            run_tool_call_scope(_run_tool_context(protected, owner_principal_id=principal.id)),
            protected_tool_evidence_scope((protected,)),
            pytest.raises(RunApprovalWaiting),
        ):
            await executor.execute(
                "write.external.email.send",
                {"to": ALLOWED_RECIPIENT, "body": "hi"},
                principal=principal,
            )

    asyncio.run(scenario())
    assert len(checkpoints.calls) == 1
    call = checkpoints.calls[0]
    parked_window_s = (call["expires_at"] - call["requested_at"]).total_seconds()
    assert parked_window_s == 14400.0  # the tier-2 policy floor, NOT expiry_s=1000


def test_canonical_run_park_keeps_escalation_strictly_inside_the_policy_window() -> None:
    """With PRODUCTION settings (tier2_sla_s == the tier-2 policy floor, 14400 s)
    the parked Run row's sla_deadline must land STRICTLY before expires_at.

    `await_outcome` (and the run-checkpoint expiry drivers) check hard expiry
    first, so equality deletes §4.2 escalate-and-notify for every default
    tier-2 Run approval. RED on 1c7ae211: sla_deadline == expires_at.
    """
    sink = RecordingAuditSink()
    _store, gate, _log, executor = _wire(
        sink, approval_settings(tier2_sla_s=14400.0, expiry_s=86400.0, poll_interval_s=0.0)
    )
    principal = make_principal()
    protected = encode_protected_reference(
        SourceBoundToolResultRef(
            reference_id="protected-search-result-a",
            stamps=(_source_stamp(),),
            result_hash=hashlib.sha256(b"protected search result").hexdigest(),
        )
    )
    joined = RunApprovalJoin(
        approval_id="approval-run-a",
        run_id="run-canonical-approval",
        attempt_id="attempt-a",
        checkpoint_id="checkpoint-a",
        action_hash="0" * 64,
        principal_id="alice",
        policy_hash="0" * 64,
        protected_invocation_id="invocation-a",
        protected_invocation_hash="0" * 64,
        tier=2,
        action="external.write",
    )

    class _Checkpoints:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def find_resumed_join(self, **_kwargs: Any) -> Any:
            return None

        async def create_wait(self, **kwargs: Any) -> RunApprovalJoin:
            self.calls.append(kwargs)
            return joined

        async def load_checkpoint(self, *_args: Any, **_kwargs: Any) -> Any:
            return SimpleNamespace(status="pending")

    checkpoints = _Checkpoints()
    gate._run_approval_checkpoints = checkpoints  # type: ignore[attr-defined]

    async def scenario() -> None:
        with (
            run_tool_call_scope(_run_tool_context(protected, owner_principal_id=principal.id)),
            protected_tool_evidence_scope((protected,)),
            pytest.raises(RunApprovalWaiting),
        ):
            await executor.execute(
                "write.external.email.send",
                {"to": ALLOWED_RECIPIENT, "body": "hi"},
                principal=principal,
            )

    asyncio.run(scenario())
    assert len(checkpoints.calls) == 1
    call = checkpoints.calls[0]
    assert (call["expires_at"] - call["requested_at"]).total_seconds() == 14400.0
    sla_s = (call["sla_deadline"] - call["requested_at"]).total_seconds()
    assert call["sla_deadline"] < call["expires_at"], (
        "run-park sla_deadline is not strictly before expires_at: escalation unreachable"
    )
    assert sla_s == 7200.0  # derived: half the policy window


# --- the approval horizon: the window and the owner's authorization are TWO
# clocks, and until these tests nothing compared them. See
# docs/architecture/approval-horizon.md and
# ne.runtime.middleware._require_honourable_approval_window.

#: The park clock every horizon test below shares (``FakeClock``'s default).
_PARK_NOW = datetime(2026, 7, 5, 12, 0, tzinfo=UTC)

#: Seconds of owner authorization left at park in the SHORT-HORIZON case.
#: Deliberately not a round number: the leak assertions below check that neither
#: it nor the overrun it implies appears anywhere in the published sentence, and
#: a round number could collide with an unrelated digit run.
_SHORT_HORIZON_S = 873


def _horizon_checkpoints() -> Any:
    """A recording ``create_wait`` stand-in shared by the horizon tests."""

    joined = RunApprovalJoin(
        approval_id="approval-run-a",
        run_id="run-canonical-approval",
        attempt_id="attempt-a",
        checkpoint_id="checkpoint-a",
        action_hash="0" * 64,
        principal_id="alice",
        policy_hash="0" * 64,
        protected_invocation_id="invocation-a",
        protected_invocation_hash="0" * 64,
        tier=2,
        action="external.write",
    )

    class _Checkpoints:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def find_resumed_join(self, **_kwargs: Any) -> Any:
            return None

        async def create_wait(self, **kwargs: Any) -> RunApprovalJoin:
            self.calls.append(kwargs)
            return joined

        async def load_checkpoint(self, *_args: Any, **_kwargs: Any) -> Any:
            return SimpleNamespace(status="pending")

    return _Checkpoints()


def _owner_context(*, principal_id: str, remaining_s: int) -> AuthenticatedRequestContext:
    """The run owner's frozen snapshot with ``remaining_s`` left at ``_PARK_NOW``."""

    return AuthenticatedRequestContext(
        principal_id=principal_id,
        authorization_groups=(),
        department="engineering",
        issuer="https://issuer.example.test",
        expires_at=int(_PARK_NOW.timestamp()) + remaining_s,
    )


#: A decision document for ``write.external.email.send`` whose 14400 s window is
#: DECLARED rather than inherited from ``tiers.rego``'s floor — i.e. what the
#: bundle emits when ``deploy/opa/data/ne/config/data.json`` names this action.
#: The window value is deliberately the SAME as the floor's, so the two horizon
#: tests below differ in exactly one bit: who chose the number.
_DECLARED_WINDOW_DOC = {
    "effect": "require-approval",
    "tier": 2,
    "reason": "rego-faithful: require-approval (tier 2)",
    "obligations": {},
    "approval_window_seconds": 14400,
    "approval_window_declared": True,
    "policy_version": "1.1.0",
}


def _park_under_horizon(
    remaining_s: int, *, declared: bool = False
) -> tuple[Any, BaseException | None]:
    """Drive one canonical Run park with ``remaining_s`` of owner authorization.

    ``declared`` selects the PDP answer's PROVENANCE, not its value: both routes
    resolve 14400 s, and only the bundle-1.4.0 ``approval_window_declared`` bit
    differs. The conftest's rego-faithful stub has no data document, so its
    windows are always FLOORS — which is the deployment shape the guard must not
    refuse.

    Returns the recording checkpoints stand-in and whatever the park raised
    (``RunApprovalWaiting`` on the parking path).
    """

    sink = RecordingAuditSink()
    overrides = {"write.external.email.send": dict(_DECLARED_WINDOW_DOC)} if declared else None
    _store, gate, log, executor = _wire(
        sink,
        approval_settings(expiry_s=1000.0, poll_interval_s=0.0),
        clock=FakeClock(),
        overrides=overrides,
    )
    principal = make_principal()
    protected = encode_protected_reference(
        SourceBoundToolResultRef(
            reference_id="protected-search-result-a",
            stamps=(_source_stamp(),),
            result_hash=hashlib.sha256(b"protected search result").hexdigest(),
        )
    )
    checkpoints = _horizon_checkpoints()
    gate._run_approval_checkpoints = checkpoints  # type: ignore[attr-defined]

    async def scenario() -> BaseException | None:
        with (
            authenticated_request_scope(
                _owner_context(principal_id=principal.id, remaining_s=remaining_s)
            ),
            run_tool_call_scope(_run_tool_context(protected, owner_principal_id=principal.id)),
            protected_tool_evidence_scope((protected,)),
        ):
            try:
                await executor.execute(
                    "write.external.email.send",
                    {"to": ALLOWED_RECIPIENT, "body": "hi"},
                    principal=principal,
                )
            except BaseException as exc:  # the raise IS the assertion, either way
                return exc
        return None

    raised = asyncio.run(scenario())
    assert log.calls == []  # the send handler never ran on either path
    return checkpoints, raised


def test_run_park_refuses_loudly_when_a_DECLARED_window_outruns_the_owner_authorization() -> None:
    """A four-hour promise THIS DEPLOYMENT WROTE DOWN is refused AT PARK, and no
    durable wait is created for a continuation that could never run.

    The demo-box defect is the shape: the tier-2 window is 14400 s while the
    pilot realm mints 900 s tokens, so the run parked against a deadline hours
    away and died `authorization_expired` a quarter of an hour later — 10 of 79
    runs on the deployed lineage and 100% of its failures, with the approver's
    queue still showing time left. When that 14400 came from
    `approval_windows.actions`/`.tiers`, somebody configured a promise this
    deployment cannot keep and the loud failure is the right answer: refusing
    here spends no approval row, no checkpoint and no human decision on it.

    ⭐ The floor-route twin below is the SAME park with the SAME numbers and a
    different provenance bit, and it must PARK. Read them together.

    MUTATION-CHECK: delete the `_require_honourable_approval_window(...)` call
    from `_require_canonical_run_approval` (middleware.py, immediately after
    `resolve_approval_deadlines_s`) and this goes RED — the park succeeds,
    `create_wait` records one call and `RunApprovalWaiting` is raised instead of
    `ApprovalWindowUnhonourableError`, which is exactly the silent stranding the
    check exists to stop.
    """

    checkpoints, raised = _park_under_horizon(_SHORT_HORIZON_S, declared=True)
    assert isinstance(raised, ApprovalWindowUnhonourableError), raised
    assert checkpoints.calls == []  # NOTHING durable was created
    assert raised.window_seconds == 14400.0  # the policy promise, named
    assert raised.action == "write.external.email.send"


def test_run_park_on_the_builtin_floor_is_not_refused_and_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The identical park, resolved from `tiers.rego`'s FLOOR, still parks.

    ⛔ THE SHIPPED GUARD REFUSED THIS, and that is what an adversarial review
    proved wrong: the floor is unhonourable against any realistic session
    horizon on EVERY park, so refusing on it converts a 12.7% silent failure
    into a 100% loud one for every action this deployment has not declared a
    window for. A floor is not a promise anyone made.

    Nothing unsafe proceeds. If the approval outruns the horizon the resume seam
    terminalizes `authorization_expired` — the pre-existing, already-handled
    outcome — so the guard records the finding at WARNING and gets out of the
    way. That warning names the action, the resolved window and the owner
    horizon, which is what an operator needs to go and declare one.

    ⚠ MECHANISM, NOT A LIVE ROUTE CLAIM. This drives the floor through the
    canonical Run park because that is where the guard runs; it does not assert
    that any production caller resolves a tier-2 floor by escalation. The
    confirmed production floor route is route 1 — a workflow write leg whose PDP
    answer carries NO window at all, so the `cfg.expiry_s` fallback applies —
    pinned in `test_agentic_ladder_parks_canonical_approval.py`.

    MUTATION-CHECK: make the guard unconditional again (drop the
    `if not declared:` branch in `_require_honourable_approval_window`) and this
    goes RED — `ApprovalWindowUnhonourableError` instead of `RunApprovalWaiting`,
    and `create_wait` records nothing.
    """

    with caplog.at_level("WARNING", logger="ne.runtime.middleware"):
        checkpoints, raised = _park_under_horizon(_SHORT_HORIZON_S)

    assert isinstance(raised, RunApprovalWaiting), raised
    assert len(checkpoints.calls) == 1  # the durable wait REALLY was created
    call = checkpoints.calls[0]
    assert (call["expires_at"] - call["requested_at"]).total_seconds() == 14400.0

    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1, warnings
    assert "write.external.email.send" in warnings[0]
    assert "14400" in warnings[0]
    horizon = int(_PARK_NOW.timestamp()) + _SHORT_HORIZON_S
    assert str(horizon) in warnings[0]


def test_run_park_proceeds_when_the_owner_authorization_covers_the_window() -> None:
    """The check refuses ONLY an unkeepable promise: give the owner more
    authorization than the window and the park is exactly what it always was.

    Without this the "fix" could be a park that never happens, which would take
    the deployment from a 12.7% silent failure rate to a 100% loud one.

    The EXACT-FIT case is asserted too, because equality is the boundary the
    check has to get right in the permissive direction: the delayed-seam guard
    refuses at `now >= context.expires_at`, the same instant the window stops
    promising anything, so a horizon landing exactly on the window's end covers
    every instant the window offers.

    MUTATION-CHECK: invert the early-return comparison in
    `_require_honourable_approval_window` (`>=` to `<`) and this goes RED — the
    honourable park raises `ApprovalWindowUnhonourableError` instead of
    `RunApprovalWaiting` and `create_wait` records no call. Narrow it from `>=`
    to `>` and the exact-fit half below goes RED on its own, rejecting a
    coherent configuration.
    """

    for remaining_s in (14400 + 600, 14400):  # comfortably over, then EXACTLY on
        checkpoints, raised = _park_under_horizon(remaining_s, declared=True)
        assert isinstance(raised, RunApprovalWaiting), (remaining_s, raised)
        assert len(checkpoints.calls) == 1
        call = checkpoints.calls[0]
        assert (call["expires_at"] - call["requested_at"]).total_seconds() == 14400.0


#: Every source tree whose string literals can name a governed action. Read
#: rather than retyped: the two longest names in this repo are module constants
#: (`_TOOL = "write.internal.identity.credential_rotation"`), so a scan that only
#: matched `ToolSpec(name="...")` would miss them — which is precisely how
#: `ACTION_MAX` was sized to a SAMPLE twice, at 32 and then at 38.
_ACTION_SOURCE_ROOTS = ("packages", "tools", "apps")

#: The file types this scan reads. `.py` alone was the same KIND of sampling as
#: the two mis-sized bounds above — a scope chosen from where the names happen
#: to be today rather than from where one could be. TS/TOML/JSON/YAML are here
#: as headroom, not because any of them registers a name now: as of this commit
#: every scanned name outside `deploy/agents/registry.toml` comes from a `.py`
#: file, and the widening buys the scan the ability to see it if that changes.
#: An over-approximation can only make `ACTION_MAX` bigger, and too big is
#: caught by the budget assertion below.
_ACTION_SOURCE_SUFFIXES = (".py", ".pyi", ".ts", ".tsx", ".toml", ".json", ".yaml", ".yml")

#: A dotted lower-snake literal in either quoting style a STRING uses here —
#: `"..."` and `'...'`. The single-quote arm is HEADROOM, exactly like the
#: TS/TOML/JSON/YAML suffixes above, and it reads NOTHING today: drop it and the
#: scan still finds the same 43 names with the same two 43-character maxima.
#: Every match the single-quote arm makes under the scanned roots is in a `.py`
#: file (22 distinct), and nearly all of them are names quoted inside PROSE
#: rather than written as code: tokenizing the 808 scanned `.py`/`.pyi` sources
#: finds only THREE single-quoted string LITERALS whose content is a dotted
#: lower-snake name (`'state.db'`, `'m365.json'`, `'salesforce.json'` — all
#: filenames), because ruff format writes Python literals double-quoted.
#: `_names_a_governed_action` accepts none of the 22; the `.ts`/`.tsx`/`.toml`
#: sources produce no match at all.
#:
#: ⛔ AN EARLIER REVISION OF THIS COMMENT CLAIMED "TOML AND TYPESCRIPT BOTH
#: DEFAULT TO SINGLE QUOTES". Both halves are wrong, and wrong in the OPPOSITE
#: direction — the console pins `"singleQuote": false`
#: (`packages/ne-console/.prettierrc.json`, enforced by CI's `frontend` job
#: running `prettier --check .` over the package; 2710 of 2710 single-line
#: `import … from` statements under `src` are double-quoted, zero single), and
#: TOML's BASIC string is the double-quoted one — which is what all 33
#: `tool_grants` entries in `deploy/agents/registry.toml` are written in.
#:
#: Backticks are EXCLUDED for SCOPE, not because the test would go red without
#: the exclusion. MEASURED: add a backtick to the character class and the scan
#: goes from 43 names to 48, the maximum stays 43, and
#: `test_action_max_is_scanned_from_the_registry_not_retyped` still PASSES. All
#: five additions are RST/Markdown inline literals in comments and docstrings,
#: none of them written as a string anywhere this scan reads —
#: `read.docs.search` (`ne.api.tools`), `read.metrics.query`
#: (`transcript/promotion.ts`), `write.internal.ontology.release`
#: (`ne.brain_ontology.gateway`), `read.py` (a MODULE FILENAME, not an action at
#: all), and the longest of them, `read.kg.orders_per_account_count` at 32 —
#: eleven characters short of the bound. So the exclusion is what keeps the scan
#: measuring what the repo REGISTERS rather than what it writes ABOUT; it does
#: not move `ACTION_MAX` in either direction on today's repo.
_DOTTED_LITERAL = re.compile(r"[\"']([a-z0-9_]+(?:\.[a-z0-9_]+)+)[\"']")


def _names_a_governed_action(
    candidate: str, rules: tuple[tuple[str, ...], tuple[str, ...]]
) -> bool:
    """Could `tiers.rego` classify this literal as an action at all?

    ⛔ THE PREFIX LIST USED TO BE TYPED HERE (`read|write|draft|finance|legal`)
    and called "deliberately wider" than the actions that can reach the refusal.
    It was NARROWER. `tiers.rego` also classifies by INFIX —
    `contains(input.action, ".pii.")`, tier 2, under ANY prefix — so a 49-char
    `export.pii.…` name, which `tiers.rego` classifies tier 2 and the bundle
    answers `require-approval` for, matched nothing here and left the bound
    sized to a sample for the third time. The
    grammar is now READ from the policy, all tiers, via
    `test_approval_window_is_honourable.action_classification_rules`.

    All tiers, not just 2-and-up, and that breadth is the point: route 1 reaches
    the guard with a TIER-1 name (`write.internal.crm.note`, escalated to the
    tier-2 floor by `_effective_tier` after the PDP has answered), and one of
    the two 43-character maxima — `write.internal.identity.credential_rotation`
    — is itself a `write.internal.` name. An over-approximation can only make
    `ACTION_MAX` bigger, and being too big is caught by the budget assertion
    below rather than by silent truncation.

    `rules` is passed in rather than re-derived per candidate: the policy is
    read once per scan, not once per string literal in the repo.
    """

    prefixes, infixes = rules
    return any(candidate.startswith(prefix) for prefix in prefixes) or any(
        infix in candidate for infix in infixes
    )


def _registered_action_names() -> dict[str, str]:
    """Every statically-written action name in the repo, mapped to where it lives.

    Sources: string literals in non-test package/tool sources, plus the
    `tool_grants` of every agent in `deploy/agents/registry.toml` — the registry
    matters because a grant is what lets an agent reach a park at all, and
    `read.kg.overdue_provisional_credit_disputes` (43) is held there.
    """

    found: dict[str, str] = {}
    rules = action_classification_rules()
    for base in _ACTION_SOURCE_ROOTS:
        root = _REPO_ROOT / base
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix not in _ACTION_SOURCE_SUFFIXES:
                continue
            if {"tests", "test"} & set(path.parts) or "node_modules" in path.parts:
                continue
            # `.test.ts` / `_test.py` live BESIDE their subject rather than under
            # a test directory, and the console's fixtures live under `src/test/`
            # (singular), so neither is caught by a `tests`-only directory check.
            if ".test." in path.name or path.stem.endswith("_test"):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for match in _DOTTED_LITERAL.finditer(text):
                candidate = match.group(1)
                if _names_a_governed_action(candidate, rules):
                    found.setdefault(candidate, str(path.relative_to(_REPO_ROOT)))
    registry = tomllib.loads((_REPO_ROOT / "deploy" / "agents" / "registry.toml").read_text())
    for agent in registry["agents"]:
        for grant in agent.get("tool_grants", ()):
            found.setdefault(str(grant), "deploy/agents/registry.toml")
    return found


def test_action_max_is_scanned_from_the_registry_not_retyped() -> None:
    """`ACTION_MAX` equals the longest action this repo statically registers.

    ⛔ THIS BOUND HAS NOW BEEN WRONG TWICE, both times because it was sized to a
    SAMPLE while claiming to be sized to the repo. It was 32 ("the longest is
    `write.external.email.send`", 25), then 38 ("the longest is
    `write.internal.network.egress.restrict`"), while
    `write.internal.identity.credential_rotation` (43) and
    `read.kg.overdue_provisional_credit_disputes` (43, and a real `tool_grants`
    entry on `agt_01k3overduecreditreview00000`) were already registered. So the
    number is no longer typed in this test either: it is SCANNED, and the
    constant must equal what the scan finds.

    43 is simultaneously the maximum and the CEILING — the rendered sentence at
    43 characters with an eight-digit window is exactly `SAFE_REASON_MAX` — so a
    44-character action is not a reason to raise the bound; it is a reason to
    shorten the sentence. Both directions are asserted.

    MUTATION-CHECKS:

    * set `ACTION_MAX` to 42 (or back to 38) and the equality goes RED, naming
      the actions that would be truncated. So does 44: the constant must EQUAL
      the scan, in both directions;
    * lengthen the sentence in `ApprovalWindowUnhonourableError.__init__` by one
      word and the BUDGET assertion goes RED — that is the assertion 43 as a
      ceiling rests on, and it is the one the old `len(safe_reason) <=
      SAFE_REASON_MAX` check could not make, since `__init__` had already sliced
      to exactly that bound;
    * delete BOTH 43s from every place the repo writes them —
      `write.internal.identity.credential_rotation` (`ne.worker
      .credential_rotation`, `ne.worker.testing`, `ne.api.platform`,
      `ne.cli.agent_transport`) and `read.kg.overdue_provisional_credit_disputes`
      (`ne.api.workflows`, `ne.runtime.workflow_execution_profile`, and the
      `tool_grants` in `deploy/agents/registry.toml`) — and the scan drops to 41,
      so the equality reds. ⛔ Deleting ONE site does NOT red it, and an earlier
      revision of this bullet claimed it would: each name is written at three or
      four of those places and the registry arm re-adds the second one
      regardless. The scan is reading the whole repo, not one file of it;
    * add `_PROBE = "export.pii.customer_records_bulk_download_request"` (49
      characters; `tiers.rego` classifies it tier 2 on the `.pii.` INFIX alone,
      under a prefix no class list mentions) to any scanned source and the
      equality reds, naming it. Under the retyped `read|write|draft|finance|
      legal` prefix regex this test stayed GREEN — that is the sample this
      round removed.
    """

    registered = _registered_action_names()
    longest = max(len(name) for name in registered)
    at_maximum = sorted(name for name in registered if len(name) == longest)
    assert longest == ApprovalWindowUnhonourableError.ACTION_MAX, (
        f"ACTION_MAX is {ApprovalWindowUnhonourableError.ACTION_MAX} but this repo "
        f"registers {at_maximum} at {longest} characters — the published sentence would "
        "truncate the action name it exists to identify"
    )

    # THE BUDGET, computed WITHOUT the constructor's own slice. `safe_reason` is
    # already cut to SAFE_REASON_MAX in `__init__`, so asserting its length
    # against that bound can never fail and proves nothing; the overhead is
    # measured from a construction that is far too short to be sliced.
    probe = ApprovalWindowUnhonourableError(action="a", window_seconds=99_999_999.0)
    overhead = len(probe.safe_reason) - len(repr("a")) - len("99999999")
    budget = overhead + len(repr("Z" * longest)) + len("99999999")
    assert budget <= ApprovalWindowUnhonourableError.SAFE_REASON_MAX, (
        f"the rendered sentence is {budget} characters at ACTION_MAX with an eight-digit "
        f"window, past the {ApprovalWindowUnhonourableError.SAFE_REASON_MAX} cap — the "
        "actionable tail would be dropped; shorten the sentence rather than raise the bound"
    )

    # And every longest-registered action really does survive whole, through the
    # real constructor, with the actionable tail intact.
    for action in at_maximum:
        error = ApprovalWindowUnhonourableError(action=action, window_seconds=99_999_999.0)
        assert action in error.safe_reason
        assert error.safe_reason.endswith("Configure this action's approval window to fit.")


def test_the_refusal_publishes_a_named_reason_and_never_the_owner_horizon() -> None:
    """The run says WHAT is misconfigured, and discloses nothing from which the
    owner's token expiry can be derived.

    Both halves are load-bearing. Without a published reason the reader gets the
    derived floor — "the failing step published no explanation. Recorded failure
    class: ..." — which is the sentence the deployed box shows today and which
    names no cause. And the first cut of this check published
    `overrun_seconds = window - remaining`, which makes the owner's exact token
    expiry solvable from public policy config; the window and the action are
    public deployment policy, the horizon is not.

    MUTATION-CHECK: append the overrun to the sentence in
    `ApprovalWindowUnhonourableError.__init__` (e.g. `f" Overrun:
    {seconds - 873}s."`) and this goes RED on the derivability assertion; drop
    the `safe_reason` assignment and it goes RED on the published-verbatim
    assertion (`published_failure_reason` falls through to the derived floor).
    """

    from ne.runtime.run_finalizer import derived_failure_reason, published_failure_reason

    _checkpoints, raised = _park_under_horizon(_SHORT_HORIZON_S, declared=True)
    assert isinstance(raised, ApprovalWindowUnhonourableError), raised
    reason = raised.safe_reason

    published = published_failure_reason(safe_reason=reason, failure_class=type(raised).__name__)
    assert published == reason  # verbatim, NOT the derived floor
    assert published != derived_failure_reason(type(raised).__name__)

    # It names the promise and the action — both public deployment policy.
    assert "14400" in reason
    assert "write.external.email.send" in reason

    # It names nothing the horizon can be solved from.
    horizon = int(_PARK_NOW.timestamp()) + _SHORT_HORIZON_S
    for leaked in (str(horizon), str(_SHORT_HORIZON_S), str(14400 - _SHORT_HORIZON_S)):
        assert leaked not in reason, f"the published reason discloses {leaked}"

    # Bounded and servable exactly where every other safe reason is bounded.
    assert len(reason) <= ApprovalWindowUnhonourableError.SAFE_REASON_MAX
    assert all(character.isprintable() for character in reason)
