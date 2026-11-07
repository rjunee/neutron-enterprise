# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""U3/F2: the REAL agentic ladder, through the REAL canonical approval gate.

WHY THIS FILE EXISTS. Every approval end-to-end test in the repo drives an
adapter that calls ``checkpoints.create_wait(...)`` itself and raises
``RunApprovalWaiting`` by hand, fabricating the two fields the gate is supposed
to compute (``stamp_receipt_id=f"stamp-{run_id}"``,
``stamp_receipt_hash="f" * 64``). ``_require_canonical_run_approval`` is
therefore never entered by any of them, and the carrier-population path in
``AgenticPlanner`` was never exercised with MORE THAN ONE reference anywhere in
the repo. That is why live run ``7f782fe0`` died at ``_run_source_reference``
with no park and no ``ne_approval_requests`` row while CI stayed green.

So nothing here is stubbed on the path under test. Real ``AgenticPlanner`` (via
``run_model_planned_agent``) -> real ``ReceiptGatedToolExecutor`` over a real
audit chain -> real ``GovernedToolExecutor`` -> real ``DurableApprovalGate`` ->
real ``RunApprovalCheckpointStore`` over real SQLite stores. The only scripted
thing is the model itself, which is the one component a test must supply.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa

from ne.audit import PostgresAuditSink, ensure_audit_schema
from ne.contracts import get_config_hash
from ne.contracts.identity import AuthenticatedRequestContext
from ne.contracts.runtime import ToolSpec
from ne.contracts.source_authority import (
    CurrentSourceAuthorityReceipt,
    M365SourcePoint,
    SourceAuthorityDecision,
    SourceAuthorityStamp,
    SourceAuthorityState,
    SourceBoundModelSendResolution,
    SourceBoundToolResultRef,
    SourceContentArtifactIdentity,
    SourceContractIdentity,
    SourceIdentity,
    canonical_source_authority_stamps,
)
from ne.contracts.types import ApprovalTier
from ne.govern import create_guardrail_engine
from ne.govern.config import (
    ArgSpec,
    GovernSettings,
    GuardrailSettings,
    RateLimitRule,
    RateLimitSettings,
    RecipientSettings,
    ToolArgKind,
    ToolArgPolicy,
)
from ne.runtime.approval_store import SqlAlchemyApprovalStore, ensure_approval_schema
from ne.runtime.approvals import DurableApprovalGate
from ne.runtime.middleware import _evidence_set_receipt, write_approval_floor_scope
from ne.runtime.private_payloads import RunPrivatePayloadCipher, StaticPrivatePayloadKeys
from ne.runtime.run_audit_receipts import RunAuditReceiptSink, RunAuditTarget
from ne.runtime.run_checkpoint import (
    RunApprovalCheckpointStore,
    RunApprovalWaiting,
    ne_agent_run_continuation_checkpoints,
)
from ne.runtime.run_finalizer import RunTurnTarget
from ne.runtime.run_runner import ReceiptGatedToolExecutor
from ne.runtime.run_store import AgentRunStore, RunState, ensure_run_schema, ne_agent_runs
from ne.runtime.run_workflow import RunWorkflowStateStore, ensure_run_workflow_schema
from ne.runtime.runner import run_model_planned_agent
from ne.runtime.source_results import (
    ProtectedSourceResultGuard,
    authenticated_request_scope,
    encode_protected_reference,
    model_call_scope,
    protected_tool_evidence_scope,
)
from ne.runtime.store import SqlAlchemySessionStore, ensure_runtime_schema
from ne.runtime.tools import ToolBinding

from .conftest import (
    ALLOWED_RECIPIENT,
    ScriptedGateway,
    approval_settings,
    make_executor,
    make_principal,
    text_completion,
    tool_call_completion,
)

_OWNER = "alice"

#: Route 1's action, spelled exactly as `ne.api.workflows.CRM_NOTE_TOOL`.
_CRM_NOTE_TOOL = "write.internal.crm.note"

