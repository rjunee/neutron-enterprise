# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Atomic Run-bound approval continuation checkpoints.

This is deliberately separate from the standalone approval gate.  A canonical
Run approval is useful only when its protected invocation, approval request,
safe event, continuation checkpoint, exact approval join, waiting Run state and
payload-free audit intent become visible in one database commit.
"""

from __future__ import annotations

import hmac
import json
import math
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict

from ne.contracts.source_authority import ProtectedSourceInvocationRef, SourceAuthorityStamp
from ne.events import append
from ne.runtime.approval_store import (
    PENDING_STATUSES,
    SqlAlchemyApprovalStore,
    ne_approval_evidence,
    ne_approval_requests,
)
from ne.runtime.errors import (
    ApprovalSourceWithheldFromApproverError,
    RunApprovalAuthorityRetryableError,
    RunApprovalContinuationExhaustedError,
)
from ne.runtime.private_payloads import (
    PrivatePayloadDomain,
    protected_payload_aad_binding,
    protected_target_hash,
)
from ne.runtime.run_store import (
    AgentRunStore,
    RunState,
    _envelope_values,
    _hash,
    _id,
    _pack_source_bound_payload,
    _stamp_set_hash,
    ne_agent_protected_source_invocations,
    ne_agent_run_attempt_links,
    ne_agent_runs,
)
from ne.runtime.source_results import ProtectedSourceResultGuard
from ne.runtime.store import metadata
from ne.schema import ApprovalEvent

__all__ = [
    "STANDING_APPROVAL_REASON",
    "STANDING_LAPSED_STATUS",
    "RunApprovalCheckpointStore",
    "RunApprovalJoin",
    "RunApprovalResumeRequest",
    "RunApprovalWaiting",
    "RunContinuationCheckpoint",
    "StandingApproval",
    "StandingDecision",
    "expire_canonical_run_approval_in_transaction",
    "ne_agent_run_approval_audit_outbox",
    "ne_agent_run_approval_joins",
    "ne_agent_run_approval_resume_intents",
    "ne_agent_run_continuation_checkpoints",
    "ne_agent_standing_approvals",
]


#: The ``decided_reason`` a successor's approval row carries when its decision
#: came from a standing approval rather than from a human answering THIS park.
#: Named, not blank: a reader must be able to tell "a human decided this exact
#: effect earlier, and the platform honoured that decision under a fresh owner
#: context" apart from "a human sat in front of this request".
STANDING_APPROVAL_REASON = "standing_approval_consumed"

#: The state a standing approval reaches when its policy-configured lifetime
#: runs out. SPEC names it (``it lapses to `approval_lapsed` and authorizes
#: nothing``), so the durable status carries the SPEC's word rather than a
#: shorter synonym a reader would have to translate.
STANDING_LAPSED_STATUS = "approval_lapsed"


def _as_utc(value: datetime) -> datetime:
    """Read a stored timestamp as UTC-aware (SQLite hands back naive values)."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class RunApprovalWaiting(RuntimeError):
    """Typed control: a canonical Run parked atomically for human approval."""

    def __init__(self, joined: RunApprovalJoin) -> None:
        self.joined = joined
        super().__init__("canonical Run is waiting for approval")