#: The authorized text the guard delivers, and an answer that QUOTES it. A run
#: that was served protected material must quote it back (NE-SPAN-BIND); the
#: second test below runs to completion, so it needs a contract-valid answer.
_CHUNK_SENTENCE = "The servicing note records a hardship credit posted on May ninth."
_QUOTING_ANSWER = (
    'The record is explicit. The source states: "The servicing note records a '
    'hardship credit posted on May ninth." That resolves the dispute.'
)


def _structured_completion(final_text: str) -> Any:
    """A final reply in the run's declared output contract."""

    return text_completion(json.dumps({"final_text": final_text}))


def _stamped_reference(*, reference_id: str, item_id: str) -> SourceBoundToolResultRef:
    """One stamped Tool-result reference bound to its OWN source item.

    Each read of the ladder must carry a DISTINCT stamp, otherwise the parked
    union canonicalizes down to a single member and a one-source park would be
    indistinguishable from the real multi-source one.
    """

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
        receipt_id=f"receipt-{item_id}",
        source_identity=source,
        artifact_identity=artifact,
        source_revision="etag-a",
        source_contract=contract,
        contract_epoch=4,
        acl_snapshot_hash=artifact.acl_snapshot_hash,
        state=SourceAuthorityState.PRESENT,
        admitted_at=datetime(2026, 8, 28, 12, tzinfo=UTC),
        authority_as_of=datetime(2026, 8, 28, 11, 59, tzinfo=UTC),
        audit_event_id=f"audit-{item_id}",
        lineage_id=f"lineage-{item_id}",
        freshness_sla_seconds=Decimal("900"),
    )
    return SourceBoundToolResultRef(
        reference_id=reference_id,
        stamps=(
            SourceAuthorityStamp(
                source_identity=source,
                artifact_identity=artifact,
                selected_head_revision=receipt.source_revision,
                acl_snapshot_hash=receipt.acl_snapshot_hash,
                source_contract=contract,
                contract_epoch=receipt.contract_epoch,
                authority_receipt_id=receipt.receipt_id,
                authority_receipt_hash=receipt.receipt_hash,
            ),
        ),
        result_hash=hashlib.sha256(reference_id.encode()).hexdigest(),
    )


#: The ladder: three governed reads, three distinct sources, three distinct stamps.
_LADDER = (
    _stamped_reference(reference_id="protected-graph-path", item_id="item-graph-path"),
    _stamped_reference(reference_id="protected-loan-row", item_id="item-loan-row"),
    _stamped_reference(reference_id="protected-servicing-note", item_id="item-servicing-note"),
)
_LADDER_ENCODED = tuple(encode_protected_reference(reference) for reference in _LADDER)


class _AuthorizingResolver:
    """Authorizes every reference, echoing its own stamps -- the honest case."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    async def for_model_send(self, reference: Any, **kwargs: Any) -> SourceBoundModelSendResolution:
        self.seen.append(reference.reference_id)
        return SourceBoundModelSendResolution(
            decision=SourceAuthorityDecision.AUTHORIZED,
            materialized_text=_CHUNK_SENTENCE,
            stamps=reference.stamps,
            run_id=kwargs["run_id"],
            model_call_attempt_id=kwargs["model_call_attempt_id"],
        )


class _RevokingResolver(_AuthorizingResolver):
    """Authorizes everything EXCEPT one named reference (U3/F3, ladder-level)."""

    def __init__(self, revoked_reference_id: str) -> None:
        super().__init__()
        self.revoked = revoked_reference_id

    async def for_model_send(self, reference: Any, **kwargs: Any) -> SourceBoundModelSendResolution:
        self.seen.append(reference.reference_id)
        if reference.reference_id != self.revoked:
            return await super().for_model_send(reference, **kwargs)
        return SourceBoundModelSendResolution(
            decision=SourceAuthorityDecision.PERMANENT_WITHHELD,
            materialized_text=None,
            stamps=(),
            run_id=kwargs["run_id"],
            model_call_attempt_id=kwargs["model_call_attempt_id"],
        )


class _LadderTools:
    """The tool surface: one governed read serving the ladder, one Tier-2 send.

    The read reuses the conftest's registered ``read.docs.search`` name so the
    real guardrail engine and the Rego-faithful PDP admit it unchanged; each
    successive call serves the NEXT protected reference, which is what makes
    this a multi-read ladder rather than one read repeated.
    """

    def __init__(self) -> None:
        self.reads: list[dict[str, Any]] = []
        self.sends: list[dict[str, Any]] = []
        self.notes: list[dict[str, Any]] = []

    async def _read(self, args: Any) -> str:
        # Cycles, so a SECOND run over the same stack re-serves the same ladder.
        # The F1 successor suite drives two runs against one stack and needs the
        # second to produce the identical evidence set — a successor whose
        # evidence differed would not be the same effect at all.
        self.reads.append(dict(args))
        return _LADDER_ENCODED[(len(self.reads) - 1) % len(_LADDER_ENCODED)]

    async def _send(self, args: Any) -> str:
        self.sends.append(dict(args))
        return "sent"

    async def _note(self, args: Any) -> str:
        self.notes.append(dict(args))
        return "noted"

    def bindings(self) -> list[ToolBinding]:
        return [
            ToolBinding(
                spec=ToolSpec(
                    name="read.docs.search",
                    description="Search internal docs.",
                    tier=ApprovalTier.TIER_0,
                    input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
                ),
                handler=self._read,
            ),
            ToolBinding(
                spec=ToolSpec(
                    name="write.external.email.send",
                    description="Send an external email.",
                    tier=ApprovalTier.TIER_2,
                    input_schema={
                        "type": "object",
                        "properties": {"to": {"type": "string"}, "body": {"type": "string"}},
                    },
                ),
                handler=self._send,
                extract_recipients=lambda a: (str(a["to"]),) if a.get("to") else (),
            ),
            # ROUTE 1's action, at the tier PRODUCTION declares it at. Tier 1 is
            # load-bearing: it is what makes the PDP answer `allow` with a NULL
            # window, so the park resolves `cfg.expiry_s` and the guard sees no
            # policy window at all. Registering it at tier 2 here would test a
            # different route.
            ToolBinding(
                spec=ToolSpec(
                    name=_CRM_NOTE_TOOL,
                    description="Attach a note to a CRM record.",
                    tier=ApprovalTier.TIER_1,
                    input_schema={
                        "type": "object",
                        "properties": {
                            "record_id": {"type": "string"},
                            "body": {"type": "string"},
                        },
                    },
                ),
                handler=self._note,
            ),
        ]


class _Stack:
    """Every real component of the run, assembled once."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        guardrails_for: Any = None,
        owner_expires_at: int = 4_102_444_800,
    ) -> None:
        #: The run owner's stored authorization horizon. MUTABLE on purpose: the
        #: F1 successor suite parks one run under a short-lived login and then
        #: admits the next under a FRESH one, which is the whole shape of
        #: "only a new authenticated retry may run".
        self.owner_expires_at = owner_expires_at
        self.config_hash = get_config_hash()
        self.runtime = sa.create_engine(f"sqlite:///{tmp_path / 'runtime.db'}")
        self.audit_engine = sa.create_engine(f"sqlite:///{tmp_path / 'audit.db'}")
        ensure_runtime_schema(self.runtime)
        ensure_run_schema(self.runtime)
        ensure_run_workflow_schema(self.runtime)
        ensure_approval_schema(self.runtime)
        ensure_audit_schema(self.audit_engine)
        self.runs = AgentRunStore(
            self.runtime,
            RunPrivatePayloadCipher(
                StaticPrivatePayloadKeys(current_key_id="k1", keys={"k1": b"k" * 32})
            ),
        )
        self.sessions = SqlAlchemySessionStore(self.runtime)
        self.approvals = SqlAlchemyApprovalStore(self.runtime)
        self.sink = PostgresAuditSink(self.audit_engine)
        self.gate = DurableApprovalGate(
            store=self.approvals, sink=self.sink, settings=approval_settings()
        )
        # The REAL checkpoint store -- this is the object every other approval
        # end-to-end test reaches around by calling create_wait itself.
        self.gate._run_approval_checkpoints = RunApprovalCheckpointStore(  # type: ignore[attr-defined]
            self.runs, self.approvals
        )
        self.tools = _LadderTools()
        self.principal = make_principal(_OWNER)
        self.governed = make_executor(
            self.tools.bindings(),
            self.sink,
            approval_gate=self.gate,
            guardrails=None if guardrails_for is None else guardrails_for(self.sink),
        )

    async def executor(self) -> ReceiptGatedToolExecutor:
        conversation = await self.sessions.create_conversation("agent-a", "finance", _OWNER)
        accepted = await self.runs.admit_start(
            SimpleNamespace(
                idempotency_key=f"start-{uuid.uuid4()}",
                request_hash=uuid.uuid4().hex * 2,
                expected_version=0,
                input_plaintext=b"protected prompt",
                conversation_id=conversation.conversation_id,
                owner_principal_id=_OWNER,
                department="finance",
                conversation_agent_name="agent-a",
                agent_id="agent-a",
                agent_version=1,
                strategy="agentic",
                config_hash=self.config_hash,
                capability_snapshot_hash="2" * 64,
                capability_snapshot_receipt_id="snapshot-receipt",
                authenticated_context=AuthenticatedRequestContext(
                    principal_id=_OWNER,
                    authorization_groups=(),
                    department="finance",
                    issuer="https://issuer.example.test",
                    expires_at=self.owner_expires_at,
                ),
            )
        )
        RunWorkflowStateStore(self.runtime, run_store=self.runs).claim(
            dispatch_id=accepted.enqueue_id,
            run_id=accepted.run_id,
            owner_principal_id=_OWNER,
            config_hash=self.config_hash,
        )
        run = await self.runs.get_run(run_id=accepted.run_id, owner_principal_id=_OWNER)
        assert run is not None
        self.run = run
        self.attempt_id = accepted.attempt_id
        target = RunTurnTarget(
            conversation_id=run.conversation_id,
            run_id=run.run_id,
            turn_id=f"turn-{run.run_id}",
            turn_sequence=1,
            attachment_id=f"attachment-{run.run_id}",
            attempt_id=accepted.attempt_id,
        )
        receipts = RunAuditReceiptSink(
            self.sink,
            audit_engine=self.audit_engine,
            runtime_engine=self.runtime,
            run_store=self.runs,
            target=RunAuditTarget(
                run_id=run.run_id, attempt_id=accepted.attempt_id, step_id="run-tool"
            ),
            config_hash=self.config_hash,
        )
        # Production's exact nesting (run_workflow.py:2525): the receipt gate is
        # OUTSIDE the governed executor, and the planner is outside both.
        return ReceiptGatedToolExecutor(
            self.governed,
            audit=receipts,
            run=run,
            target=target,
            owner_principal_id=_OWNER,
            step_id="run-tool",
        )

    def dispose(self) -> None:
        self.runtime.dispose()
        self.audit_engine.dispose()