ne_agent_run_continuation_checkpoints = sa.Table(
    "ne_agent_run_continuation_checkpoints",
    metadata,
    sa.Column("checkpoint_id", sa.String, primary_key=True),
    sa.Column("checkpoint_key", sa.String(64), nullable=False, unique=True),
    sa.Column("run_id", sa.String, nullable=False, unique=True),
    sa.Column("attempt_id", sa.String, nullable=False),
    sa.Column("step_id", sa.String, nullable=False),
    sa.Column("tool_call_id", sa.String, nullable=False),
    sa.Column("status", sa.String, nullable=False),
    sa.Column("protected_invocation_id", sa.String, nullable=False, unique=True),
    sa.Column("protected_invocation_hash", sa.String(64), nullable=False),
    sa.Column("stamp_receipt_id", sa.String, nullable=False),
    sa.Column("stamp_receipt_hash", sa.String(64), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint(
        "status IN ('pending','approved','resumed','consumed','rejected','expired',"
        "'source_withheld')",
        name="ck_run_continuation_status",
    ),
    sa.ForeignKeyConstraint(["run_id"], ["ne_agent_runs.run_id"], name="fk_checkpoint_run"),
    sa.ForeignKeyConstraint(
        ["attempt_id", "run_id"],
        ["ne_agent_run_attempt_links.attempt_id", "ne_agent_run_attempt_links.run_id"],
        name="fk_checkpoint_attempt",
    ),
    sa.ForeignKeyConstraint(
        ["protected_invocation_id"],
        ["ne_agent_protected_source_invocations.protected_invocation_id"],
        name="fk_checkpoint_invocation",
    ),
)

ne_agent_run_approval_joins = sa.Table(
    "ne_agent_run_approval_joins",
    metadata,
    sa.Column("approval_id", sa.String, primary_key=True),
    sa.Column("run_id", sa.String, nullable=False, unique=True),
    sa.Column("attempt_id", sa.String, nullable=False),
    sa.Column("checkpoint_id", sa.String, nullable=False, unique=True),
    sa.Column("action_hash", sa.String(64), nullable=False),
    sa.Column("principal_id", sa.String, nullable=False),
    sa.Column("policy_hash", sa.String(64), nullable=False),
    sa.Column("protected_invocation_id", sa.String, nullable=False),
    sa.Column("protected_invocation_hash", sa.String(64), nullable=False),
    # Park-time governance metadata (U2 adversarial review, F2): ``tier`` selects
    # which approval policy applies (Tier-3 dual-approval), so the decide path
    # must verify the decision-time approval row against this insert-only record
    # instead of trusting a mutable re-load. Nullable only because ALTER TABLE
    # ADD COLUMN cannot add NOT NULL to a populated table; ``_join`` fails closed
    # on NULL, and legacy rows are deliberately never backfilled (the only
    # source would be the mutable request row — Sol review, P1).
    sa.Column("tier", sa.Integer, nullable=True),
    sa.Column("action", sa.String, nullable=True),
    # The POLICY-resolved standing-approval lifetime for this action, in
    # seconds, frozen at park (F1). Persisted HERE, in the insert-only join,
    # because the seam that mints a standing approval — the terminalization of
    # an `authorization_expired` attempt — has no live PDP call to ask. NULL
    # means the park recorded no policy lifetime (a legacy row, or a bundle
    # that named none), and NULL mints NO standing approval: the decision dies
    # with its attempt exactly as it did before F1. That is the fail-safe
    # direction and it is the reason this column is allowed to be nullable at
    # all — an absent lifetime can never be read as an unbounded one.
    sa.Column("standing_lifetime_s", sa.Float, nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(["run_id"], ["ne_agent_runs.run_id"], name="fk_run_approval_run"),
    sa.ForeignKeyConstraint(
        ["checkpoint_id"],
        ["ne_agent_run_continuation_checkpoints.checkpoint_id"],
        name="fk_run_approval_checkpoint",
    ),
    sa.ForeignKeyConstraint(
        ["protected_invocation_id"],
        ["ne_agent_protected_source_invocations.protected_invocation_id"],
        name="fk_run_approval_invocation",
    ),
)

#: A DECIDED, UNCONSUMED approval that outlived the attempt it was asked for.
#:
#: SPEC (delayed identity truth): "A Tier-2/3 approval decision is durable
#: approver-authority evidence and survives the attempt it was asked for."
#: When a parked attempt terminalizes ``authorization_expired``, its
#: decided-but-unconsumed approval becomes a row here — bound to the exact
#: tuple ``(owner_principal_id, action_hash, policy_hash,
#: protected_invocation_hash)``, with a policy-configured ``lapses_at``.
#:
#: What this row IS: evidence that a named human approved THIS EXACT effect,
#: made under that human's OWN fresh authority at decision time. What it is
#: NOT, and must never be read as: owner execution authority. The owner's
#: expired snapshot is never revived, rebound, or replaced (SPEC RED list) —
#: the successor brings its own verifier-authored context with CURRENT groups,
#: and this row only spares the successor a second trip to the same human for
#: a bit-identical effect.
#:
#: Every failure direction is closed by construction:
#:   * a different owner/action-hash/policy-hash/invocation-hash never matches
#:     the partial unique binding index, so it finds nothing and parks;
#:   * past ``lapses_at`` the row is CAS'd to ``approval_lapsed`` and
#:     authorizes nothing;
#:   * ``consumed`` is a one-way CAS under the successor's Run lock, so the
#:     row can satisfy AT MOST ONE successor;
#:   * ``predecessor_*``/``successor_*`` make the audit linkage durable rather
#:     than reconstructed.
ne_agent_standing_approvals = sa.Table(
    "ne_agent_standing_approvals",
    metadata,
    sa.Column("standing_id", sa.String, primary_key=True),
    sa.Column("owner_principal_id", sa.String, nullable=False),
    #: The tool NAME, carried beside the hashes because it is NOT in them:
    #: ``action_hash`` digests the ARGUMENTS, not the action. The same lesson
    #: ``_require_canonical_run_approval`` already learned — a different
    #: registered tool invoked with byte-identical arguments must never wear an
    #: approved effect's identity.
    sa.Column("action", sa.String, nullable=False),
    #: The tier the human decided under. Also not in the tuple, also
    #: load-bearing: a Tier-2 decision may never satisfy a Tier-3 gate, and an
    #: escalation (provisional context) can move the tier while every hash
    #: stays identical.
    sa.Column("tier", sa.Integer, nullable=False),
    sa.Column("action_hash", sa.String(64), nullable=False),
    sa.Column("policy_hash", sa.String(64), nullable=False),
    sa.Column("protected_invocation_hash", sa.String(64), nullable=False),
    sa.Column("status", sa.String, nullable=False),
    sa.Column("approver", sa.String, nullable=False),
    sa.Column("second_approver", sa.String, nullable=True),
    sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("lapses_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("predecessor_approval_id", sa.String, nullable=False, unique=True),
    sa.Column("predecessor_run_id", sa.String, nullable=False),
    sa.Column("predecessor_attempt_id", sa.String, nullable=False),
    sa.Column("successor_approval_id", sa.String, nullable=True, unique=True),
    sa.Column("successor_run_id", sa.String, nullable=True),
    sa.Column("successor_attempt_id", sa.String, nullable=True),
    sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint(
        "status IN ('standing','consumed','approval_lapsed')",
        name="ck_standing_approval_status",
    ),
    sa.CheckConstraint("length(action_hash) = 64", name="ck_standing_approval_action_hash"),
    sa.CheckConstraint("length(policy_hash) = 64", name="ck_standing_approval_policy_hash"),
    sa.CheckConstraint(
        "length(protected_invocation_hash) = 64", name="ck_standing_approval_invocation_hash"
    ),
    # A consumed row names its successor and nothing else does. Without this,
    # "consumed at most once" would rest on application code alone.
    sa.CheckConstraint(
        "(CASE WHEN status = 'consumed' THEN 1 ELSE 0 END) = "
        "(CASE WHEN successor_approval_id IS NOT NULL THEN 1 ELSE 0 END)",
        name="ck_standing_approval_consumed_names_successor",
    ),
    sa.ForeignKeyConstraint(
        ["predecessor_approval_id"],
        ["ne_agent_run_approval_joins.approval_id"],
        name="fk_standing_approval_predecessor",
    ),
    sa.ForeignKeyConstraint(
        ["successor_approval_id"],
        ["ne_agent_run_approval_joins.approval_id"],
        name="fk_standing_approval_successor",
    ),
    # AT MOST ONE live standing approval per binding tuple. Partial so a
    # consumed or lapsed row never blocks a later human decision on the same
    # effect — the index enforces "one usable answer", not "one answer ever".
    sa.Index(
        "uq_standing_approval_live_binding",
        "owner_principal_id",
        "action_hash",
        "policy_hash",
        "protected_invocation_hash",
        unique=True,
        sqlite_where=sa.text("status = 'standing'"),
        postgresql_where=sa.text("status = 'standing'"),
    ),
)

ne_agent_run_approval_audit_outbox = sa.Table(
    "ne_agent_run_approval_audit_outbox",
    metadata,
    sa.Column("intent_id", sa.String, primary_key=True),
    sa.Column("approval_id", sa.String, nullable=False, unique=True),
    sa.Column("run_id", sa.String, nullable=False),
    sa.Column("attempt_id", sa.String, nullable=False),
    sa.Column("action_hash", sa.String(64), nullable=False),
    sa.Column("event_kind", sa.String, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(
        ["run_id"], ["ne_agent_runs.run_id"], name="fk_run_approval_outbox_run"
    ),
)

ne_agent_run_approval_resume_intents = sa.Table(
    "ne_agent_run_approval_resume_intents",
    metadata,
    sa.Column("resume_id", sa.String, primary_key=True),
    sa.Column("approval_id", sa.String, nullable=False, unique=True),
    sa.Column("decision", sa.String, nullable=False),
    sa.Column("run_id", sa.String, nullable=False),
    sa.Column("attempt_id", sa.String, nullable=False),
    sa.Column("dispatch_id", sa.String, nullable=False),
    sa.Column("owner_principal_id", sa.String, nullable=False),
    sa.Column("config_hash", sa.String(64), nullable=False),
    sa.Column("action_hash", sa.String(64), nullable=False),
    sa.Column("policy_hash", sa.String(64), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(
        ["approval_id"],
        ["ne_agent_run_approval_joins.approval_id"],
        name="fk_run_approval_resume_join",
    ),
    sa.ForeignKeyConstraint(
        ["attempt_id", "run_id"],
        ["ne_agent_run_attempt_links.attempt_id", "ne_agent_run_attempt_links.run_id"],
        name="fk_run_approval_resume_attempt",
    ),
    sa.CheckConstraint(
        "decision IN ('approved','rejected','expired')",
        name="ck_run_approval_resume_decision",
    ),
    sa.CheckConstraint("length(config_hash) = 64", name="ck_run_approval_resume_config"),
    sa.CheckConstraint("length(action_hash) = 64", name="ck_run_approval_resume_action"),
    sa.CheckConstraint("length(policy_hash) = 64", name="ck_run_approval_resume_policy"),
)


@dataclass(frozen=True, slots=True)
class RunContinuationCheckpoint:
    checkpoint_id: str
    run_id: str
    attempt_id: str
    step_id: str
    tool_call_id: str
    status: str
    protected_invocation_id: str
    protected_invocation_hash: str
    stamp_receipt_id: str
    stamp_receipt_hash: str


@dataclass(frozen=True, slots=True)
class RunApprovalJoin:
    approval_id: str
    run_id: str
    attempt_id: str
    checkpoint_id: str
    action_hash: str
    principal_id: str
    policy_hash: str
    protected_invocation_id: str
    protected_invocation_hash: str
    #: Park-time governance metadata (F2): the tier/action the human's approval
    #: policy was selected under, bound at park time in the insert-only join.
    tier: int
    action: str


@dataclass(frozen=True, slots=True)
class StandingDecision:
    """A human decision landing PAST the run owner's authorization horizon.

    Handed to :meth:`RunApprovalCheckpointStore.terminalize_authorization_expired`
    by the decide route so the decision is recorded as durable approver-authority
    evidence IN THE SAME TRANSACTION that terminalizes the attempt. Both halves
    are true at once and neither is conditional on the other: the decision is
    real (a named human, freshly authenticated, approved this exact effect) and
    the attempt is dead (nothing executes, nothing is disclosed, the owner's
    expired snapshot is never revived).
    """

    approver: str
    second_approver: str | None = None


@dataclass(frozen=True, slots=True)
class StandingApproval:
    """One row of :data:`ne_agent_standing_approvals`, as read back."""

    standing_id: str
    owner_principal_id: str
    action: str
    tier: int
    action_hash: str
    policy_hash: str
    protected_invocation_hash: str
    status: str
    approver: str
    second_approver: str | None
    decided_at: datetime
    lapses_at: datetime
    predecessor_approval_id: str
    predecessor_run_id: str
    predecessor_attempt_id: str


def _standing(row: Any) -> StandingApproval:
    return StandingApproval(
        standing_id=str(row.standing_id),
        owner_principal_id=str(row.owner_principal_id),
        action=str(row.action),
        tier=int(row.tier),
        action_hash=str(row.action_hash),
        policy_hash=str(row.policy_hash),
        protected_invocation_hash=str(row.protected_invocation_hash),
        status=str(row.status),
        approver=str(row.approver),
        second_approver=None if row.second_approver is None else str(row.second_approver),
        decided_at=row.decided_at,
        lapses_at=row.lapses_at,
        predecessor_approval_id=str(row.predecessor_approval_id),
        predecessor_run_id=str(row.predecessor_run_id),
        predecessor_attempt_id=str(row.predecessor_attempt_id),
    )


class RunApprovalResumeRequest(BaseModel):
    """Content-free exact input for the decision-triggered DBOS resume workflow."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    resume_id: str
    approval_id: str
    decision: str
    run_id: str
    attempt_id: str
    dispatch_id: str
    owner_principal_id: str
    config_hash: str
    action_hash: str
    policy_hash: str


def expire_canonical_run_approval_in_transaction(
    conn: sa.Connection,
    *,
    approval_id: str,
    decided_at: datetime,
    event: ApprovalEvent,
) -> str | None:
    """Expire one Run-bound wait atomically, or return ``None`` for legacy rows.

    The decision, evidence row, exact continuation checkpoint and content-free
    resume intent share the caller's transaction. A replay returns the stored
    terminal decision and emits no second evidence/intent.
    """

    joined = conn.execute(
        sa.select(ne_agent_run_approval_joins)
        .where(ne_agent_run_approval_joins.c.approval_id == approval_id)
        .with_for_update()
    ).one_or_none()
    if joined is None:
        return None
    request = conn.execute(
        sa.select(ne_approval_requests.c.action, ne_approval_requests.c.tier).where(
            ne_approval_requests.c.request_id == approval_id
        )
    ).one()
    changed = conn.execute(
        ne_approval_requests.update()
        .where(
            ne_approval_requests.c.request_id == approval_id,
            ne_approval_requests.c.status.in_(PENDING_STATUSES),
        )
        .values(status="expired", decided_at=decided_at)
    )
    if changed.rowcount != 1:
        return str(
            conn.execute(
                sa.select(ne_approval_requests.c.status).where(
                    ne_approval_requests.c.request_id == approval_id
                )
            ).scalar_one()
        )
    append(conn, event.model_copy(update={"approval_id": approval_id}))
    conn.execute(
        ne_approval_evidence.insert().values(
            request_id=approval_id,
            action=str(request.action),
            tier=int(request.tier),
            outcome="expired",
            approver=None,
            decided_at=decided_at,
            clean=False,
        )
    )
    checkpoint = conn.execute(
        ne_agent_run_continuation_checkpoints.update()
        .where(
            ne_agent_run_continuation_checkpoints.c.checkpoint_id == joined.checkpoint_id,
            ne_agent_run_continuation_checkpoints.c.status == "pending",
        )
        .values(status="expired", updated_at=decided_at)
    )
    if checkpoint.rowcount != 1:
        raise RuntimeError("Run approval expiry has no pending exact checkpoint")

    from ne.runtime.run_workflow import ne_agent_run_workflow_claims

    workflow = conn.execute(
        sa.select(
            ne_agent_run_workflow_claims.c.dispatch_id,
            ne_agent_run_workflow_claims.c.owner_principal_id,
            ne_agent_run_workflow_claims.c.config_hash,
        ).where(
            ne_agent_run_workflow_claims.c.run_id == joined.run_id,
            ne_agent_run_workflow_claims.c.attempt_id == joined.attempt_id,
            ne_agent_run_workflow_claims.c.owner_principal_id == joined.principal_id,
        )
    ).one_or_none()
    if workflow is None:
        raise RuntimeError("Run approval expiry has no exact durable workflow join")
    conn.execute(
        ne_agent_run_approval_resume_intents.insert().values(
            resume_id=str(uuid.uuid4()),
            approval_id=approval_id,
            decision="expired",
            run_id=str(joined.run_id),
            attempt_id=str(joined.attempt_id),
            dispatch_id=str(workflow.dispatch_id),
            owner_principal_id=str(workflow.owner_principal_id),
            config_hash=str(workflow.config_hash),
            action_hash=str(joined.action_hash),
            policy_hash=str(joined.policy_hash),
            created_at=decided_at,
        )
    )
    return "expired"


def _sha(name: str, value: str) -> None:
    if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError(f"{name} must be lowercase sha256")


def _checkpoint(row: Any) -> RunContinuationCheckpoint:
    return RunContinuationCheckpoint(
        **{
            field: str(getattr(row, field))
            for field in RunContinuationCheckpoint.__dataclass_fields__
        }
    )


def _join(row: Any) -> RunApprovalJoin:
    tier = getattr(row, "tier", None)
    action = getattr(row, "action", None)
    if tier is None or action is None:
        # Fail closed: a join without its park-time governance metadata cannot
        # prove which approval policy the human decided under. Legacy rows
        # (parked before the columns existed) are deliberately NOT backfilled —
        # the only available source is the mutable request row, and copying it
        # would bless a tampered value as park-time truth (Sol review, P1). Such
        # rows stay undecidable until the owner intervenes.
        raise LookupError("Run approval join is missing its park-time governance metadata")
    return RunApprovalJoin(
        tier=int(tier),
        action=str(action),
        **{
            field: str(getattr(row, field))
            for field in RunApprovalJoin.__dataclass_fields__
            if field not in {"tier", "action"}
        },
    )


def _require_parked_governance(joined: Any, *, tier: Any, action: Any) -> None:
    """Fail closed unless decision-time tier/action equal the park-time join (F2).

    ``joined`` is the insert-only ``ne_agent_run_approval_joins`` row (or its
    dataclass); ``tier``/``action`` are the values just re-loaded from the
    mutable ``ne_approval_requests`` row. ``tier`` selects which approval policy
    applies (Tier-3 dual-approval), so a decision may never proceed on values
    that drifted from what was parked.
    """
    parked_tier = getattr(joined, "tier", None)
    parked_action = getattr(joined, "action", None)
    if (
        parked_tier is None
        or parked_action is None
        or int(tier) != int(parked_tier)
        or not hmac.compare_digest(str(action), str(parked_action))
    ):
        raise LookupError("Run approval governance metadata drifted from its parked join")


class RunApprovalCheckpointStore:
    """The sole writer for canonical Run approval waits."""

    def __init__(self, runs: AgentRunStore, approvals: SqlAlchemyApprovalStore) -> None:
        if runs._engine is not approvals.engine:  # same Engine => same transaction boundary
            raise ValueError("Run and approval stores must share one SQLAlchemy engine")
        self._runs = runs
        self._approvals = approvals
        self._engine = approvals.engine

    async def create_wait(
        self,
        *,
        run_id: str,
        attempt_id: str,
        step_id: str,
        tool_call_id: str,
        action: str,
        action_hash: str,
        tier: int,
        principal_id: str,
        policy_hash: str,
        invocation_plaintext: bytes,
        source_stamps: Sequence[SourceAuthorityStamp],
        stamp_receipt_id: str,
        stamp_receipt_hash: str,
        requested_at: datetime,
        sla_deadline: datetime,
        expires_at: datetime,
        event: ApprovalEvent,
        standing_lifetime_s: float | None = None,
    ) -> RunApprovalJoin:
        """Claim or load one complete wait; a conflicting replay fails closed.

        ``standing_lifetime_s`` is the POLICY-resolved standing-approval
        lifetime for this action (F1), frozen into the insert-only join so the
        terminalization seam — which has no live PDP call — can compute
        ``lapses_at`` from the answer policy gave AT PARK. ``None`` means the
        policy named none, and a park with no lifetime mints no standing
        approval: the decision dies with its attempt, exactly as before F1.
        Deliberately NOT part of the replay integrity comparison: the join is
        insert-only, so the FIRST park's policy answer is what binds, and a
        later data-document edit cannot retro-extend a lifetime already
        recorded.
        """
        for name, value in (
            ("action_hash", action_hash),
            ("policy_hash", policy_hash),
            ("stamp_receipt_hash", stamp_receipt_hash),
        ):
            _sha(name, value)
        if not source_stamps:
            raise ValueError("a protected Run approval requires source authority stamps")
        key = self.continuation_key(
            run_id=run_id,
            attempt_id=attempt_id,
            step_id=step_id,
            tool_call_id=tool_call_id,
            action_hash=action_hash,
        )
        return await self._runs._call_db(
            self._create_wait_sync,
            key,
            run_id,
            attempt_id,
            step_id,
            tool_call_id,
            action,
            action_hash,
            tier,
            principal_id,
            policy_hash,
            invocation_plaintext,
            tuple(source_stamps),
            stamp_receipt_id,
            stamp_receipt_hash,
            requested_at,
            sla_deadline,
            expires_at,
            event,
            standing_lifetime_s,
        )

    def _create_wait_sync(
        self,
        key: str,
        run_id: str,
        attempt_id: str,
        step_id: str,
        tool_call_id: str,
        action: str,
        action_hash: str,
        tier: int,
        principal_id: str,
        policy_hash: str,
        plaintext: bytes,
        stamps: tuple[SourceAuthorityStamp, ...],
        stamp_receipt_id: str,
        stamp_receipt_hash: str,
        now: datetime,
        sla_deadline: datetime,
        expires_at: datetime,
        event: ApprovalEvent,
        standing_lifetime_s: float | None,
    ) -> RunApprovalJoin:
        with self._engine.begin() as conn:
            # Serialize contenders on the Run before testing the idempotency row.
            # Under Postgres READ COMMITTED, the select after this lock observes a
            # winner that committed while this transaction waited.
            run = conn.execute(
                sa.select(ne_agent_runs).where(ne_agent_runs.c.run_id == run_id).with_for_update()
            ).one_or_none()
            if run is None or str(run.owner_principal_id) != principal_id:
                raise ValueError("Run is not an owned approval target")
            existing = conn.execute(
                sa.select(ne_agent_run_approval_joins)
                .join(
                    ne_agent_run_continuation_checkpoints,
                    ne_agent_run_continuation_checkpoints.c.checkpoint_id
                    == ne_agent_run_approval_joins.c.checkpoint_id,
                )
                .where(ne_agent_run_continuation_checkpoints.c.checkpoint_key == key)
            ).one_or_none()
            if existing is not None:
                candidate = _join(existing)
                expected = (run_id, attempt_id, action_hash, principal_id, policy_hash)
                actual = (
                    candidate.run_id,
                    candidate.attempt_id,
                    candidate.action_hash,
                    candidate.principal_id,
                    candidate.policy_hash,
                )
                if not all(
                    hmac.compare_digest(a, b) for a, b in zip(actual, expected, strict=True)
                ):
                    raise ValueError("Run approval checkpoint replay changed its declared intent")
                checkpoint = conn.execute(
                    sa.select(ne_agent_run_continuation_checkpoints).where(
                        ne_agent_run_continuation_checkpoints.c.checkpoint_id
                        == candidate.checkpoint_id
                    )
                ).one()
                invocation = conn.execute(
                    sa.select(ne_agent_protected_source_invocations).where(
                        ne_agent_protected_source_invocations.c.protected_invocation_id
                        == candidate.protected_invocation_id
                    )
                ).one()
                approval = conn.execute(
                    sa.select(ne_approval_requests).where(
                        ne_approval_requests.c.request_id == candidate.approval_id
                    )
                ).one()
                replay_strings = (
                    (str(checkpoint.step_id), step_id),
                    (str(checkpoint.tool_call_id), tool_call_id),
                    (str(checkpoint.protected_invocation_hash), _hash(plaintext)),
                    (str(checkpoint.stamp_receipt_id), stamp_receipt_id),
                    (str(checkpoint.stamp_receipt_hash), stamp_receipt_hash),
                    (str(invocation.stamp_set_hash), _stamp_set_hash(stamps)),
                    (str(invocation.run_id), run_id),
                    (str(invocation.owner_principal_id), principal_id),
                    (str(approval.action), action),
                    (str(approval.principal_id), principal_id),
                    (str(approval.args_hash), action_hash),
                )
                expected_binding = [
                    run_id,
                    attempt_id,
                    step_id,
                    tool_call_id,
                    candidate.approval_id,
                ]
                if (
                    not all(
                        hmac.compare_digest(actual_value, expected_value)
                        for actual_value, expected_value in replay_strings
                    )
                    or int(approval.tier) != tier
                    or candidate.tier != tier
                    or candidate.action != action
                    or str(invocation.target_kind) != "approval_action"
                    or list(invocation.target_binding) != expected_binding
                ):
                    raise ValueError(
                        "Run approval checkpoint replay changed its protected invocation"
                    )
                return candidate

            # ONE CONTINUATION PER RUN, SAID OUT LOUD. Both durable tables are
            # UNIQUE on ``run_id``, and two seams read ``(run_id, attempt_id)``
            # as an at-most-one address (see
            # ``RunApprovalContinuationExhaustedError``). ``existing`` is None
            # here, so any checkpoint already on this Run belongs to a DIFFERENT
            # call — which is what a second approval-gated tool call in a Run
            # that already spent its continuation produces. Refuse it as
            # governance, under the Run lock taken above and BEFORE any insert,
            # instead of letting the UNIQUE index abort a half-written
            # transaction with a raw ``IntegrityError`` after the first effect
            # has already been sent.
            #
            # DELIBERATELY BEFORE THE ``RUNNING`` CHECK BELOW, which changes one
            # outcome and is worth saying so rather than leaving it to be found.
            # A Run STILL PARKED at its wait is ``WAITING_APPROVAL``, so a second
            # ``create_wait`` against it used to fall through to that check and
            # raise ``ValueError`` — which kills the attempt. It now raises the
            # typed blocking refusal instead and the second effect becomes a
            # ``BLOCKED:`` tool result the model is told about: the more specific
            # answer, and the true one for every Run in that state. (The F1
            # SATISFIED-park route arrives here ``RUNNING`` instead, ``resume``
            # having CAS'd it back — that is the route that used to reach the
            # raw ``IntegrityError``, and it is what this guard was written for.)
            held = conn.execute(
                sa.select(ne_agent_run_continuation_checkpoints.c.checkpoint_key)
                .where(ne_agent_run_continuation_checkpoints.c.run_id == run_id)
                .limit(1)
            ).scalar()
            if held is not None:
                raise RunApprovalContinuationExhaustedError(
                    "this Run already holds an approval continuation; a second "
                    "approval-gated effect needs its own run"
                )
            if str(run.status) != RunState.RUNNING.value:
                raise ValueError("Run is not an owned running approval target")
            attempt = conn.execute(
                sa.select(ne_agent_run_attempt_links.c.attempt_id).where(
                    ne_agent_run_attempt_links.c.attempt_id == attempt_id,
                    ne_agent_run_attempt_links.c.run_id == run_id,
                )
            ).one_or_none()
            if attempt is None:
                raise ValueError("approval attempt does not belong to Run")

            approval_id, checkpoint_id = str(uuid.uuid4()), str(uuid.uuid4())
            target_binding = (run_id, attempt_id, step_id, tool_call_id, approval_id)
            target_hash = protected_target_hash(
                PrivatePayloadDomain.PROTECTED_SOURCE_INVOCATION,
                principal_id,
                "approval_action",
                target_binding,
            )
            packed = _pack_source_bound_payload(plaintext, stamps)
            invocation_id = _id()
            envelope = self._runs._cipher.encrypt_for_target(
                domain=PrivatePayloadDomain.PROTECTED_SOURCE_INVOCATION,
                row_id=invocation_id,
                owner_principal_id=principal_id,
                binding=protected_payload_aad_binding(
                    target_kind="approval_action",
                    target_binding=target_binding,
                    content_status=None,
                ),
                plaintext=packed,
            )
            stamp_hash = _stamp_set_hash(stamps)
            conn.execute(
                ne_agent_protected_source_invocations.insert().values(
                    protected_invocation_id=invocation_id,
                    run_id=run_id,
                    owner_principal_id=principal_id,
                    target_kind="approval_action",
                    target_hash=target_hash,
                    target_binding=list(target_binding),
                    conversation_id=None,
                    turn_id=None,
                    turn_sequence=None,
                    attachment_id=None,
                    attempt_id=None,
                    content_status=None,
                    stamp_set_hash=stamp_hash,
                    created_at=now,
                    **_envelope_values(envelope),
                )
            )
            protected_hash = _hash(plaintext)
            event = event.model_copy(update={"approval_id": approval_id})
            payload = json.dumps(
                {
                    "tool": action,
                    "disclosure": {
                        "protected_invocation_ref": {
                            "reference_id": invocation_id,
                            "invocation_hash": protected_hash,
                            "stamp_receipt_id": stamp_receipt_id,
                            "stamp_receipt_hash": stamp_receipt_hash,
                        }
                    },
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            self._approvals._create_conn(
                conn,
                request_id=approval_id,
                action=action,
                tier=tier,
                principal_id=principal_id,
                payload=payload,
                now=now,
                sla_deadline=sla_deadline,
                expires_at=expires_at,
                event=event,
                action_scope=run_id,
                args_hash=action_hash,
                run_bound=True,
            )
            conn.execute(
                ne_agent_run_continuation_checkpoints.insert().values(
                    checkpoint_id=checkpoint_id,
                    checkpoint_key=key,
                    run_id=run_id,
                    attempt_id=attempt_id,
                    step_id=step_id,
                    tool_call_id=tool_call_id,
                    status="pending",
                    protected_invocation_id=invocation_id,
                    protected_invocation_hash=protected_hash,
                    stamp_receipt_id=stamp_receipt_id,
                    stamp_receipt_hash=stamp_receipt_hash,
                    created_at=now,
                    updated_at=now,
                )
            )
            values = dict(
                approval_id=approval_id,
                run_id=run_id,
                attempt_id=attempt_id,
                checkpoint_id=checkpoint_id,
                action_hash=action_hash,
                principal_id=principal_id,
                policy_hash=policy_hash,
                protected_invocation_id=invocation_id,
                protected_invocation_hash=protected_hash,
                tier=tier,
                action=action,
                created_at=now,
            )
            conn.execute(
                ne_agent_run_approval_joins.insert().values(
                    standing_lifetime_s=standing_lifetime_s, **values
                )
            )
            changed = conn.execute(
                ne_agent_runs.update()
                .where(
                    ne_agent_runs.c.run_id == run_id,
                    ne_agent_runs.c.status == RunState.RUNNING.value,
                )
                .values(
                    status=RunState.WAITING_APPROVAL.value,
                    version=ne_agent_runs.c.version + 1,
                    updated_at=now,
                )
            )
            if changed.rowcount != 1:
                raise ValueError("Run approval wait lost its state transition")
            conn.execute(
                ne_agent_run_approval_audit_outbox.insert().values(
                    intent_id=str(uuid.uuid4()),
                    approval_id=approval_id,
                    run_id=run_id,
                    attempt_id=attempt_id,
                    action_hash=action_hash,
                    event_kind="approval.requested",
                    created_at=now,
                )
            )
            return RunApprovalJoin(
                tier=tier,
                action=action,
                **{
                    field: str(values[field])
                    for field in RunApprovalJoin.__dataclass_fields__
                    if field not in {"tier", "action"}
                },
            )

    @staticmethod
    def continuation_key(
        *, run_id: str, attempt_id: str, step_id: str, tool_call_id: str, action_hash: str
    ) -> str:
        """The exact ``checkpoint_key`` a park writes — the ONE identity of a wait.

        Exposed so a continuation can look its own wait up by the identity the
        park stored, instead of re-deriving that wait from ambient carriers only
        the original agent loop ever populated.
        """

        return _hash(
            json.dumps(
                [run_id, attempt_id, step_id, tool_call_id, action_hash], separators=(",", ":")
            ).encode()
        )

    async def find_resumed_join(
        self,
        *,
        run_id: str,
        attempt_id: str,
        step_id: str,
        tool_call_id: str,
        action_hash: str,
    ) -> RunApprovalJoin | None:
        """The join of an ALREADY-RESUMED continuation for this exact call, or ``None``.

        A continuation that reached ``resumed`` has already had a named human's
        decision landed AND every source-authority stamp re-evaluated under the
        run owner's bound context (``authorize(operation="resume")``). Reading
        the pinned wait back by its own key is the SAME governance with none of
        the re-derivation that made the resume unreachable.
        """

        key = self.continuation_key(
            run_id=run_id,
            attempt_id=attempt_id,
            step_id=step_id,
            tool_call_id=tool_call_id,
            action_hash=action_hash,
        )
        return await self._runs._call_db(self._find_resumed_join_sync, key)

    def _find_resumed_join_sync(self, key: str) -> RunApprovalJoin | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(ne_agent_run_approval_joins)
                .join(
                    ne_agent_run_continuation_checkpoints,
                    ne_agent_run_continuation_checkpoints.c.checkpoint_id
                    == ne_agent_run_approval_joins.c.checkpoint_id,
                )
                .where(
                    ne_agent_run_continuation_checkpoints.c.checkpoint_key == key,
                    ne_agent_run_continuation_checkpoints.c.status == "resumed",
                )
            ).one_or_none()
        return None if row is None else _join(row)

    async def database_now(self) -> datetime:
        """The DATABASE's current UTC, never the calling process's clock.

        An expiry decision, the snapshot it is measured against and the
        terminalization it triggers all live in one database; measuring the
        first against a drifting API-process clock would let a skewed host
        approve a continuation the database considers dead, or refuse one it
        considers live.
        """

        return await self._runs._call_db(self._database_now_sync)

    def _database_now_sync(self) -> datetime:
        from ne.runtime.run_workflow import _database_now

        with self._engine.connect() as conn:
            return _database_now(conn)

    async def pending_audit_intent(self, approval_id: str) -> str | None:
        """The unclaimed ``approval.requested`` audit-outbox intent, if any.

        The park writes this row and NOTHING drained it, so no canonical Run
        audit receipt with ``decision_kind='approval'`` was ever recorded — and
        ``RunFinalizer._observed_approvals`` refuses to compose ANY terminal for
        a Run that ever parked one. That is why an approved Run hung in
        ``running`` forever instead of at least failing cleanly.
        """

        return await self._runs._call_db(self._pending_audit_intent_sync, approval_id)

    def _pending_audit_intent_sync(self, approval_id: str) -> str | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(ne_agent_run_approval_audit_outbox.c.intent_id).where(
                    ne_agent_run_approval_audit_outbox.c.approval_id == approval_id,
                    ne_agent_run_approval_audit_outbox.c.claimed_at.is_(None),
                )
            ).one_or_none()
        return None if row is None else str(row.intent_id)

    async def complete_audit_intent(self, intent_id: str, *, claimed_at: datetime) -> None:
        """Mark one audit-outbox intent drained. Idempotent; never re-opens a row."""

        await self._runs._call_db(self._complete_audit_intent_sync, intent_id, claimed_at)

    def _complete_audit_intent_sync(self, intent_id: str, claimed_at: datetime) -> None:
        with self._engine.begin() as conn:
            changed = conn.execute(
                ne_agent_run_approval_audit_outbox.update()
                .where(
                    ne_agent_run_approval_audit_outbox.c.intent_id == intent_id,
                    ne_agent_run_approval_audit_outbox.c.claimed_at.is_(None),
                )
                .values(claimed_at=claimed_at)
            )
            if changed.rowcount == 1:
                return
            # Zero rows is EITHER a benign replay (another drain already claimed
            # this exact intent — the receipt exists, nothing is lost) OR a
            # caller addressing an intent that never existed, which would mean
            # a drain just claimed to have drained NOTHING. Tell them apart.
            exists = conn.execute(
                sa.select(ne_agent_run_approval_audit_outbox.c.intent_id).where(
                    ne_agent_run_approval_audit_outbox.c.intent_id == intent_id
                )
            ).one_or_none()
            if exists is None:
                raise RuntimeError("Run approval audit intent is unknown; nothing was drained")

    async def consume_resumed(self, approval_id: str, *, consumed_at: datetime) -> None:
        """Fence one resumed continuation as CONSUMED, exactly once, before its effect.

        One approval authorizes ONE execution. This CAS (``resumed`` ->
        ``consumed``) is the durable edge that makes that true: it commits
        BEFORE the governed handler runs, so a workflow re-execution — DBOS
        retry, lease takeover, crash recovery — can never find the continuation
        ``resumed`` again and re-issue the effect. A replay that arrives after
        this fence SETTLES (existing terminal, or the honest
        ``RunEffectSettlementUncertain`` FAILED) instead of re-executing.

        The failure mode this chooses is the safe one: a crash between this
        commit and the send leaves a consumed continuation whose effect never
        issued, which terminalizes FAILED — never the double-send.
        """

        await self._runs._call_db(self._consume_resumed_sync, approval_id, consumed_at)

    def _consume_resumed_sync(self, approval_id: str, now: datetime) -> None:
        with self._engine.begin() as conn:
            joined = conn.execute(
                sa.select(ne_agent_run_approval_joins.c.checkpoint_id).where(
                    ne_agent_run_approval_joins.c.approval_id == approval_id
                )
            ).one_or_none()
            if joined is None:
                raise LookupError("Run approval join unavailable")
            changed = conn.execute(
                ne_agent_run_continuation_checkpoints.update()
                .where(
                    ne_agent_run_continuation_checkpoints.c.checkpoint_id == joined.checkpoint_id,
                    ne_agent_run_continuation_checkpoints.c.status == "resumed",
                )
                .values(status="consumed", updated_at=now)
            )
            if changed.rowcount != 1:
                raise RuntimeError(
                    "Run approval continuation is not resumed; its one execution was "
                    "already consumed"
                )

    async def find_join_for_attempt(
        self, *, run_id: str, attempt_id: str
    ) -> RunApprovalJoin | None:
        """The approval join parked by this exact Run attempt, or ``None``."""

        return await self._runs._call_db(self._find_join_for_attempt_sync, run_id, attempt_id)

    def _find_join_for_attempt_sync(self, run_id: str, attempt_id: str) -> RunApprovalJoin | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(ne_agent_run_approval_joins).where(
                    ne_agent_run_approval_joins.c.run_id == run_id,
                    ne_agent_run_approval_joins.c.attempt_id == attempt_id,
                )
            ).one_or_none()
        return None if row is None else _join(row)

    async def terminalize_authorization_expired(
        self,
        approval_id: str,
        *,
        decided_at: datetime,
        decision: StandingDecision | None = None,
        standing_event: ApprovalEvent | None = None,
    ) -> bool:
        """Make a parked continuation non-actionable because AUTHORIZATION ran out.

        Per the specification, at or past the run owner's verified expiry the
        platform "executes/discloses nothing, atomically makes any parked
        approval/checkpoint non-actionable and terminally fails the attempt with
        safe reason ``authorization_expired``". Only the withholding half was
        ever built; the platform raised :class:`AuthorizationExpiredError` into
        the void and stranded the run in ``running`` with the approval consumed
        and a 200 APPROVED receipt already in the approver's hand.

        This does NOT revive the expired snapshot, does NOT rebind it to a fresh
        one and does NOT re-authenticate as anybody — all three are RED.
        It only makes the existing refusal VISIBLE.

        F1 — STANDING APPROVAL. In the SAME transaction, a decided-but-unconsumed
        approval becomes a standing approval (:data:`ne_agent_standing_approvals`)
        bound to ``(owner_principal_id, action_hash, policy_hash,
        protected_invocation_hash)`` and lapsing at the park-time
        policy-resolved lifetime. Two sources, and only these two:

        * ``decision`` is not ``None`` — a named human decided PAST the owner's
          horizon. Deciding is an APPROVER-authority act, evidenced by that
          approver's own fresh token; recording it asserts nothing about the
          owner's groups. Nothing about the attempt changes: it still
          terminalizes, still executes nothing, still discloses nothing.
        * the approval row is already ``approved`` and unconsumed — the decision
          landed INSIDE the window and the resume then crossed the horizon.

        Anything else mints nothing. In particular a ``pending``/``pending_second``
        row with no ``decision`` records nothing (no human answered), and a join
        with no park-time ``standing_lifetime_s`` records nothing (no policy
        lifetime ⇒ no standing approval, never an unbounded one).

        The return value is UNCHANGED — ``True`` iff this call terminalized the
        attempt — so no existing caller's contract moves. Whether a standing
        approval was minted is read back separately through
        :meth:`standing_approval_for`, which keeps "did the attempt die" and
        "was the decision preserved" as the two independent facts they are.
        """

        return await self._runs._call_db(
            self._terminalize_authorization_expired_sync,
            approval_id,
            decided_at,
            decision,
            standing_event,
        )

    def _terminalize_authorization_expired_sync(
        self,
        approval_id: str,
        now: datetime,
        decision: StandingDecision | None = None,
        standing_event: ApprovalEvent | None = None,
    ) -> bool:
        from ne.runtime.run_finalizer import RunFinalizer, RunUnavailableTerminalCandidate
        from ne.runtime.run_workflow import (
            AUTHORIZATION_EXPIRED_REASON,
            RunWorkflowStateStore,
            _canonical,
            _database_now,
            _digest,
            ne_agent_run_execution_checkpoints,
        )

        with self._engine.begin() as conn:
            joined = conn.execute(
                sa.select(ne_agent_run_approval_joins).where(
                    ne_agent_run_approval_joins.c.approval_id == approval_id
                )
            ).one_or_none()
            if joined is None:
                raise LookupError("Run approval join unavailable")
            # Same lock order as ``_withhold_sync``: the Run is the lifecycle
            # linearization point and is locked before the mutable approval rows.
            run_status = RunState(
                str(
                    conn.execute(
                        sa.select(ne_agent_runs.c.status)
                        .where(ne_agent_runs.c.run_id == joined.run_id)
                        .with_for_update()
                    ).scalar_one()
                )
            )
            approval_row = conn.execute(
                sa.select(
                    ne_approval_requests.c.status,
                    ne_approval_requests.c.tier,
                    ne_approval_requests.c.action,
                    ne_approval_requests.c.first_approver,
                    ne_approval_requests.c.second_approver,
                    ne_approval_requests.c.decided_at,
                )
                .where(ne_approval_requests.c.request_id == approval_id)
                .with_for_update()
            ).one()
            approval_status = str(approval_row.status)
            checkpoint_status = str(
                conn.execute(
                    sa.select(ne_agent_run_continuation_checkpoints.c.status)
                    .where(
                        ne_agent_run_continuation_checkpoints.c.checkpoint_id
                        == joined.checkpoint_id
                    )
                    .with_for_update()
                ).scalar_one()
            )
            actionable = {
                ("pending", "pending", RunState.WAITING_APPROVAL),
                ("pending_second", "pending", RunState.WAITING_APPROVAL),
                ("approved", "approved", RunState.WAITING_APPROVAL),
                ("approved", "resumed", RunState.RUNNING),
            }
            if (approval_status, checkpoint_status, run_status) not in actionable:
                return False
            approval = conn.execute(
                ne_approval_requests.update()
                .where(
                    ne_approval_requests.c.request_id == approval_id,
                    ne_approval_requests.c.status == approval_status,
                )
                .values(
                    status="expired",
                    decided_reason=AUTHORIZATION_EXPIRED_REASON,
                    decided_at=now,
                )
            )
            if approval.rowcount != 1:
                return False
            # F1: the DECISION survives the attempt. Written inside the same
            # transaction that kills the attempt, so the two facts can never
            # disagree — there is no window in which the run is terminal but
            # the approver's decision was silently dropped, and none in which a
            # standing approval exists for an attempt that is still alive.
            self._mint_standing_approval(
                conn,
                joined=joined,
                approval_row=approval_row,
                decision=decision,
                event=standing_event,
                now=now,
            )
            checkpoint = conn.execute(
                ne_agent_run_continuation_checkpoints.update()
                .where(
                    ne_agent_run_continuation_checkpoints.c.checkpoint_id == joined.checkpoint_id,
                    ne_agent_run_continuation_checkpoints.c.status == checkpoint_status,
                )
                .values(status="expired", updated_at=now)
            )
            if checkpoint.rowcount != 1:
                raise RuntimeError("authorization expiry lost its exact continuation CAS")
            database_now = _database_now(conn)
            # Written BEFORE the finalizer so the Run publishes THIS reason and
            # not the finalizer's derived class-name floor: the reader must be
            # told the authorization ran out, not merely that something failed.
            # ``finalize_unavailable_in_transaction`` leaves an existing
            # ``execution_failed`` checkpoint untouched.
            recorded = conn.execute(
                sa.select(ne_agent_run_execution_checkpoints.c.checkpoint_id).where(
                    ne_agent_run_execution_checkpoints.c.run_id == joined.run_id,
                    ne_agent_run_execution_checkpoints.c.attempt_id == joined.attempt_id,
                    ne_agent_run_execution_checkpoints.c.stage == "execution_failed",
                )
            ).one_or_none()
            if recorded is None:
                safe_detail = {
                    "error_class": "AuthorizationExpired",
                    "error_reason": AUTHORIZATION_EXPIRED_REASON,
                }
                conn.execute(
                    ne_agent_run_execution_checkpoints.insert().values(
                        checkpoint_id=str(uuid.uuid4()),
                        run_id=joined.run_id,
                        attempt_id=joined.attempt_id,
                        stage="execution_failed",
                        intent_hash=_digest(
                            {"stage": "execution_failed", "safe_detail": safe_detail}
                        ),
                        safe_detail=_canonical(safe_detail),
                        created_at=database_now,
                    )
                )
            lifecycle = RunWorkflowStateStore(self._engine, run_store=self._runs)
            target, lease = lifecycle._approval_finalizer_context(
                conn, joined=joined, now=database_now
            )
            RunFinalizer(self._runs).finalize_unavailable_in_transaction(
                conn,
                RunUnavailableTerminalCandidate(
                    target=target,
                    status="failed",
                    outcome="failed",
                    failure_class="AuthorizationExpired",
                ),
                lease=lease,
            )
            return True

    # -- standing approvals (F1) -------------------------------------------

    @staticmethod
    def _standing_lifetime(joined: Any) -> float | None:
        """The park-time policy lifetime, or ``None`` if there is not a usable one.

        Absent, zero, negative and non-finite all answer ``None``, and ``None``
        means MINT NOTHING. This is the one place where "policy said nothing"
        must never widen into "standing forever" — an unbounded carryover of a
        human decision is precisely the claim the platform cannot back.
        """
        raw = getattr(joined, "standing_lifetime_s", None)
        if raw is None or isinstance(raw, bool) or not isinstance(raw, int | float):
            return None
        lifetime = float(raw)
        return lifetime if math.isfinite(lifetime) and lifetime > 0 else None

    def _mint_standing_approval(
        self,
        conn: sa.Connection,
        *,
        joined: Any,
        approval_row: Any,
        decision: StandingDecision | None,
        event: ApprovalEvent | None,
        now: datetime,
    ) -> str | None:
        """Record a decided-unconsumed approval as standing. Fail-safe on every edge.

        Called ONLY from inside the ``authorization_expired`` terminalization
        transaction, holding that Run's lock. Returns the new standing id, or
        ``None`` when nothing should be minted — and "nothing" is the answer to
        every ambiguity, because a standing approval that should not exist is
        strictly worse than an approver being asked again.
        """
        lifetime = self._standing_lifetime(joined)
        if lifetime is None:
            return None
        # TIER AND ACTION COME FROM THE INSERT-ONLY JOIN, NEVER FROM THE MUTABLE
        # REQUEST ROW. Both are written into the durable standing approval
        # inserted below, and ``tier`` also selects the ceremony validated below
        # (Tier-3 needs two distinct named approvers) — so a drifted
        # ``ne_approval_requests`` row could otherwise mint a carryover naming a
        # tool or a tier the human never approved. Every decide path already
        # refuses that drift through ``_require_parked_governance``; this seam
        # cannot call it, because raising inside the terminalization transaction
        # would abort the very ``authorization_expired`` terminal that
        # transaction exists to write and strand the Run again. Reading the
        # park-time record instead is the same invariant with the fail-safe
        # direction: the join is insert-only, so there is nothing to drift.
        #
        # ⚠ WHAT THIS DOES NOT COVER, AND THE MECHANISM THAT DOES. The standing
        # ``ApprovalEvent`` appended at the end of this method is NOT built
        # here: the only caller that supplies one builds it — ``ne-api``'s
        # ``_refuse_expired_authorization``, from ``int(row.tier)`` and
        # ``f"tool:{row.action}"`` off the MUTABLE row — and the ``append``
        # below overwrites its ``approval_id`` and nothing else. So reading the
        # join closes the DURABLE ROW against drift and leaves that event
        # exactly where it already was. ⛔ What keeps the event honest is a
        # separate check, and a reader who thinks this one covers it is the
        # reader who deletes it: both decide routes call
        # ``_parked_governance_matches(joined, row)`` — the route-level twin of
        # ``_require_parked_governance`` — and answer 409 on drift BEFORE
        # ``_refuse_expired_authorization`` runs, so a drifted row never reaches
        # the event constructor at all.
        #
        # NULL tier/action means a legacy park from before the columns existed
        # (they are nullable only for that reason, and are deliberately never
        # backfilled from the mutable row) — mint NOTHING, the same answer the
        # NULL lifetime above gets.
        parked_tier = getattr(joined, "tier", None)
        parked_action = getattr(joined, "action", None)
        if parked_tier is None or parked_action is None:
            return None
        tier = int(parked_tier)
        action = str(parked_action)
        status = str(approval_row.status)
        if decision is not None:
            # A named human decided PAST the horizon. Re-validate the ceremony
            # HERE rather than trusting the route: a Tier-3 effect needs two
            # DISTINCT named approvers, and a half-finished Tier-3 ceremony must
            # leave no standing approval at all — the successor asks both humans
            # again.
            if status not in PENDING_STATUSES:
                return None
            approver = decision.approver
            second_approver = decision.second_approver
            if not approver:
                return None
            if tier >= 3:
                if not second_approver or second_approver == approver:
                    return None
            elif second_approver is not None:
                return None
            decided_at = now
        elif status == "approved":
            # Decided INSIDE the window; the resume then crossed the horizon.
            # The decision is already durable and already evidenced — carry it.
            approver = str(approval_row.first_approver or "")
            second_approver = (
                None if approval_row.second_approver is None else str(approval_row.second_approver)
            )
            if not approver:
                return None
            if tier >= 3 and (not second_approver or second_approver == approver):
                return None
            decided_at = approval_row.decided_at or now
        else:
            # Nobody answered. There is no decision to carry forward.
            return None
        existing = conn.execute(
            sa.select(ne_agent_standing_approvals.c.standing_id)
            .where(
                ne_agent_standing_approvals.c.owner_principal_id == joined.principal_id,
                ne_agent_standing_approvals.c.action_hash == joined.action_hash,
                ne_agent_standing_approvals.c.policy_hash == joined.policy_hash,
                ne_agent_standing_approvals.c.protected_invocation_hash
                == joined.protected_invocation_hash,
                ne_agent_standing_approvals.c.status == "standing",
            )
            .with_for_update()
        ).scalar_one_or_none()
        if existing is not None:
            # One live standing approval per binding tuple. A second decision on
            # a bit-identical effect does not stack a second carryover — the
            # partial unique index would refuse it anyway; refusing here keeps
            # the terminalization transaction from failing over a race it can
            # simply observe.
            #
            # ⚠ AND IT MISREPORTS TO THE SECOND APPROVER. The returned id is the
            # FIRST decision's row, whose ``predecessor_approval_id`` names the
            # FIRST park — so ``standing_approval_for(this approval_id)`` finds
            # nothing, and the decide route's 409 takes its "your decision was
            # NOT recorded" branch. That sentence is wrong in one specific way:
            # approver B's decision really was not recorded, but approver A's
            # carryover for the byte-identical effect WILL fire on the owner's
            # next run, so the effect is not dead either. Approver B is told to
            # expect nothing and something happens. KNOWN AND DELIBERATE, not
            # closed here: making the route say the true thing means widening
            # its read from "the standing row minted by THIS approval" to "the
            # live standing row for this binding tuple", which changes what a
            # 409 asserts, and this is the wrong change to make in a review
            # pass. Reachable only when two approvals for a bit-identical effect
            # under the same owner and bundle version both terminalize at the
            # horizon; nothing in the demo produces it.
            return str(existing)
        standing_id = str(uuid.uuid4())
        conn.execute(
            ne_agent_standing_approvals.insert().values(
                standing_id=standing_id,
                owner_principal_id=str(joined.principal_id),
                action=action,
                tier=tier,
                action_hash=str(joined.action_hash),
                policy_hash=str(joined.policy_hash),
                protected_invocation_hash=str(joined.protected_invocation_hash),
                status="standing",
                approver=approver,
                second_approver=second_approver,
                decided_at=decided_at,
                lapses_at=decided_at + timedelta(seconds=lifetime),
                predecessor_approval_id=str(joined.approval_id),
                predecessor_run_id=str(joined.run_id),
                predecessor_attempt_id=str(joined.attempt_id),
                successor_approval_id=None,
                successor_run_id=None,
                successor_attempt_id=None,
                consumed_at=None,
                created_at=now,
            )
        )
        if event is not None:
            # The safe ApprovalEvent for the decision itself. Emitted only when
            # the caller supplied one — the in-window-decision path already
            # appended its own event when the decision landed, and a second
            # would double-count the same human act.
            append(conn, event.model_copy(update={"approval_id": str(joined.approval_id)}))
        return standing_id

    async def standing_approval_for(self, predecessor_approval_id: str) -> StandingApproval | None:
        """The standing approval minted from this predecessor approval, if any."""

        return await self._runs._call_db(self._standing_approval_for_sync, predecessor_approval_id)

    def _standing_approval_for_sync(self, predecessor_approval_id: str) -> StandingApproval | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(ne_agent_standing_approvals).where(
                    ne_agent_standing_approvals.c.predecessor_approval_id == predecessor_approval_id
                )
            ).one_or_none()
        return None if row is None else _standing(row)

    async def find_live_standing_approval(
        self,
        *,
        owner_principal_id: str,
        action_hash: str,
        policy_hash: str,
        protected_invocation_hash: str,
        now: datetime,
    ) -> StandingApproval | None:
        """The live standing approval for this exact binding tuple, or ``None``.

        LAPSES the row as a side effect when its policy-configured lifetime has
        run out: an expired carryover must be RECORDED ``approval_lapsed``, not
        merely ignored, so the durable record says what happened rather than leaving a
        row that reads as usable forever.

        This is deliberately a separate step from
        :meth:`consume_standing_approval`. The successor's live stamp re-check
        runs BETWEEN them, so a successor whose owner lost source authority in
        the interim never consumes the carryover: it parks for a fresh human
        decision with the standing approval still standing, which is what
        "degrades to ask a human again, never to execute anyway" means here.
        """

        return await self._runs._call_db(
            self._find_live_standing_approval_sync,
            owner_principal_id,
            action_hash,
            policy_hash,
            protected_invocation_hash,
            now,
        )

    def _find_live_standing_approval_sync(
        self,
        owner_principal_id: str,
        action_hash: str,
        policy_hash: str,
        protected_invocation_hash: str,
        now: datetime,
    ) -> StandingApproval | None:
        with self._engine.begin() as conn:
            row = conn.execute(
                sa.select(ne_agent_standing_approvals)
                .where(
                    ne_agent_standing_approvals.c.owner_principal_id == owner_principal_id,
                    ne_agent_standing_approvals.c.action_hash == action_hash,
                    ne_agent_standing_approvals.c.policy_hash == policy_hash,
                    ne_agent_standing_approvals.c.protected_invocation_hash
                    == protected_invocation_hash,
                    ne_agent_standing_approvals.c.status == "standing",
                )
                .with_for_update()
            ).one_or_none()
            if row is None:
                return None
            if now >= _as_utc(row.lapses_at):
                conn.execute(
                    ne_agent_standing_approvals.update()
                    .where(
                        ne_agent_standing_approvals.c.standing_id == row.standing_id,
                        ne_agent_standing_approvals.c.status == "standing",
                    )
                    .values(status=STANDING_LAPSED_STATUS)
                )
                return None
            return _standing(row)

    async def consume_standing_approval(
        self,
        approval_id: str,
        *,
        standing_id: str,
        owner_principal_id: str,
        action: str,
        tier: int,
        action_hash: str,
        policy_hash: str,
        protected_invocation_hash: str,
        now: datetime,
        event: ApprovalEvent,
    ) -> StandingApproval | None:
        """Consume one standing approval for the SUCCESSOR park ``approval_id``.

        Atomic and AT MOST ONCE. In one transaction: re-verify the full binding
        (owner, action name, tier, and all three hashes) against BOTH the
        successor's insert-only join and the standing row, re-check the
        lifetime, CAS the standing row ``standing -> consumed`` naming its
        successor, and land the decision on the successor's own approval row and
        continuation checkpoint.

        ⚠ THE LOCK ORDER IS JOIN-FIRST, NOT RUN-FIRST, and that is deliberate
        rather than an oversight — but it is worth stating because the file has
        TWO orders and a reader who assumes one will be wrong half the time.
        The LIFECYCLE mutators (``_withhold_sync``,
        ``_terminalize_authorization_expired_sync``) lock ``ne_agent_runs``
        first: the Run is their linearization point, they change its state, and
        they say so. The CONTINUATION-CLAIM paths — this method and
        ``_resume_sync`` — lock the CONTINUATION first (join, then standing,
        then the approval request and checkpoint) and reach the Run row only
        through ``_resume_sync``'s guarded ``WAITING_APPROVAL -> RUNNING`` CAS,
        which takes its lock LAST. That ``UPDATE`` does take the Run row's lock
        — it has to, or there would be no pair to invert — but it takes it after
        the continuation rows, it needs no prior lock to be correct, and this
        method never writes the Run row at all. Taking an EXPLICIT Run lock here
        would put this method's Run acquisition FIRST while leaving its
        immediate successor call, ``resume``, continuation-first — inverting the
        pair that always runs back to back, which is worse than the asymmetry it
        would remove.

        Every ordering failure is FAIL-CLOSED anyway: a lost race on any of
        these rows returns ``None``, and ``None`` parks for a fresh human
        decision. Nothing here can answer "execute anyway".

        Returns the consumed row, or ``None`` when anything at all did not line
        up — a lost CAS, a lapse that landed between the peek and here, a tuple
        that drifted, a successor whose park is no longer pending. Every
        ``None`` means the caller parks for a fresh human decision. There is no
        branch that answers "execute anyway".

        What this does NOT do, and must never do: touch the predecessor attempt
        (it stays terminally ``authorization_expired``), read anything from the
        predecessor's expired authorization snapshot, or skip the successor's
        own policy evaluation and stamp re-checks — the caller has already run
        both under the successor's live verifier-authored context.
        """

        return await self._runs._call_db(
            self._consume_standing_approval_sync,
            approval_id,
            standing_id,
            owner_principal_id,
            action,
            tier,
            action_hash,
            policy_hash,
            protected_invocation_hash,
            now,
            event,
        )

    def _consume_standing_approval_sync(
        self,
        approval_id: str,
        standing_id: str,
        owner_principal_id: str,
        action: str,
        tier: int,
        action_hash: str,
        policy_hash: str,
        protected_invocation_hash: str,
        now: datetime,
        event: ApprovalEvent,
    ) -> StandingApproval | None:
        with self._engine.begin() as conn:
            joined = conn.execute(
                sa.select(ne_agent_run_approval_joins)
                .where(ne_agent_run_approval_joins.c.approval_id == approval_id)
                .with_for_update()
            ).one_or_none()
            if joined is None:
                return None
            # The SUCCESSOR's own park must be exactly what the caller says it
            # is. Read from the insert-only join, never from the mutable request
            # row — the same lesson `_require_parked_governance` records.
            successor_binding = (
                str(joined.principal_id),
                str(joined.action_hash),
                str(joined.policy_hash),
                str(joined.protected_invocation_hash),
            )
            claimed_binding = (
                owner_principal_id,
                action_hash,
                policy_hash,
                protected_invocation_hash,
            )
            if not all(
                hmac.compare_digest(a, b)
                for a, b in zip(successor_binding, claimed_binding, strict=True)
            ):
                return None
            parked_tier = getattr(joined, "tier", None)
            parked_action = getattr(joined, "action", None)
            if (
                parked_tier is None
                or parked_action is None
                or int(parked_tier) != tier
                or not hmac.compare_digest(str(parked_action), action)
            ):
                return None
            row = conn.execute(
                sa.select(ne_agent_standing_approvals)
                .where(
                    ne_agent_standing_approvals.c.standing_id == standing_id,
                    ne_agent_standing_approvals.c.status == "standing",
                )
                .with_for_update()
            ).one_or_none()
            if row is None:
                return None
            standing_binding = (
                str(row.owner_principal_id),
                str(row.action_hash),
                str(row.policy_hash),
                str(row.protected_invocation_hash),
            )
            if not all(
                hmac.compare_digest(a, b)
                for a, b in zip(standing_binding, claimed_binding, strict=True)
            ):
                return None
            # THE TOOL NAME AND THE TIER ARE PART OF WHAT THE HUMAN APPROVED and
            # neither is inside any of the three hashes: `action_hash` digests
            # the ARGUMENTS, and `policy_hash` digests the BUNDLE VERSION. So a
            # different registered tool called with byte-identical arguments, or
            # the same tool escalated to Tier-3 by provisional context, would
            # otherwise match the tuple exactly. Equality, not "at least" —
            # a Tier-2 decision may not satisfy a Tier-3 gate, and a Tier-3
            # decision is not evidence a Tier-2 ceremony was performed.
            if int(row.tier) != tier or not hmac.compare_digest(str(row.action), action):
                return None
            if now >= _as_utc(row.lapses_at):
                conn.execute(
                    ne_agent_standing_approvals.update()
                    .where(
                        ne_agent_standing_approvals.c.standing_id == standing_id,
                        ne_agent_standing_approvals.c.status == "standing",
                    )
                    .values(status=STANDING_LAPSED_STATUS)
                )
                return None
            if str(row.predecessor_approval_id) == approval_id:
                # A park can never consume the carryover it produced. Nothing
                # today can reach this (the predecessor is terminal), but the
                # cost of the guard is one comparison and the cost of its
                # absence is a self-satisfying approval.
                return None
            request = conn.execute(
                sa.select(
                    ne_approval_requests.c.status,
                    ne_approval_requests.c.tier,
                    ne_approval_requests.c.action,
                )
                .where(ne_approval_requests.c.request_id == approval_id)
                .with_for_update()
            ).one()
            if (
                str(request.status) != "pending"
                or int(request.tier) != tier
                or not hmac.compare_digest(str(request.action), action)
            ):
                return None
            consumed = conn.execute(
                ne_agent_standing_approvals.update()
                .where(
                    ne_agent_standing_approvals.c.standing_id == standing_id,
                    ne_agent_standing_approvals.c.status == "standing",
                )
                .values(
                    status="consumed",
                    consumed_at=now,
                    successor_approval_id=approval_id,
                    successor_run_id=str(joined.run_id),
                    successor_attempt_id=str(joined.attempt_id),
                )
            )
            if consumed.rowcount != 1:
                # AT MOST ONCE. Another successor crossed this fence first.
                return None
            decided = conn.execute(
                ne_approval_requests.update()
                .where(
                    ne_approval_requests.c.request_id == approval_id,
                    ne_approval_requests.c.status == "pending",
                )
                .values(
                    status="approved",
                    first_approver=str(row.approver),
                    second_approver=(
                        None if row.second_approver is None else str(row.second_approver)
                    ),
                    decided_reason=STANDING_APPROVAL_REASON,
                    decided_at=now,
                )
            )
            if decided.rowcount != 1:
                raise RuntimeError("standing approval consumption lost its exact decision CAS")
            append(conn, event.model_copy(update={"approval_id": approval_id}))
            conn.execute(
                ne_approval_evidence.insert().values(
                    request_id=approval_id,
                    action=action,
                    tier=tier,
                    outcome="approved",
                    approver=str(row.approver),
                    decided_at=now,
                    clean=True,
                )
            )
            checkpoint = conn.execute(
                ne_agent_run_continuation_checkpoints.update()
                .where(
                    ne_agent_run_continuation_checkpoints.c.checkpoint_id == joined.checkpoint_id,
                    ne_agent_run_continuation_checkpoints.c.status == "pending",
                )
                .values(status="approved", updated_at=now)
            )
            if checkpoint.rowcount != 1:
                raise RuntimeError("standing approval consumption has no pending exact checkpoint")
            return _standing(row)

    async def get_join(self, approval_id: str) -> RunApprovalJoin | None:
        return await self._runs._call_db(self._get_join_sync, approval_id)

    def _get_join_sync(self, approval_id: str) -> RunApprovalJoin | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(ne_agent_run_approval_joins).where(
                    ne_agent_run_approval_joins.c.approval_id == approval_id
                )
            ).one_or_none()
            return None if row is None else _join(row)

    async def authorize(
        self,
        approval_id: str,
        *,
        guard: ProtectedSourceResultGuard,
        operation: str,
        decided_at: datetime,
    ) -> dict[str, Any] | None:
        """Rehydrate and reauthorize the exact joined invocation.

        Non-authorized outcomes split by WHOSE authority the bound context
        carries (U2 adversarial review, F1):

        * ``operation="resume"`` runs under the RUN OWNER's bound context — a
          permanent withholding there is a genuine authority decision about the
          run itself, so it is persisted (``reconcile_source_authority``) and
          ``None`` is returned.
        * every other operation (``"detail"``) runs under an APPROVER's bound
          context — a permanent withholding there is only a fact about that
          reader's ACL standing, so it raises
          :class:`ApprovalSourceWithheldFromApproverError`, persists NOTHING,
          and the request stays pending for another approver. A read must never
          terminalize a Run.

        Retryable/indeterminate authority raises
        :class:`RunApprovalAuthorityRetryableError` while leaving the wait
        pending and non-actionable. TYPED rather than a bare ``RuntimeError``
        because every ne-runtime error IS a ``RuntimeError``, so a handler that
        writes ``except RuntimeError`` to mean "the retryable arm" is in fact
        catching this whole package's hierarchy — the fail-closed
        ``ProtectedExecutionError`` family, which this chain does raise,
        included. ⚠ THREE CALL SITES, and none of them NAMES the class
        yet: the approval DETAIL GET (``ne-api/routers/approvals.py``,
        ``operation="detail"``) catches ``(LookupError, RuntimeError)`` and
        answers 409 for both; ``RunWorkflowService._resolve_effect_continuation``
        (``run_workflow.py``, ``operation="resume"``) wraps it in no handler at
        all; and ``ne-worker/workflows.py`` (``operation="resume"``, twice)
        catches only ``AuthorizationExpiredError``. The DETAIL GET is
        deliberately left coarse: it is a READ surface that persists nothing and
        terminalizes nothing, so a broad refusal there costs no terminal. The
        two resume sites are not doing coarse catching at all — they let the
        arm propagate — which is why nothing there needs narrowing either. The
        successor gate, where a swallowed ``AuthorizationExpiredError`` DOES
        cost a terminal, is the seam that catches the class by name.

        The protected invocation row exists under its PARK-time key only: owner
        = the RUN owner, binding ``(run_id, attempt_id, step_id, tool_call_id,
        approval_id)``. The load below therefore uses the JOIN's identity and
        run-keyed prefix (with target hash, AAD-bound decryption and stamp-set
        hash enforced by the store) plus an hmac echo of the join's own
        invocation hash. The guard then re-evaluates every stamp for the CURRENT
        bound context via :meth:`ProtectedSourceResultGuard.resolve_stored_for_approval`
        — it must NOT re-load by a decision-time ``(approval_id,
        decision_attempt_id)`` key, which is computed here at decision time and
        can never match the park-time row (the structural always-409).
        """
        result = await self._resolve_current_source_authority(
            approval_id, guard=guard, operation=operation
        )
        if result.retryable:
            raise RunApprovalAuthorityRetryableError(
                "Run approval authority is retryable and remains pending"
            )
        if not result.authorized:
            if operation == "resume":
                await self.reconcile_source_authority(
                    approval_id, decision="permanent_withheld", decided_at=decided_at
                )
                return None
            raise ApprovalSourceWithheldFromApproverError(
                "current source authority does not admit this approver to the "
                "parked invocation; the request remains pending for an approver "
                "who holds authority over every underlying source"
            )
        try:
            arguments = json.loads(result.text)
        except ValueError as exc:
            raise RunApprovalAuthorityRetryableError(
                "Run approval invocation is unavailable"
            ) from exc
        if not isinstance(arguments, dict):
            raise RunApprovalAuthorityRetryableError("Run approval invocation is unavailable")
        return arguments

    async def check_decision_authority(
        self,
        approval_id: str,
        *,
        guard: ProtectedSourceResultGuard,
        operation: str,
    ) -> None:
        """Authority floor for a decide path — no plaintext leaves this method.

        The reviewer-confirmed fact behind this seam (U2 adversarial review,
        F1): neither ``approve`` nor ``reject`` consumes the decrypted
        arguments — the sole disclosure consumer is the payload GET. So the
        decide paths call THIS method, which re-evaluates every stamp for the
        deciding principal's bound context, validates that the invocation still
        parses to the JSON argument mapping the resume will need (Sol review,
        P1 — a decision must never persist against an unresumable invocation),
        and then discards everything. (The stamps are packed inside the AEAD
        envelope with the plaintext, so the store must still open the row to
        evaluate them; what this method removes is the parsed argument mapping
        crossing into the API layer for no consumer.)

        Outcomes: authorized returns ``None``; retryable raises
        :class:`RunApprovalAuthorityRetryableError` (still pending, retry
        later); non-authorized raises
        :class:`ApprovalSourceWithheldFromApproverError` (still pending for
        another approver, nothing persisted, nothing terminal — an approver who
        cannot see the material may not decide on it, and their ACL standing is
        never recorded as a source revocation).

        THOSE THREE ARE THIS METHOD'S OWN ANSWERS AND ARE NOT ALL THAT CAN COME
        OUT OF IT. The resolve chain below AUTHENTICATES BEFORE IT EVALUATES:
        ``resolve_stored_for_approval``'s first statement is
        ``self.authorization()``, which raises ``AuthorizationExpiredError`` past
        the bound snapshot's expiry and ``MissingAuthenticatedContextError`` with
        no snapshot bound — both ``ProtectedExecutionError``, both fail-closed,
        both raising straight through this method. ``LookupError`` comes out of
        ``_resolve_current_source_authority`` for a missing join or a drifted
        invocation hash. So a caller that means "the retryable arm" must catch
        :class:`RunApprovalAuthorityRetryableError` and never ``RuntimeError`` —
        the latter is the base class of every error named here.
        """
        result = await self._resolve_current_source_authority(
            approval_id, guard=guard, operation=operation
        )
        if result.retryable:
            raise RunApprovalAuthorityRetryableError(
                "Run approval authority is retryable and remains pending"
            )
        if not result.authorized:
            raise ApprovalSourceWithheldFromApproverError(
                "current source authority does not admit this approver to the "
                "parked invocation; the request remains pending for an approver "
                "who holds authority over every underlying source"
            )
        # Shape floor (Sol cross-model review, P1): the old materializing
        # ``authorize`` also proved the invocation parses to the argument
        # MAPPING the resume will need; dropping that with the materialization
        # let a decision persist-and-enqueue against an unresumable invocation.
        # Validate locally and DISCARD — nothing is returned to the caller.
        try:
            arguments = json.loads(result.text)
        except ValueError as exc:
            raise RunApprovalAuthorityRetryableError(
                "Run approval invocation is unavailable"
            ) from exc
        if not isinstance(arguments, dict):
            raise RunApprovalAuthorityRetryableError("Run approval invocation is unavailable")
        return None

    async def _resolve_current_source_authority(
        self,
        approval_id: str,
        *,
        guard: ProtectedSourceResultGuard,
        operation: str,
    ):
        """Load the park-keyed invocation and stamp-evaluate it for the bound context."""
        joined = await self.get_join(approval_id)
        if joined is None:
            raise LookupError("Run approval join unavailable")
        stored = await self._runs.load_protected_source_invocation(
            reference_id=joined.protected_invocation_id,
            owner_principal_id=joined.principal_id,
            target_kind="approval_action",
            target_id=joined.run_id,
            attempt_id=joined.attempt_id,
        )
        if not hmac.compare_digest(stored.plaintext_hash, joined.protected_invocation_hash):
            raise LookupError("Run approval protected invocation changed")
        reference = ProtectedSourceInvocationRef(
            reference_id=joined.protected_invocation_id,
            stamps=stored.stamps,
            invocation_hash=joined.protected_invocation_hash,
        )
        attempt = _hash(json.dumps([approval_id, operation], separators=(",", ":")).encode())
        return await guard.resolve_stored_for_approval(
            stored,
            reference,
            approval_id=approval_id,
            decision_attempt_id=attempt,
        )

    async def load_checkpoint(
        self, approval_id: str, *, expected_action_hash: str, principal_id: str, policy_hash: str
    ) -> RunContinuationCheckpoint:
        return await self._runs._call_db(
            self._load_checkpoint_sync, approval_id, expected_action_hash, principal_id, policy_hash
        )

    def _load_checkpoint_sync(
        self, approval_id: str, action_hash: str, principal_id: str, policy_hash: str
    ) -> RunContinuationCheckpoint:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(ne_agent_run_approval_joins, ne_agent_run_continuation_checkpoints)
                .join(
                    ne_agent_run_continuation_checkpoints,
                    ne_agent_run_continuation_checkpoints.c.checkpoint_id
                    == ne_agent_run_approval_joins.c.checkpoint_id,
                )
                .where(ne_agent_run_approval_joins.c.approval_id == approval_id)
            ).one_or_none()
        if row is None or not (
            hmac.compare_digest(str(row.action_hash), action_hash)
            and hmac.compare_digest(str(row.principal_id), principal_id)
            and hmac.compare_digest(str(row.policy_hash), policy_hash)
        ):
            raise LookupError("Run approval continuation unavailable")
        return _checkpoint(row)

    async def decide(
        self,
        approval_id: str,
        *,
        status: str,
        expected_action_hash: str,
        principal_id: str,
        policy_hash: str,
        approver: str | None,
        reason: str | None,
        decided_at: datetime,
        event: ApprovalEvent,
        second_approver: str | None = None,
    ) -> bool:
        """Land a joined decision and checkpoint CAS in one transaction."""
        if status not in {"approved", "rejected", "expired"}:
            raise ValueError("unsupported Run approval decision")
        if status == "approved" and not approver:
            raise ValueError("approved Run continuation requires a named approver")
        return await self._runs._call_db(
            self._decide_sync,
            approval_id,
            status,
            expected_action_hash,
            principal_id,
            policy_hash,
            approver,
            second_approver,
            reason,
            decided_at,
            event,
        )

    def _decide_sync(
        self,
        approval_id: str,
        status: str,
        action_hash: str,
        principal_id: str,
        policy_hash: str,
        approver: str | None,
        second_approver: str | None,
        reason: str | None,
        now: datetime,
        event: ApprovalEvent,
    ) -> bool:
        from ne.runtime.run_workflow import ne_agent_run_workflow_claims

        with self._engine.begin() as conn:
            joined = conn.execute(
                sa.select(ne_agent_run_approval_joins)
                .where(ne_agent_run_approval_joins.c.approval_id == approval_id)
                .with_for_update()
            ).one_or_none()
            if joined is None or not (
                hmac.compare_digest(str(joined.action_hash), action_hash)
                and hmac.compare_digest(str(joined.principal_id), principal_id)
                and hmac.compare_digest(str(joined.policy_hash), policy_hash)
            ):
                raise LookupError("Run approval decision binding unavailable")
            request = conn.execute(
                sa.select(
                    ne_approval_requests.c.action,
                    ne_approval_requests.c.tier,
                    ne_approval_requests.c.status,
                    ne_approval_requests.c.first_approver,
                ).where(ne_approval_requests.c.request_id == approval_id)
            ).one()
            _require_parked_governance(joined, tier=request.tier, action=request.action)
            if (
                status == "approved"
                and int(request.tier) >= 3
                and (
                    str(request.status) != "pending_second"
                    or not approver
                    or str(request.first_approver or "") != approver
                    or not second_approver
                    or second_approver == approver
                )
            ):
                raise ValueError("Tier-3 Run approval requires two distinct named approvers")
            changed = conn.execute(
                ne_approval_requests.update()
                .where(
                    ne_approval_requests.c.request_id == approval_id,
                    ne_approval_requests.c.status.in_(PENDING_STATUSES),
                )
                .values(
                    status=status,
                    first_approver=approver,
                    second_approver=second_approver,
                    decided_reason=reason,
                    decided_at=now,
                )
            )
            if changed.rowcount != 1:
                return False
            append(conn, event.model_copy(update={"approval_id": approval_id}))
            conn.execute(
                ne_approval_evidence.insert().values(
                    request_id=approval_id,
                    action=str(request.action),
                    tier=int(request.tier),
                    outcome=status,
                    approver=approver,
                    decided_at=now,
                    clean=status == "approved",
                )
            )
            checkpoint = conn.execute(
                ne_agent_run_continuation_checkpoints.update()
                .where(
                    ne_agent_run_continuation_checkpoints.c.checkpoint_id == joined.checkpoint_id,
                    ne_agent_run_continuation_checkpoints.c.status == "pending",
                )
                .values(status=status, updated_at=now)
            )
            if checkpoint.rowcount != 1:
                raise RuntimeError("approval decision has no pending exact checkpoint")
            workflow = conn.execute(
                sa.select(
                    ne_agent_run_workflow_claims.c.dispatch_id,
                    ne_agent_run_workflow_claims.c.owner_principal_id,
                    ne_agent_run_workflow_claims.c.config_hash,
                ).where(
                    ne_agent_run_workflow_claims.c.run_id == joined.run_id,
                    ne_agent_run_workflow_claims.c.attempt_id == joined.attempt_id,
                    ne_agent_run_workflow_claims.c.owner_principal_id == joined.principal_id,
                )
            ).one_or_none()
            if workflow is None:
                raise RuntimeError("approval decision has no exact durable workflow join")
            conn.execute(
                ne_agent_run_approval_resume_intents.insert().values(
                    resume_id=str(uuid.uuid4()),
                    approval_id=approval_id,
                    decision=status,
                    run_id=str(joined.run_id),
                    attempt_id=str(joined.attempt_id),
                    dispatch_id=str(workflow.dispatch_id),
                    owner_principal_id=str(workflow.owner_principal_id),
                    config_hash=str(workflow.config_hash),
                    action_hash=str(joined.action_hash),
                    policy_hash=str(joined.policy_hash),
                    created_at=now,
                )
            )
            return True

    async def record_first_approval(
        self,
        approval_id: str,
        *,
        expected_action_hash: str,
        principal_id: str,
        policy_hash: str,
        approver: str,
        decided_at: datetime,
        event: ApprovalEvent,
    ) -> bool:
        """Land only Tier-3's first named check; the Run remains parked."""

        if not approver:
            raise ValueError("Tier-3 first approval requires a named approver")
        return await self._runs._call_db(
            self._record_first_approval_sync,
            approval_id,
            expected_action_hash,
            principal_id,
            policy_hash,
            approver,
            decided_at,
            event,
        )

    def _record_first_approval_sync(
        self,
        approval_id: str,
        action_hash: str,
        principal_id: str,
        policy_hash: str,
        approver: str,
        now: datetime,
        event: ApprovalEvent,
    ) -> bool:
        with self._engine.begin() as conn:
            joined = conn.execute(
                sa.select(ne_agent_run_approval_joins)
                .where(ne_agent_run_approval_joins.c.approval_id == approval_id)
                .with_for_update()
            ).one_or_none()
            if joined is None or not (
                hmac.compare_digest(str(joined.action_hash), action_hash)
                and hmac.compare_digest(str(joined.principal_id), principal_id)
                and hmac.compare_digest(str(joined.policy_hash), policy_hash)
            ):
                raise LookupError("Run approval first-check binding unavailable")
            request = conn.execute(
                sa.select(ne_approval_requests.c.action, ne_approval_requests.c.tier).where(
                    ne_approval_requests.c.request_id == approval_id
                )
            ).one()
            _require_parked_governance(joined, tier=request.tier, action=request.action)
            changed = conn.execute(
                ne_approval_requests.update()
                .where(
                    ne_approval_requests.c.request_id == approval_id,
                    ne_approval_requests.c.status == "pending",
                    ne_approval_requests.c.tier >= 3,
                )
                .values(status="pending_second", first_approver=approver)
            )
            if changed.rowcount != 1:
                return False
            append(conn, event.model_copy(update={"approval_id": approval_id}))
            return True

    async def load_resume_request(self, approval_id: str) -> RunApprovalResumeRequest:
        """Load the sole decision-bound resume intent; never derive it from an event."""

        return await self._runs._call_db(self._load_resume_request_sync, approval_id)

    def _load_resume_request_sync(self, approval_id: str) -> RunApprovalResumeRequest:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(ne_agent_run_approval_resume_intents).where(
                    ne_agent_run_approval_resume_intents.c.approval_id == approval_id
                )
            ).one_or_none()
        if row is None:
            raise LookupError("Run approval resume intent is unavailable")
        return RunApprovalResumeRequest(
            **{field: str(getattr(row, field)) for field in RunApprovalResumeRequest.model_fields}
        )

    async def require_resume_request(
        self, request: RunApprovalResumeRequest
    ) -> RunApprovalResumeRequest:
        persisted = await self.load_resume_request(request.approval_id)
        if not hmac.compare_digest(persisted.model_dump_json(), request.model_dump_json()):
            raise LookupError("Run approval resume request changed")
        return persisted

    async def load_pending_resume_requests(
        self, *, limit: int = 100
    ) -> tuple[RunApprovalResumeRequest, ...]:
        """Load decision intents whose exact canonical Run is still parked.

        The workflow id is the persisted ``resume_id``. Until the resume worker
        changes the Run out of ``waiting_approval``, repeated reconciliation is a
        DBOS-idempotent enqueue of that one identity and closes the API
        commit-before-enqueue crash window.
        """

        if limit < 1 or limit > 1000:
            raise ValueError("pending approval resume limit must be between 1 and 1000")
        return await self._runs._call_db(self._load_pending_resume_requests_sync, limit)

    def _load_pending_resume_requests_sync(
        self, limit: int
    ) -> tuple[RunApprovalResumeRequest, ...]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.select(ne_agent_run_approval_resume_intents)
                .join(
                    ne_agent_runs,
                    ne_agent_runs.c.run_id == ne_agent_run_approval_resume_intents.c.run_id,
                )
                .join(
                    ne_agent_run_approval_joins,
                    ne_agent_run_approval_joins.c.approval_id
                    == ne_agent_run_approval_resume_intents.c.approval_id,
                )
                .join(
                    ne_agent_run_continuation_checkpoints,
                    ne_agent_run_continuation_checkpoints.c.checkpoint_id
                    == ne_agent_run_approval_joins.c.checkpoint_id,
                )
                .where(
                    ne_agent_runs.c.status == RunState.WAITING_APPROVAL.value,
                    ne_agent_run_continuation_checkpoints.c.status
                    == ne_agent_run_approval_resume_intents.c.decision,
                )
                .order_by(ne_agent_run_approval_resume_intents.c.created_at)
                .limit(limit)
            ).all()
        return tuple(
            RunApprovalResumeRequest(
                **{
                    field: str(getattr(row, field))
                    for field in RunApprovalResumeRequest.model_fields
                }
            )
            for row in rows
        )

    async def reconcile_source_authority(
        self, approval_id: str, *, decision: str, decided_at: datetime
    ) -> bool:
        """Permanent denial cancels atomically; retryable Unknown stays pending.

        Reachable ONLY from a genuine authority decision about the run itself —
        the resume-side re-evaluation under the RUN OWNER's bound context
        (``authorize(operation="resume")``). An approver-context refusal must
        NEVER call this (U2 adversarial review, F1): a reader's ACL standing is
        not a revocation, and recording it as one is a false audit record.
        """
        if decision == "retryable_unknown":
            return False
        if decision != "permanent_withheld":
            raise ValueError("unknown source-authority decision")
        return await self._runs._call_db(self._withhold_sync, approval_id, decided_at)

    def _withhold_sync(self, approval_id: str, now: datetime) -> bool:
        from ne.runtime.run_finalizer import RunFinalizer, RunUnavailableTerminalCandidate
        from ne.runtime.run_workflow import RunWorkflowStateStore, _database_now

        with self._engine.begin() as conn:
            joined = conn.execute(
                sa.select(ne_agent_run_approval_joins).where(
                    ne_agent_run_approval_joins.c.approval_id == approval_id
                )
            ).one_or_none()
            if joined is None:
                raise LookupError("Run approval join unavailable")
            # Run is the lifecycle linearization point. Lock it before mutable
            # approval/checkpoint rows so this path cannot form the retired
            # approval->Run reverse-lock cycle.
            run_status = RunState(
                str(
                    conn.execute(
                        sa.select(ne_agent_runs.c.status)
                        .where(ne_agent_runs.c.run_id == joined.run_id)
                        .with_for_update()
                    ).scalar_one()
                )
            )
            approval_status = str(
                conn.execute(
                    sa.select(ne_approval_requests.c.status)
                    .where(ne_approval_requests.c.request_id == approval_id)
                    .with_for_update()
                ).scalar_one()
            )
            checkpoint_status = str(
                conn.execute(
                    sa.select(ne_agent_run_continuation_checkpoints.c.status)
                    .where(
                        ne_agent_run_continuation_checkpoints.c.checkpoint_id
                        == joined.checkpoint_id
                    )
                    .with_for_update()
                ).scalar_one()
            )
            exact_source_check_pairs = {
                ("pending", "pending", RunState.WAITING_APPROVAL),
                ("pending_second", "pending", RunState.WAITING_APPROVAL),
                ("approved", "approved", RunState.WAITING_APPROVAL),
                ("approved", "resumed", RunState.RUNNING),
            }
            if (approval_status, checkpoint_status, run_status) not in exact_source_check_pairs:
                return False
            approval = conn.execute(
                ne_approval_requests.update()
                .where(
                    ne_approval_requests.c.request_id == approval_id,
                    ne_approval_requests.c.status == approval_status,
                )
                .values(
                    status="source_withheld",
                    # Attribution matters (F1): this transition is reachable only
                    # from the resume-side re-evaluation under the run owner's
                    # bound context, so the record names WHOSE authority was
                    # denied instead of asserting an unattributed revocation.
                    decided_reason=(
                        "run owner's current source authority permanently denied at resume"
                    ),
                    decided_at=now,
                )
            )
            if approval.rowcount != 1:
                return False
            checkpoint = conn.execute(
                ne_agent_run_continuation_checkpoints.update()
                .where(
                    ne_agent_run_continuation_checkpoints.c.checkpoint_id == joined.checkpoint_id,
                    ne_agent_run_continuation_checkpoints.c.status == checkpoint_status,
                )
                .values(status="source_withheld", updated_at=now)
            )
            if checkpoint.rowcount != 1:
                raise RuntimeError("source withholding lost its exact continuation CAS")
            lifecycle = RunWorkflowStateStore(self._engine, run_store=self._runs)
            target, lease = lifecycle._approval_finalizer_context(
                conn, joined=joined, now=_database_now(conn)
            )
            RunFinalizer(self._runs).finalize_unavailable_in_transaction(
                conn,
                RunUnavailableTerminalCandidate(
                    target=target,
                    status="failed",
                    outcome="failed",
                    failure_class="SourceAuthorityWithheld",
                ),
                lease=lease,
            )
            return True

    async def resume(
        self,
        approval_id: str,
        *,
        expected_action_hash: str,
        principal_id: str,
        policy_hash: str,
        resumed_at: datetime,
    ) -> RunContinuationCheckpoint:
        """Claim an approved continuation by approval id, never latest event."""
        return await self._runs._call_db(
            self._resume_sync,
            approval_id,
            expected_action_hash,
            principal_id,
            policy_hash,
            resumed_at,
        )

    def _resume_sync(
        self,
        approval_id: str,
        action_hash: str,
        principal_id: str,
        policy_hash: str,
        now: datetime,
    ) -> RunContinuationCheckpoint:
        with self._engine.begin() as conn:
            row = conn.execute(
                sa.select(ne_agent_run_approval_joins, ne_agent_run_continuation_checkpoints)
                .join(
                    ne_agent_run_continuation_checkpoints,
                    ne_agent_run_continuation_checkpoints.c.checkpoint_id
                    == ne_agent_run_approval_joins.c.checkpoint_id,
                )
                .where(ne_agent_run_approval_joins.c.approval_id == approval_id)
                .with_for_update()
            ).one_or_none()
            if row is None or not (
                hmac.compare_digest(str(row.action_hash), action_hash)
                and hmac.compare_digest(str(row.principal_id), principal_id)
                and hmac.compare_digest(str(row.policy_hash), policy_hash)
            ):
                raise LookupError("Run approval continuation unavailable")
            checkpoint = conn.execute(
                ne_agent_run_continuation_checkpoints.update()
                .where(
                    ne_agent_run_continuation_checkpoints.c.checkpoint_id == row.checkpoint_id,
                    ne_agent_run_continuation_checkpoints.c.status == "approved",
                )
                .values(status="resumed", updated_at=now)
            )
            run = conn.execute(
                ne_agent_runs.update()
                .where(
                    ne_agent_runs.c.run_id == row.run_id,
                    ne_agent_runs.c.status == RunState.WAITING_APPROVAL.value,
                )
                .values(
                    status=RunState.RUNNING.value,
                    version=ne_agent_runs.c.version + 1,
                    updated_at=now,
                )
            )
            if checkpoint.rowcount != 1 or run.rowcount != 1:
                raise LookupError("Run approval continuation is not resumable")
            return replace(_checkpoint(row), status="resumed")


_CONTINUATION_STATUS_CONSTRAINT = "ck_run_continuation_status"
_CONTINUATION_STATUS_DDL = (
    "status IN ('pending','approved','resumed','consumed','rejected','expired','source_withheld')"
)


def ensure_continuation_status_compatibility(conn: sa.Connection) -> None:
    """Admit the ``consumed`` continuation status on an EXISTING database.

    ``metadata.create_all`` never replaces a check constraint that already
    exists, so a database created before the consume fence still refuses
    ``consumed`` — and the FIRST approval resume after deploy would die on its
    own fence CAS (``consume_resumed``) with a constraint violation.  A manual
    runbook DDL step is the weaker instrument: this runs at every boot under
    ``ensure_run_schema``'s schema lock, exactly like the other compatibility
    routines.  Postgres swaps the constraint in place
    (the ``_ensure_submission_schema_compatibility`` pattern); SQLite cannot
    replace a CHECK in place, so the one table is rebuilt with rows preserved
    (the ``_rebuild_sqlite_run_lifecycle_table`` pattern — the caller already
    holds ``PRAGMA foreign_keys=OFF`` and runs ``PRAGMA foreign_key_check``
    afterwards).
    """

    inspector = sa.inspect(conn)
    if not inspector.has_table(ne_agent_run_continuation_checkpoints.name):
        return
    constraints = {
        str(constraint.get("name")): "".join(str(constraint.get("sqltext") or "").lower().split())
        for constraint in inspector.get_check_constraints(
            ne_agent_run_continuation_checkpoints.name
        )
    }
    current = constraints.get(_CONTINUATION_STATUS_CONSTRAINT)
    if current is not None and "'consumed'" in current:
        return
    if conn.dialect.name == "postgresql":
        if current is not None:
            conn.exec_driver_sql(
                f"ALTER TABLE {ne_agent_run_continuation_checkpoints.name} "
                f'DROP CONSTRAINT "{_CONTINUATION_STATUS_CONSTRAINT}"'
            )
        conn.exec_driver_sql(
            f"ALTER TABLE {ne_agent_run_continuation_checkpoints.name} "
            f"ADD CONSTRAINT {_CONTINUATION_STATUS_CONSTRAINT} "
            f"CHECK ({_CONTINUATION_STATUS_DDL})"
        )
        return
    if conn.dialect.name != "sqlite":
        raise RuntimeError(
            "run continuation schema does not support "
            f"{conn.dialect.name!r}; apply manually: ALTER TABLE "
            f"{ne_agent_run_continuation_checkpoints.name} DROP CONSTRAINT "
            f"{_CONTINUATION_STATUS_CONSTRAINT}; ADD CONSTRAINT "
            f"{_CONTINUATION_STATUS_CONSTRAINT} CHECK ({_CONTINUATION_STATUS_DDL})"
        )
    if current is None:
        # No named status constraint at all: nothing refuses `consumed`.
        return
    _rebuild_sqlite_continuation_table(conn)


def _rebuild_sqlite_continuation_table(conn: sa.Connection) -> None:
    """Replace only the continuation table with the current status constraint."""

    from ne.runtime.run_store import (
        ne_agent_protected_source_invocations,
        ne_agent_run_attempt_links,
        ne_agent_runs,
    )

    preparer = conn.dialect.identifier_preparer
    temporary_name = "ne_agent_run_continuation_checkpoints_next"
    temporary = preparer.quote(temporary_name)
    canonical = preparer.quote(ne_agent_run_continuation_checkpoints.name)
    replacement_metadata = sa.MetaData()
    # FK targets must exist in the replacement metadata for DDL rendering.
    ne_agent_runs.to_metadata(replacement_metadata)
    ne_agent_run_attempt_links.to_metadata(replacement_metadata)
    ne_agent_protected_source_invocations.to_metadata(replacement_metadata)
    replacement = ne_agent_run_continuation_checkpoints.to_metadata(
        replacement_metadata,
        name=temporary_name,
    )
    if (
        conn.execute(
            sa.text("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = :name"),
            {"name": temporary_name},
        ).first()
        is not None
    ):
        raise RuntimeError("stale continuation migration table requires operator recovery")
    conn.execute(sa.schema.CreateTable(replacement))
    column_names = [column.name for column in ne_agent_run_continuation_checkpoints.columns]
    columns_sql = ", ".join(preparer.quote(name) for name in column_names)
    conn.exec_driver_sql(
        f"INSERT INTO {temporary} ({columns_sql}) SELECT {columns_sql} FROM {canonical}"
    )
    source_count = int(conn.exec_driver_sql(f"SELECT count(*) FROM {canonical}").scalar_one())
    target_count = int(conn.exec_driver_sql(f"SELECT count(*) FROM {temporary}").scalar_one())
    if source_count != target_count:
        raise RuntimeError("continuation migration row count changed")
    conn.exec_driver_sql(f"DROP TABLE {canonical}")
    conn.exec_driver_sql(f"ALTER TABLE {temporary} RENAME TO {canonical}")
    for index in sorted(
        ne_agent_run_continuation_checkpoints.indexes, key=lambda value: str(value.name)
    ):
        if index.name is None:
            raise RuntimeError("continuation index lacks an explicit name")
        index_name = preparer.quote(index.name)
        index_columns = ", ".join(preparer.quote(column.name) for column in index.columns)
        uniqueness = "UNIQUE " if index.unique else ""
        conn.exec_driver_sql(
            f"CREATE {uniqueness}INDEX {index_name} ON {canonical} ({index_columns})"
        )