def _ladder_plan(reads: int, *, final: Any = None) -> ScriptedGateway:
    """A model plan: ``reads`` governed reads, then the Tier-2 send.

    ``final`` is appended only for the run that is EXPECTED to continue past the
    send. The parking run never reaches it -- ``RunApprovalWaiting`` leaves the
    loop at the effect -- and supplying one there would hide a missing park.
    """

    return ScriptedGateway(
        [
            *(
                tool_call_completion(
                    "read.docs.search",
                    {"q": f"hop-{hop}", "step_intent": f"Read ladder hop {hop}."},
                )
                for hop in range(reads)
            ),
            tool_call_completion(
                "write.external.email.send",
                {
                    "to": ALLOWED_RECIPIENT,
                    "body": "the evidenced summary",
                    "step_intent": "Send the evidenced summary.",
                },
            ),
            *(() if final is None else (final,)),
        ]
    )


async def _drive(
    stack: _Stack,
    gateway: ScriptedGateway,
    guard: ProtectedSourceResultGuard,
    *,
    max_parked_writes: int = 1,
) -> Any:
    """Drive one governed run.

    ``max_parked_writes`` defaults to the single write every test here plans.
    The F1 successor suite raises it to plan TWO gated writes in one run, which
    is the shape that reaches ``RunApprovalContinuationExhaustedError``.
    """
    executor = await stack.executor()
    with (
        authenticated_request_scope(
            AuthenticatedRequestContext(
                principal_id=_OWNER,
                authorization_groups=(),
                department="finance",
                issuer="https://issuer.example.test",
                expires_at=stack.owner_expires_at,
            )
        ),
        model_call_scope(run_id=stack.run.run_id, model_call_attempt_id=stack.attempt_id),
    ):
        return await run_model_planned_agent(
            task="answer from the governed record",
            instructions="use governed tools",
            principal=stack.principal,
            executor=executor,
            gateway=gateway,
            settings=approval_settings(),
            sink=stack.sink,
            max_tool_calls=6,
            total_tokens_limit=100_000,
            wall_clock_seconds=30.0,
            max_parked_writes=max_parked_writes,
            source_guard=guard,
        )


def test_real_agentic_ladder_parks_a_canonical_run_approval(tmp_path: Path) -> None:
    """THE test whose absence caused the outage: a >=2-read ladder into a REAL park.

    Nothing on the path is stubbed. The planner populates the evidence carrier
    from its own server-observed served set, the receipt-gated executor
    accumulates the authoritative ``served_protected_refs``, and
    ``_require_canonical_run_approval`` -- entered for real, by this test alone
    -- computes the evidence-set binding and parks the wait.

    The assertions are the ones the hand-written parks could never make: the
    binding is the COMPLETE three-source set, not one read's, and it is the
    value the gate computed rather than a fabricated ``f"stamp-{run_id}"``.
    """

    stack = _Stack(tmp_path)
    resolver = _AuthorizingResolver()
    gateway = _ladder_plan(reads=len(_LADDER))

    async def scenario() -> Any:
        with pytest.raises(RunApprovalWaiting) as raised:
            await _drive(stack, gateway, ProtectedSourceResultGuard(resolver))  # type: ignore[arg-type]
        return raised.value.joined, await stack.approvals.list_pending()

    try:
        joined, pending = asyncio.run(scenario())

        # The ladder really ran, and the guarded effect really did not.
        assert len(stack.tools.reads) == len(_LADDER)
        assert stack.tools.sends == []

        # A REAL parked approval row -- the thing live run 7f782fe0 never got.
        assert [row.request_id for row in pending] == [joined.approval_id]
        assert pending[0].action == "write.external.email.send"
        assert int(pending[0].tier) == 2

        with stack.runtime.connect() as conn:
            checkpoint = conn.execute(sa.select(ne_agent_run_continuation_checkpoints)).one()
            run_status = conn.execute(sa.select(ne_agent_runs.c.status)).scalar_one()
        assert run_status == RunState.WAITING_APPROVAL.value
        assert checkpoint.status == "pending"

        # THE COMPLETE SET. The binding is the gate's own deterministic digest
        # over ALL THREE reads -- not one read's reference_id, and emphatically
        # not the fabricated `stamp-{run_id}` every other approval test asserts.
        ordered = tuple(sorted(_LADDER, key=lambda reference: reference.reference_id))
        expected_id, expected_hash = _evidence_set_receipt(ordered)
        assert checkpoint.stamp_receipt_id == expected_id
        assert checkpoint.stamp_receipt_hash == expected_hash
        assert checkpoint.stamp_receipt_id.startswith("evidence-set:")
        assert checkpoint.stamp_receipt_id != f"stamp-{stack.run.run_id}"
        assert checkpoint.stamp_receipt_hash != "f" * 64

        # And the parked authority is the UNION: one distinct stamp per read.
        parked = canonical_source_authority_stamps(
            tuple(stamp for reference in _LADDER for stamp in reference.stamps)
        )
        assert len(parked) == len(_LADDER)
        assert {name for name in resolver.seen} == {reference.reference_id for reference in _LADDER}
    finally:
        stack.dispose()


def test_real_agentic_ladder_withholds_when_a_non_first_source_is_revoked(
    tmp_path: Path,
) -> None:
    """U3/F3 at ladder level: revoke the LAST read's authority, get no park at all.

    The pre-effect re-authorization covers every member of the union, so
    revoking the third read withholds the effect even though reads one and two
    are still perfectly authorized. No approval row is parked, because an effect
    resting on revoked evidence must never reach a human's queue in the first
    place. An implementation that re-checked only ``references[0]`` would park
    here instead.
    """

    stack = _Stack(tmp_path)
    revoked = _LADDER[-1].reference_id
    resolver = _RevokingResolver(revoked)

    gateway = _ladder_plan(reads=len(_LADDER), final=_structured_completion(_QUOTING_ANSWER))

    async def scenario() -> Any:
        guard = ProtectedSourceResultGuard(resolver)  # type: ignore[arg-type]
        result = await _drive(stack, gateway, guard)
        return result, await stack.approvals.list_pending()

    try:
        result, pending = asyncio.run(scenario())
        # The write was REFUSED before any effect, by the pre-effect union recheck.
        refusals = [step.outcome for step in result.steps if step.tool.startswith("write.")]
        assert refusals == ["refused:SourceAuthorityWithheldError"], result.steps
        assert stack.tools.sends == []  # the effect never happened
        assert list(pending) == []  # and nothing was parked for a human to approve
        assert revoked in resolver.seen  # the NON-FIRST member really was consulted
        with stack.runtime.connect() as conn:
            assert conn.execute(sa.select(ne_agent_run_continuation_checkpoints)).all() == []
    finally:
        stack.dispose()


# --- ROUTE 1: the workflow write leg, and why the guard must not refuse it ---


def _route_one_guardrails(sink: Any) -> Any:
    """The real guardrail engine, allowlisting route 1's action.

    The conftest's shared settings allowlist four canonical tools and this is a
    fifth, so the engine is built here rather than by widening a fixture every
    other module in this package depends on. It is the REAL engine with a REAL
    arg policy — only the allowlist differs.
    """

    return create_guardrail_engine(
        GovernSettings(
            guardrails=GuardrailSettings(
                recipients=RecipientSettings(allowed_recipients=(ALLOWED_RECIPIENT,)),
                rate=RateLimitSettings(default=RateLimitRule(max_calls=1000, window_s=3600.0)),
                tools={
                    "read.docs.search": ToolArgPolicy(args={"q": ArgSpec(kind=ToolArgKind.text)}),
                    "write.external.email.send": ToolArgPolicy(
                        args={
                            "to": ArgSpec(kind=ToolArgKind.email),
                            "body": ArgSpec(kind=ToolArgKind.text),
                        }
                    ),
                    _CRM_NOTE_TOOL: ToolArgPolicy(
                        args={
                            "record_id": ArgSpec(kind=ToolArgKind.identifier),
                            "body": ArgSpec(kind=ToolArgKind.text),
                        }
                    ),
                },
            )
        ),
        sink=sink,
    )


def test_a_workflow_write_leg_parks_instead_of_being_refused(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """ROUTE 1, end to end: a tier-1 write escalated by the workflow's own floor
    scope, executed through a REAL `ReceiptGatedToolExecutor`, PARKS.

    ⛔ THIS IS THE ROUTE THE SHIPPED GUARD BROKE. `ne.api.workflows` runs each
    subject's write leg inside a `write_approval_floor_scope` at
    `SUBJECT_WRITE_GATE_TIER`, and those legs DO execute as canonical Run parks
    — the `ReceiptGatedToolExecutor` is `RunExecutionContext.executor` and the
    execution seam forwards it — so `_require_honourable_approval_window` fires
    on them. Under an unconditional check every one of them is refused, while on
    `main` they park and succeed.

    ⭐ AND THE MECHANISM IS WHY NO CONFIGURATION CAN RESCUE THEM. The ToolSpec
    declares TIER 1, `middleware.py` threads `{"tier": int(spec.tier)}` into the
    PDP input, so the PDP is asked about a tier-1 action and answers `allow` with
    `approval_window_seconds: null`. The tier-2 floor is applied AFTERWARDS, by
    `_effective_tier`, which the PDP never sees. So the window here is the
    `ApprovalSettings.expiry_s` FALLBACK — no policy window exists to declare,
    and no `deploy/opa/data/ne/config/data.json` entry can intercept this chain,
    because the chain never consults the document. "Declare a window for every
    parkable action" was never an available fix for this route.

    The owner horizon is deliberately SHORTER than the fallback window, so the
    guard's comparison really does fail here; what must not happen is a refusal.

    MUTATION-CHECK: make the guard unconditional (drop the `if not declared:`
    branch in `_require_honourable_approval_window`) and this goes RED with
    `ApprovalWindowUnhonourableError` and an empty approval queue.
    """

    stack = _Stack(tmp_path, guardrails_for=_route_one_guardrails)

    # 900 s of owner authorization against `approval_settings()`'s 1000 s
    # fallback expiry: the promise outruns the horizon by 100 s.
    horizon_s = 900
    fallback_expiry_s = float(approval_settings().approvals.expiry_s)
    assert fallback_expiry_s > horizon_s, "the premise (window outruns horizon) is gone"

    async def scenario() -> Any:
        executor = await stack.executor()
        owner_context = AuthenticatedRequestContext(
            principal_id=_OWNER,
            authorization_groups=(),
            department="finance",
            issuer="https://issuer.example.test",
            expires_at=int(datetime.now(tz=UTC).timestamp()) + horizon_s,
        )
        with (
            authenticated_request_scope(owner_context),
            model_call_scope(run_id=stack.run.run_id, model_call_attempt_id=stack.attempt_id),
        ):
            # One governed read first: a Tier-2 park binds the COMPLETE served
            # evidence set, and a park with no server-observed evidence is
            # refused by `_run_source_references` for an unrelated reason.
            await executor.execute(
                "read.docs.search", {"q": "the subject"}, principal=stack.principal
            )
            served = executor.served_protected_refs
            assert served, "the read served no protected reference; the premise is gone"
            with (
                protected_tool_evidence_scope(served),
                write_approval_floor_scope(ApprovalTier.TIER_2),
            ):
                try:
                    await executor.execute(
                        _CRM_NOTE_TOOL,
                        {"record_id": "0015g00000AbCdEf", "body": "the evidenced summary"},
                        principal=stack.principal,
                    )
                except BaseException as exc:  # the raise IS the assertion
                    return exc, owner_context
        return None, owner_context

    try:
        with caplog.at_level("WARNING", logger="ne.runtime.middleware"):
            raised, owner_context = asyncio.run(scenario())

        assert isinstance(raised, RunApprovalWaiting), raised
        assert stack.tools.notes == []  # the effect itself did NOT run
        pending = asyncio.run(stack.approvals.list_pending())
        assert [row.action for row in pending] == [_CRM_NOTE_TOOL]
        assert int(pending[0].tier) == 2  # the floor scope really did escalate it

        # The durable row carries the FALLBACK expiry — proof the PDP named no
        # window on this route, which is the whole reason it cannot be declared.
        row = pending[0]
        assert (row.expires_at - row.requested_at).total_seconds() == fallback_expiry_s

        # And the finding was recorded rather than swallowed.
        warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
        matching = [w for w in warnings if _CRM_NOTE_TOOL in w]
        assert len(matching) == 1, warnings
        assert str(int(owner_context.expires_at)) in matching[0]
    finally:
        stack.dispose()
