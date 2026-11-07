# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Durable state for the worker-owned canonical Agent Run workflow.

The canonical Run record is owned by :mod:`ne.runtime.run_store`.  This module
adds the execution join, renewable lease, content-free checkpoint ledger and
PRE-010E's lifecycle terminal publication.  It deliberately does not add
approval continuations (010C), execution profiles (010D), claim/output
finalization (010F), or API submission behavior (010A/010G).

Every worker dispatch first joins the opaque dispatch id to exactly one existing
Run and one server-minted attempt.  A replay/takeover can renew a lease only by
loading that same durable join; possession of a Run id is insufficient.  Safe
checkpoints are claim-or-load records, so a replay with changed checkpoint intent
fails closed instead of silently adopting the first writer's state.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import hmac
import json
import os
import tempfile
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, model_validator

from ne.audit import records_by_event_ids
from ne.contracts.governance import AuditSink
from ne.contracts.identity import AuthenticatedRequestContext
from ne.contracts.models import ModelGateway
from ne.events import new_audit_event
from ne.runtime.run_audit_receipts import (
    RunAuditReceiptSink,
    RunAuditTarget,
    ensure_run_audit_intent_schema,
    run_observation_audit_scope,
)
from ne.runtime.run_checkpoint import RunApprovalWaiting
from ne.runtime.run_finalizer import (
    RunFinalizer,
    RunFinalizerLease,
    RunFinalizerReceipt,
    RunTerminalCandidate,
    RunTurnTarget,
    RunUnavailableTerminalCandidate,
    published_failure_reason,
)
from ne.runtime.run_runner import (
    ReceiptGatedGateway,
    ReceiptGatedToolExecutor,
    RunCancellationRequested,
    RunEffectContinuation,
    RunExecutionAdapter,
    RunExecutionBoundary,
    RunExecutionContext,
    RunExecutionResult,
    RunLeaseLost,
)
from ne.runtime.run_store import (
    AgentRun,
    AgentRunStore,
    RunAuditDecisionKind,
    RunOutputVerdict,
    RunState,
    ne_agent_run_attempt_links,
    ne_agent_run_audit_receipts,
    ne_agent_run_claims,
    ne_agent_run_enqueue_outbox,
    ne_agent_run_event_seq,
    ne_agent_run_events,
    ne_agent_run_lifecycle_outbox,
    ne_agent_run_retry_lineage,
    ne_agent_run_turn_targets,
    ne_agent_runs,
)
from ne.runtime.source_results import (
    AuthorizationExpiredError,
    SourceAuthorityWithheldError,
    model_call_scope,
    run_delivery_observation_scope,
)
from ne.runtime.store import metadata
from ne.schema import AuditOutcome

__all__ = [
    "AUTHORIZATION_EXPIRED_REASON",
    "RunExecutionCheckpoint",
    "RunLeaseProbe",
    "RunTerminalPublication",
    "RunWorkflowClaim",
    "RunWorkflowRequest",
    "RunWorkflowResult",
    "RunWorkflowService",
    "RunWorkflowStateStore",
    "commit_run_terminal_in_transaction",
    "ensure_run_workflow_schema",
    "ne_agent_run_lifecycle_outbox",
]

#: The ONE safe reason the specification names for an attempt that died because
#: the run owner's verified authorization snapshot ran out.  It is a static,
#: content-free token: it discloses nothing about the run, the approver or the
#: withheld effect, and it is the string a reader sees instead of a run that
#: silently hangs in ``running`` forever.
AUTHORIZATION_EXPIRED_REASON = "authorization_expired"


ne_agent_run_workflow_claims = sa.Table(
    "ne_agent_run_workflow_claims",
    metadata,
    sa.Column("dispatch_id", sa.String, primary_key=True),
    sa.Column("run_id", sa.String, nullable=False, unique=True),
    sa.Column("owner_principal_id", sa.String, nullable=False),
    sa.Column("attempt_id", sa.String, nullable=False, unique=True),
    sa.Column("config_hash", sa.String(64), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(
        ["run_id", "owner_principal_id"],
        ["ne_agent_runs.run_id", "ne_agent_runs.owner_principal_id"],
        name="fk_run_workflow_claim_owner",
    ),
    sa.UniqueConstraint(
        "dispatch_id", "run_id", "attempt_id", name="uq_run_workflow_claim_full_identity"
    ),
    sa.ForeignKeyConstraint(
        ["attempt_id", "run_id"],
        ["ne_agent_run_attempt_links.attempt_id", "ne_agent_run_attempt_links.run_id"],
        name="fk_run_workflow_claim_attempt",
    ),
    sa.CheckConstraint("length(config_hash) = 64", name="ck_run_workflow_claim_config_hash"),
)

ne_agent_run_workflow_leases = sa.Table(
    "ne_agent_run_workflow_leases",
    metadata,
    sa.Column("run_id", sa.String, primary_key=True),
    sa.Column("dispatch_id", sa.String, nullable=False, unique=True),
    sa.Column("attempt_id", sa.String, nullable=False, unique=True),
    sa.Column("lease_token", sa.String, nullable=False, unique=True),
    sa.Column("lease_epoch", sa.Integer, nullable=False),
    sa.Column("state", sa.String, nullable=False),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(
        ["dispatch_id", "run_id", "attempt_id"],
        [
            "ne_agent_run_workflow_claims.dispatch_id",
            "ne_agent_run_workflow_claims.run_id",
            "ne_agent_run_workflow_claims.attempt_id",
        ],
        name="fk_run_workflow_lease_claim",
    ),
    sa.UniqueConstraint(
        "dispatch_id", "run_id", "attempt_id", name="uq_run_workflow_claim_identity"
    ),
    sa.CheckConstraint("lease_epoch >= 1", name="ck_run_workflow_lease_epoch"),
    sa.CheckConstraint("state IN ('active','released')", name="ck_run_workflow_lease_state"),
)

ne_agent_run_execution_checkpoints = sa.Table(
    "ne_agent_run_execution_checkpoints",
    metadata,
    sa.Column("checkpoint_id", sa.String, primary_key=True),
    sa.Column("run_id", sa.String, nullable=False),
    sa.Column("attempt_id", sa.String, nullable=False),
    sa.Column("stage", sa.String, nullable=False),
    sa.Column("intent_hash", sa.String(64), nullable=False),
    sa.Column("safe_detail", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.UniqueConstraint("run_id", "attempt_id", "stage", name="uq_run_execution_checkpoint"),
    sa.ForeignKeyConstraint(
        ["attempt_id", "run_id"],
        ["ne_agent_run_attempt_links.attempt_id", "ne_agent_run_attempt_links.run_id"],
        name="fk_run_execution_checkpoint_attempt",
    ),
    sa.CheckConstraint("length(intent_hash) = 64", name="ck_run_checkpoint_intent_hash"),
)


#: A stage name is SERVER-AUTHORED — every caller is a literal in this repo. It is
#: still shape-checked, because `heartbeat(stage: str)` is a plain string parameter
#: and "only our own code calls it" is exactly the assumption that put an
#: unvalidated driver string into the audit ledger in `3944763c`. Rejected at the
#: writer rather than sanitized downstream.
_MAX_STAGE_NAME = 64
_STAGE_NAME_ALPHABET = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:-_."
)


def _validated_stage(stage: str) -> str:
    """The stage name to persist, or a raise. Never a silently-repaired value."""
    if not isinstance(stage, str) or not stage or len(stage) > _MAX_STAGE_NAME:
        raise ValueError("Run execution boundary stage name is invalid")
    if any(character not in _STAGE_NAME_ALPHABET for character in stage):
        raise ValueError("Run execution boundary stage name has an unexpected shape")
    return stage


#: POST_DEMO item 12. Where a Run's wall-clock actually goes, recorded from the
#: stage names the execution boundary was ALREADY being handed and throwing away.
#:
#: Measured 2026-08-30 over 7 live runs: a reader waits a median 10.2 s before
#: anything can appear on screen, and 7.6 s of it (worst 34.4 s) is one stretch
#: between admission and the model being chosen that emits nothing at all. The
#: model call itself is 1.5 s — the fastest part of the run. Without spans the
#: only honest thing that could be said about those 7.6 s was that they existed.
#:
#: APPEND-ONLY and deliberately NOT a checkpoint. Execution checkpoints are
#: governance artifacts carrying an intent hash and a replay guard, and they are
#: UNIQUE on (run_id, attempt_id, stage) — but `profile:agentic-before` is
#: legitimately reached from two call sites, so a stage is not unique per attempt
#: and never was. Writing these as checkpoints would both pollute a governance
#: table and trip a replay guard on ordinary control flow.
#:
#: Content-free by construction: a stage NAME and a timestamp. No prompt, no model
#: output, no argument values ever pass through here.
ne_agent_run_stage_spans = sa.Table(
    "ne_agent_run_stage_spans",
    metadata,
    sa.Column("span_id", sa.String, primary_key=True),
    sa.Column("run_id", sa.String, nullable=False),
    sa.Column("attempt_id", sa.String, nullable=False),
    sa.Column("stage", sa.String(_MAX_STAGE_NAME), nullable=False),
    #: The DATABASE's clock, taken inside the same transaction as the lease
    #: renewal, so spans are ordered by one clock and never by a worker's.
    sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
    sa.Index("ix_run_stage_spans_attempt", "run_id", "attempt_id"),
)


#: States in which a boundary crossing is refused, checked OUTSIDE the lease
#: transaction (see ``probe``). A stage span is not written for these, so the
#: table never records a stage the platform went on to reject.
_NON_ADMITTING_RUN_STATES = frozenset(
    {
        RunState.COMPLETED,
        RunState.FAILED,
        RunState.CANCELLED,
        RunState.EXPIRED,
        RunState.CONTEXT_EXHAUSTED,
        RunState.BUDGET_EXHAUSTED,
        RunState.CANCELLING,
    }
)


_SETTLEMENT_FENCE_EXPIRES_AT = datetime(9999, 1, 1, tzinfo=UTC)
_EFFECT_STAGE_PREFIXES = ("run-model:wait:", "run-model:stream-wait:", "run-tool:wait:")


def _effect_checkpoint_address(stage: str) -> str | None:
    if not stage.startswith(_EFFECT_STAGE_PREFIXES):
        return None
    return hashlib.sha256(stage.encode()).hexdigest()


class _RunSettlementGuard:
    """Crash-released ownership guard held while an admitted effect settles."""

    def __init__(
        self,
        *,
        connection: sa.Connection | None = None,
        advisory_key: int | None = None,
        lock_fd: int | None = None,
    ) -> None:
        self._connection = connection
        self._advisory_key = advisory_key
        self._lock_fd = lock_fd

    def __enter__(self) -> _RunSettlementGuard:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        connection, self._connection = self._connection, None
        lock_fd, self._lock_fd = self._lock_fd, None
        if connection is not None:
            try:
                unlocked = connection.execute(
                    sa.text("SELECT pg_advisory_unlock(:key)"),
                    {"key": self._advisory_key},
                ).scalar_one()
                connection.commit()
                if not unlocked:
                    raise RuntimeError("Run settlement advisory lock was not owned")
            except BaseException:
                # A session whose unlock is uncertain must never return to the
                # pool carrying a leaked advisory lock.
                connection.invalidate()
                raise
            finally:
                connection.close()
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)


@dataclass(frozen=True)
class RunWorkflowClaim:
    dispatch_id: str
    run_id: str
    owner_principal_id: str
    attempt_id: str
    lease_token: str
    lease_epoch: int
    recovered: bool
    claim_event_id: str


@dataclass(frozen=True)
class RunExecutionCheckpoint:
    checkpoint_id: str
    run_id: str
    attempt_id: str
    stage: str
    intent_hash: str
    safe_detail: Mapping[str, object]
    created_at: datetime


@dataclass(frozen=True)
class RunLeaseProbe:
    expires_at: datetime
    run_status: RunState
    cancellation_requested: bool


@dataclass(frozen=True)
class RunTerminalPublication:
    publication_id: str
    run_id: str
    owner_principal_id: str
    attempt_id: str
    event_id: str
    status: RunState
    output_verdict: RunOutputVerdict
    outcome: str
    terminal_intent_hash: str
    target_hash: str | None
    protected_output_kind: str | None
    protected_output_ref: str | None
    protected_output_hash: str | None
    mapping_state: str | None
    envelope_hash: str | None
    delivery_set_id: str | None
    delivery_set_hash: str | None
    audit_receipt_set_hash: str | None
    approval_set_hash: str | None
    evidence_set_hash: str | None
    verifier_set_hash: str | None
    claimed_at: datetime | None
    published_at: datetime | None
    audit_event_id: str | None


class RunWorkflowRequest(BaseModel):
    """Content-free serializable DBOS input."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dispatch_id: str
    run_id: str
    owner_principal_id: str
    config_hash: str
    target: RunTurnTarget
    authenticated_context: AuthenticatedRequestContext


class RunWorkflowResult(BaseModel):
    """Content-free serializable DBOS result; output bytes never cross DBOS."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    attempt_id: str
    outcome: Literal[
        "execution_complete",
        "approved_effect_complete",
        "context_exhausted",
        "budget_exhausted",
        "waiting_approval",
        "cancelled",
        "failed",
        "expired",
        "refused",
    ]
    protected_result_ref: str | None = None
    protected_result_hash: str | None = None
    target_hash: str | None = None
    protected_output_kind: str | None = None
    mapping_state: str | None = None
    output_verdict: str | None = None
    envelope_hash: str | None = None
    delivery_set_id: str | None = None
    delivery_set_hash: str | None = None
    audit_receipt_set_hash: str | None = None
    approval_set_hash: str | None = None
    evidence_set_hash: str | None = None
    verifier_set_hash: str | None = None

    @model_validator(mode="after")
    def _validate_protected_result(self) -> RunWorkflowResult:
        if (self.protected_result_ref is None) != (self.protected_result_hash is None):
            raise ValueError("protected Run result ref/hash must be both present or both absent")
        if self.protected_result_hash is not None and (
            len(self.protected_result_hash) != 64
            or any(character not in "0123456789abcdef" for character in self.protected_result_hash)
        ):
            raise ValueError("protected Run result hash must be lowercase SHA-256")
        for name in (
            "target_hash",
            "envelope_hash",
            "delivery_set_hash",
            "audit_receipt_set_hash",
            "approval_set_hash",
            "evidence_set_hash",
            "verifier_set_hash",
        ):
            value = getattr(self, name)
            if value is not None and (
                len(value) != 64 or any(character not in "0123456789abcdef" for character in value)
            ):
                raise ValueError(f"{name} must be lowercase SHA-256")
        return self


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _database_now(conn: sa.Connection) -> datetime:
    if conn.dialect.name == "sqlite":
        value = str(
            conn.exec_driver_sql("SELECT strftime('%Y-%m-%d %H:%M:%f', 'now')").scalar_one()
        )
        return datetime.fromisoformat(value).replace(tzinfo=UTC)
    return _utc(conn.execute(sa.select(sa.func.current_timestamp())).scalar_one())


def _require_exact_cancel_outbox(
    conn: sa.Connection,
    *,
    run_id: str,
    attempt_id: str,
    run_version: int,
) -> None:
    rows = list(
        conn.execute(
            sa.select(ne_agent_run_enqueue_outbox.c.enqueue_id).where(
                ne_agent_run_enqueue_outbox.c.run_id == run_id,
                ne_agent_run_enqueue_outbox.c.attempt_id == attempt_id,
                ne_agent_run_enqueue_outbox.c.action == "cancel",
                ne_agent_run_enqueue_outbox.c.run_version == run_version,
            )
        ).scalars()
    )
    if len(rows) != 1:
        raise RuntimeError("cancelling Run lacks its exact durable cancel admission")


def _checkpoint(row: Any) -> RunExecutionCheckpoint:
    detail = json.loads(str(row.safe_detail))
    if not isinstance(detail, dict):
        raise RuntimeError("Run execution checkpoint safe detail is not an object")
    return RunExecutionCheckpoint(
        checkpoint_id=str(row.checkpoint_id),
        run_id=str(row.run_id),
        attempt_id=str(row.attempt_id),
        stage=str(row.stage),
        intent_hash=str(row.intent_hash),
        safe_detail=detail,
        created_at=_utc(row.created_at),
    )


def _terminal_publication(row: Any) -> RunTerminalPublication:
    return RunTerminalPublication(
        publication_id=str(row.publication_id),
        run_id=str(row.run_id),
        owner_principal_id=str(row.owner_principal_id),
        attempt_id=str(row.attempt_id),
        event_id=str(row.event_id),
        status=RunState(str(row.terminal_status)),
        output_verdict=RunOutputVerdict(str(row.output_verdict)),
        outcome=str(row.outcome),
        terminal_intent_hash=str(row.terminal_intent_hash),
        target_hash=None if row.target_hash is None else str(row.target_hash),
        protected_output_kind=(
            None if row.protected_output_kind is None else str(row.protected_output_kind)
        ),
        protected_output_ref=(
            None if row.protected_output_ref is None else str(row.protected_output_ref)
        ),
        protected_output_hash=(
            None if row.protected_output_hash is None else str(row.protected_output_hash)
        ),
        mapping_state=None if row.mapping_state is None else str(row.mapping_state),
        envelope_hash=None if row.envelope_hash is None else str(row.envelope_hash),
        delivery_set_id=None if row.delivery_set_id is None else str(row.delivery_set_id),
        delivery_set_hash=(None if row.delivery_set_hash is None else str(row.delivery_set_hash)),
        audit_receipt_set_hash=(
            None if row.audit_receipt_set_hash is None else str(row.audit_receipt_set_hash)
        ),
        approval_set_hash=(None if row.approval_set_hash is None else str(row.approval_set_hash)),
        evidence_set_hash=(None if row.evidence_set_hash is None else str(row.evidence_set_hash)),
        verifier_set_hash=(None if row.verifier_set_hash is None else str(row.verifier_set_hash)),
        claimed_at=(None if row.claimed_at is None else _utc(row.claimed_at)),
        published_at=(None if row.published_at is None else _utc(row.published_at)),
        audit_event_id=(None if row.audit_event_id is None else str(row.audit_event_id)),
    )


def _terminal_publication_select() -> sa.Select[Any]:
    return (
        sa.select(
            ne_agent_run_lifecycle_outbox.c.publication_id,
            ne_agent_run_lifecycle_outbox.c.run_id,
            ne_agent_runs.c.owner_principal_id,
            ne_agent_run_lifecycle_outbox.c.attempt_id,
            ne_agent_run_lifecycle_outbox.c.event_id,
            ne_agent_run_lifecycle_outbox.c.terminal_status,
            ne_agent_run_lifecycle_outbox.c.output_verdict,
            ne_agent_run_lifecycle_outbox.c.outcome,
            ne_agent_run_lifecycle_outbox.c.terminal_intent_hash,
            ne_agent_run_lifecycle_outbox.c.target_hash,
            ne_agent_run_lifecycle_outbox.c.protected_output_kind,
            ne_agent_run_lifecycle_outbox.c.protected_output_ref,
            ne_agent_run_lifecycle_outbox.c.protected_output_hash,
            ne_agent_run_lifecycle_outbox.c.mapping_state,
            ne_agent_run_lifecycle_outbox.c.envelope_hash,
            ne_agent_run_lifecycle_outbox.c.delivery_set_id,
            ne_agent_run_lifecycle_outbox.c.delivery_set_hash,
            ne_agent_run_lifecycle_outbox.c.audit_receipt_set_hash,
            ne_agent_run_lifecycle_outbox.c.approval_set_hash,
            ne_agent_run_lifecycle_outbox.c.evidence_set_hash,
            ne_agent_run_lifecycle_outbox.c.verifier_set_hash,
            ne_agent_run_lifecycle_outbox.c.claimed_at,
            ne_agent_run_lifecycle_outbox.c.published_at,
            ne_agent_run_lifecycle_outbox.c.audit_event_id,
        )
        .select_from(ne_agent_run_lifecycle_outbox)
        .join(ne_agent_runs, ne_agent_runs.c.run_id == ne_agent_run_lifecycle_outbox.c.run_id)
    )


def _claim_sequence(conn: sa.Connection, run_id: str) -> int:
    dialect = conn.dialect.name
    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    elif dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:  # pragma: no cover - the repository supports these two state stores
        raise RuntimeError(f"Run workflow does not support {dialect!r}")
    # ONE upsert-RETURNING round trip (both dialects) instead of the old
    # three-statement insert-if-absent / increment / read-back dance — this is
    # paid on every Run event write (perf/audit-intent-path). The UPDATE arm's
    # row lock serializes concurrent claimants exactly as before.
    return int(
        conn.execute(
            insert(ne_agent_run_event_seq)
            .values(run_id=run_id, last_sequence=1)
            .on_conflict_do_update(
                index_elements=["run_id"],
                set_={"last_sequence": ne_agent_run_event_seq.c.last_sequence + 1},
            )
            .returning(ne_agent_run_event_seq.c.last_sequence)
        ).scalar_one()
    )


def _append_safe_event(
    conn: sa.Connection,
    *,
    run_id: str,
    attempt_id: str,
    event_kind: str,
    event_status: str,
    safe_detail: Mapping[str, object],
    step_id: str | None = None,
) -> str:
    forbidden = {"prompt", "output", "content", "arguments", "claim_text", "source_title"}
    if forbidden.intersection(safe_detail):
        raise ValueError("Run workflow event safe detail carries a protected-content field")
    detail = _canonical(dict(safe_detail))
    event_id = str(uuid.uuid4())
    conn.execute(
        ne_agent_run_events.insert().values(
            event_id=event_id,
            run_id=run_id,
            attempt_id=attempt_id,
            sequence=_claim_sequence(conn, run_id),
            event_kind=event_kind,
            event_status=event_status,
            parent_event_id=None,
            step_id=step_id,
            safe_detail=detail,
            detail_hash=hashlib.sha256(detail.encode("utf-8")).hexdigest(),
            created_at=datetime.now(UTC),
        )
    )
    return event_id


def commit_run_terminal_in_transaction(
    conn: sa.Connection,
    *,
    run_id: str,
    attempt_id: str,
    status: RunState,
    outcome: str,
    output_verdict: RunOutputVerdict,
    allowed_from: Mapping[RunState, frozenset[RunState]],
    step_id: str,
    now: datetime,
    terminal_intent_hash: str | None = None,
    finalizer_metadata: Mapping[str, str | None] | None = None,
) -> str:
    """Connection-bound terminal state/event/outbox primitive for E and later F."""

    if status not in {
        RunState.COMPLETED,
        RunState.FAILED,
        RunState.CANCELLED,
        RunState.EXPIRED,
        RunState.CONTEXT_EXHAUSTED,
        RunState.BUDGET_EXHAUSTED,
    }:
        raise ValueError("Run lifecycle terminal status is unsupported")
    if output_verdict is RunOutputVerdict.REFUSED and status is not RunState.COMPLETED:
        raise ValueError("refusal is a completed output verdict, not a Run state")
    if status is not RunState.COMPLETED and output_verdict is not RunOutputVerdict.UNAVAILABLE:
        raise ValueError("non-completed Run terminal states have unavailable output")
    expected_outcome = {
        RunState.FAILED: "failed",
        RunState.CANCELLED: "cancelled",
        RunState.EXPIRED: "expired",
        RunState.CONTEXT_EXHAUSTED: "context_exhausted",
        RunState.BUDGET_EXHAUSTED: "budget_exhausted",
    }.get(status)
    if status is RunState.COMPLETED:
        # POST_DEMO item 4: a completed, non-refused run may say EITHER that it ran
        # its plan to the end, or that it resumed and executed the approved effect.
        # The set stays exact and closed — this is one more admitted value, never a
        # relaxation to "any string when completed". Every other status keeps its
        # single mandatory word.
        expected_completed = (
            frozenset({"refused"})
            if output_verdict is RunOutputVerdict.REFUSED
            else frozenset({"execution_complete", "approved_effect_complete"})
        )
        if outcome not in expected_completed:
            raise ValueError("Run terminal outcome does not match its state and output verdict")
    elif outcome != expected_outcome:
        raise ValueError("Run terminal outcome does not match its state and output verdict")
    intent_hash = terminal_intent_hash or _digest(
        {
            "run_id": run_id,
            "attempt_id": attempt_id,
            "status": status.value,
            "output_verdict": output_verdict.value,
            "outcome": outcome,
        }
    )
    if len(intent_hash) != 64 or any(
        character not in "0123456789abcdef" for character in intent_hash
    ):
        raise ValueError("Run terminal intent hash must be lowercase SHA-256")
    finalizer_values: dict[str, str | None] = {}
    if finalizer_metadata is not None:
        expected_keys = {
            "target_hash",
            "protected_output_kind",
            "protected_output_ref",
            "protected_output_hash",
            "mapping_state",
            "envelope_hash",
            "delivery_set_id",
            "delivery_set_hash",
            "audit_receipt_set_hash",
            "approval_set_hash",
            "evidence_set_hash",
            "verifier_set_hash",
        }
        if set(finalizer_metadata) != expected_keys:
            raise ValueError("Run finalizer lifecycle metadata is incomplete")
        finalizer_values = dict(finalizer_metadata)
        for key in expected_keys - {
            "protected_output_kind",
            "protected_output_ref",
            "mapping_state",
            "delivery_set_id",
        }:
            value = finalizer_values[key]
            if value is not None and (
                len(value) != 64 or any(character not in "0123456789abcdef" for character in value)
            ):
                raise ValueError(f"Run finalizer {key} must be lowercase SHA-256")
    run_row = conn.execute(
        sa.select(ne_agent_runs.c.status, ne_agent_runs.c.output_verdict)
        .where(ne_agent_runs.c.run_id == run_id)
        .with_for_update()
    ).one()
    run_status = RunState(str(run_row.status))
    existing = conn.execute(
        sa.select(ne_agent_run_lifecycle_outbox).where(
            ne_agent_run_lifecycle_outbox.c.run_id == run_id,
            ne_agent_run_lifecycle_outbox.c.attempt_id == attempt_id,
        )
    ).one_or_none()
    if existing is not None:
        if not hmac.compare_digest(str(existing.terminal_intent_hash), intent_hash):
            raise RuntimeError("Run terminal publication changed on replay")
        # POST_DEMO item 4 restored an invariant it had quietly broken, and an
        # adversarial pass named it. Before item 4, a COMPLETED terminal's outcome
        # was a total function of `output_verdict` — which IS inside
        # `finalizer_intent_hash` — so the intent hash determined the published
        # outcome and comparing the hash compared the outcome. Item 4 made the
        # outcome depend on `ne_agent_run_continuation_checkpoints.status`, read
        # live inside the finalize transaction, which no digest covers: a
        # continuation moving out of {resumed, consumed} (an authorization expiry
        # writes `expired`) would make a replaying worker compute
        # `execution_complete` while the hash still matched, and the guard would
        # wave it through. The durable row wins so nothing is mis-written, but C10's
        # whole claim is that the record cannot DRIFT — so the outcome is now
        # compared explicitly rather than trusted to be implied by the hash.
        if str(existing.outcome) != outcome:
            raise RuntimeError("Run terminal outcome changed on replay")
        if finalizer_metadata is not None and any(
            (None if getattr(existing, key) is None else str(getattr(existing, key))) != value
            for key, value in finalizer_values.items()
        ):
            raise RuntimeError("Run terminal finalizer bindings changed on replay")
        linked_event = conn.execute(
            sa.select(ne_agent_run_events.c.event_kind, ne_agent_run_events.c.event_status).where(
                ne_agent_run_events.c.event_id == existing.event_id,
                ne_agent_run_events.c.run_id == run_id,
                ne_agent_run_events.c.attempt_id == attempt_id,
            )
        ).one_or_none()
        if (
            run_status is not status
            or str(run_row.output_verdict) != output_verdict.value
            or linked_event is None
            or str(linked_event.event_kind) != "run.terminal"
            or str(linked_event.event_status) != status.value
        ):
            raise RuntimeError("Run terminal replay lacks its exact committed primary state")
        return str(existing.publication_id)
    if status not in allowed_from.get(run_status, frozenset()):
        raise RuntimeError(f"Run cannot terminalize {run_status.value}->{status.value}")
    if output_verdict is RunOutputVerdict.REFUSED:
        claim_count = int(
            conn.execute(
                sa.select(sa.func.count())
                .select_from(ne_agent_run_claims)
                .where(ne_agent_run_claims.c.run_id == run_id)
            ).scalar_one()
        )
        if claim_count:
            raise RuntimeError("refused completion must contain zero claims")
    event_id = _append_safe_event(
        conn,
        run_id=run_id,
        attempt_id=attempt_id,
        event_kind="run.terminal",
        event_status=status.value,
        safe_detail={
            "outcome": outcome,
            "status": status.value,
            "output_verdict": output_verdict.value,
            "terminal_intent_hash": intent_hash,
        },
        step_id=step_id,
    )
    publication_id = str(uuid.uuid4())
    conn.execute(
        ne_agent_run_lifecycle_outbox.insert().values(
            publication_id=publication_id,
            run_id=run_id,
            attempt_id=attempt_id,
            event_id=event_id,
            terminal_status=status.value,
            output_verdict=output_verdict.value,
            outcome=outcome,
            terminal_intent_hash=intent_hash,
            created_at=now,
            claimed_at=None,
            published_at=None,
            audit_event_id=None,
            **finalizer_values,
        )
    )
    updated = conn.execute(
        ne_agent_runs.update()
        .where(
            ne_agent_runs.c.run_id == run_id,
            ne_agent_runs.c.status == run_status.value,
        )
        .values(
            status=status.value,
            output_verdict=output_verdict.value,
            version=ne_agent_runs.c.version + 1,
            terminal_at=now,
            updated_at=now,
        )
    )
    if updated.rowcount != 1:
        raise RuntimeError("Run terminal state changed concurrently")
    return publication_id


def ensure_run_workflow_schema(engine: sa.Engine) -> None:
    """Create this module's additive runtime tables.

    Enumerated by NAME rather than left to ``metadata.create_all``: this module's
    tables register on the shared metadata only once it is IMPORTED, so a schema
    bootstrap that runs before that import would silently create every other
    table and skip these. ``ensure_run_schema`` imports ``run_checkpoint`` for
    exactly this reason and does NOT import ``run_workflow`` — so anything added
    here that is not in this tuple is import-order dependent, and the failure
    lands far away, at the first write.
    """

    for table in (
        ne_agent_run_workflow_claims,
        ne_agent_run_workflow_leases,
        ne_agent_run_execution_checkpoints,
        ne_agent_run_lifecycle_outbox,
        # POST_DEMO item 12. Omitted on the first cut, and a Codex pass caught it:
        # the span tests passed only because their harness constructs a
        # RunWorkflowStateStore, which imports this module before create_all runs.
        ne_agent_run_stage_spans,
    ):
        table.create(engine, checkfirst=True)
    ensure_run_audit_intent_schema(engine)


class RunWorkflowStateStore:
    """Synchronous, transaction-bounded lease/checkpoint store for DBOS steps."""

    def __init__(
        self,
        engine: sa.Engine,
        *,
        lease_seconds: float = 300.0,
        publication_claim_seconds: float = 300.0,
        run_store: AgentRunStore | None = None,
    ) -> None:
        if lease_seconds <= 0 or publication_claim_seconds <= 0:
            raise ValueError("Run workflow lease durations must be positive")
        self.engine = engine
        self.lease_seconds = lease_seconds
        self.publication_claim_seconds = publication_claim_seconds
        self._finalizer = None if run_store is None else RunFinalizer(run_store)

    def _settlement_advisory_key(self, run_id: str) -> int:
        digest = hashlib.sha256(f"run-settlement:{run_id}".encode()).digest()
        return int.from_bytes(digest[:8], byteorder="big", signed=True)

    def _sqlite_settlement_lock_path(self, run_id: str) -> str:
        database = self.engine.url.database
        if not database or database == ":memory:":
            # An in-memory SQLite database cannot be observed by another process;
            # a process/engine-specific OS lock is therefore the complete seam.
            database = os.path.join(
                tempfile.gettempdir(),
                f"neutron-sqlite-memory-{os.getpid()}-{id(self.engine.pool)}",
            )
        run_hash = hashlib.sha256(run_id.encode()).hexdigest()
        return f"{os.path.realpath(database)}.run-settlement-{run_hash}.lock"

    def _acquire_settlement_guard(
        self, run_id: str, *, blocking: bool
    ) -> _RunSettlementGuard | None:
        dialect = self.engine.dialect.name
        if dialect == "postgresql":
            connection = self.engine.connect()
            key = self._settlement_advisory_key(run_id)
            acquired_lock = False
            try:
                function = "pg_advisory_lock" if blocking else "pg_try_advisory_lock"
                acquired = connection.execute(
                    sa.text(f"SELECT {function}(:key)"), {"key": key}
                ).scalar_one()
                if not blocking and not acquired:
                    connection.close()
                    return None
                acquired_lock = True
                return _RunSettlementGuard(connection=connection, advisory_key=key)
            except BaseException:
                if acquired_lock:
                    connection.invalidate()
                connection.close()
                raise
        if dialect == "sqlite":
            lock_fd = os.open(
                self._sqlite_settlement_lock_path(run_id),
                os.O_CREAT | os.O_RDWR,
                0o600,
            )
            flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
            try:
                fcntl.flock(lock_fd, flags)
            except BlockingIOError:
                os.close(lock_fd)
                return None
            except BaseException:
                os.close(lock_fd)
                raise
            return _RunSettlementGuard(lock_fd=lock_fd)
        raise RuntimeError(f"Run settlement fencing does not support {dialect!r}")

    def begin_effect_settlement(self, claim: RunWorkflowClaim) -> _RunSettlementGuard:
        """Fence takeover until this live worker settles every admitted consequence."""

        guard = self._acquire_settlement_guard(claim.run_id, blocking=True)
        if guard is None:  # pragma: no cover - blocking acquisition is total
            raise RuntimeError("Run workflow settlement guard was not acquired")
        try:
            with self.engine.begin() as conn:
                now = _database_now(conn)
                run_row = conn.execute(
                    sa.select(ne_agent_runs.c.status)
                    .where(ne_agent_runs.c.run_id == claim.run_id)
                    .with_for_update()
                ).one_or_none()
                updated = conn.execute(
                    ne_agent_run_workflow_leases.update()
                    .where(
                        ne_agent_run_workflow_leases.c.run_id == claim.run_id,
                        ne_agent_run_workflow_leases.c.dispatch_id == claim.dispatch_id,
                        ne_agent_run_workflow_leases.c.attempt_id == claim.attempt_id,
                        ne_agent_run_workflow_leases.c.lease_token == claim.lease_token,
                        ne_agent_run_workflow_leases.c.lease_epoch == claim.lease_epoch,
                        ne_agent_run_workflow_leases.c.state == "active",
                    )
                    .values(expires_at=_SETTLEMENT_FENCE_EXPIRES_AT, updated_at=now)
                )
                if run_row is None or updated.rowcount != 1:
                    raise RuntimeError(
                        "Run execution lease is stale or not joined to this dispatch"
                    )
            return guard
        except BaseException:
            guard.close()
            raise

    def claim(
        self,
        *,
        dispatch_id: str,
        run_id: str,
        owner_principal_id: str,
        config_hash: str,
    ) -> RunWorkflowClaim:
        if not all((dispatch_id, run_id, owner_principal_id)):
            raise ValueError("Run workflow claim identity must be complete")
        if len(config_hash) != 64:
            raise ValueError("Run workflow config_hash must be SHA-256")
        settlement_guard = self._acquire_settlement_guard(run_id, blocking=False)
        if settlement_guard is None:
            raise RuntimeError("Run workflow lease is still owned by an active worker")
        with settlement_guard, self.engine.begin() as conn:
            now = _database_now(conn)
            run = conn.execute(
                sa.select(ne_agent_runs).where(ne_agent_runs.c.run_id == run_id).with_for_update()
            ).one_or_none()
            if run is None or str(run.owner_principal_id) != owner_principal_id:
                raise LookupError("unknown Run workflow target")
            if str(run.config_hash) != config_hash:
                raise ValueError("Run workflow config hash does not match the accepted Run")

            joined = conn.execute(
                sa.select(ne_agent_run_workflow_claims).where(
                    ne_agent_run_workflow_claims.c.run_id == run_id
                )
            ).one_or_none()
            if joined is None:
                initial_status = RunState(str(run.status))
                if initial_status not in {
                    RunState.SUBMITTED,
                    RunState.QUEUED,
                    RunState.CANCELLING,
                }:
                    raise ValueError("an unclaimed Run workflow must carry a pending admission")
                attempts = list(
                    conn.execute(
                        sa.select(ne_agent_run_attempt_links).where(
                            ne_agent_run_attempt_links.c.run_id == run_id
                        )
                    ).all()
                )
                if len(attempts) != 1:
                    raise ValueError(
                        "initial Run workflow requires exactly one admission-created attempt"
                    )
                admitted_attempt = attempts[0]
                if (
                    str(admitted_attempt.conversation_id) != str(run.conversation_id)
                    or admitted_attempt.predecessor_attempt_id is not None
                    or int(admitted_attempt.attempt_number) != 1
                ):
                    raise ValueError(
                        "initial Run workflow attempt lineage differs from its admission"
                    )
                attempt_id = str(admitted_attempt.attempt_id)
                retry_lineage = conn.execute(
                    sa.select(ne_agent_run_retry_lineage).where(
                        ne_agent_run_retry_lineage.c.successor_run_id == run_id
                    )
                ).one_or_none()
                expected_action = "retry" if retry_lineage is not None else "start"
                if retry_lineage is not None and (
                    str(retry_lineage.successor_attempt_id) != attempt_id
                    or int(retry_lineage.retry_ordinal) < 1
                    or str(retry_lineage.predecessor_run_id) == run_id
                    or str(retry_lineage.root_run_id) == run_id
                ):
                    raise ValueError("retry Run workflow lineage differs from its admission")
                admission_rows = list(
                    conn.execute(
                        sa.select(ne_agent_run_enqueue_outbox).where(
                            ne_agent_run_enqueue_outbox.c.run_id == run_id,
                            ne_agent_run_enqueue_outbox.c.attempt_id == attempt_id,
                            ne_agent_run_enqueue_outbox.c.action.in_(("start", "retry")),
                        )
                    ).all()
                )
                if (
                    len(admission_rows) != 1
                    or str(admission_rows[0].action) != expected_action
                    or admission_rows[0].claimed_at is not None
                    or int(admission_rows[0].run_version) != 1
                ):
                    raise ValueError(
                        "initial Run workflow requires one pending exact admission outbox"
                    )
                cancel_rows = list(
                    conn.execute(
                        sa.select(ne_agent_run_enqueue_outbox).where(
                            ne_agent_run_enqueue_outbox.c.run_id == run_id,
                            ne_agent_run_enqueue_outbox.c.attempt_id == attempt_id,
                            ne_agent_run_enqueue_outbox.c.action == "cancel",
                        )
                    ).all()
                )
                if initial_status is RunState.CANCELLING and (
                    len(cancel_rows) != 1
                    or cancel_rows[0].claimed_at is not None
                    or int(cancel_rows[0].run_version) != int(run.version)
                ):
                    raise ValueError(
                        "cancelling admission requires one pending exact cancel outbox"
                    )
                if initial_status is not RunState.CANCELLING and cancel_rows:
                    raise ValueError("non-cancelling admission carries a cancel outbox")
                acquired_ids = [str(admission_rows[0].enqueue_id)]
                if initial_status is RunState.CANCELLING:
                    acquired_ids.append(str(cancel_rows[0].enqueue_id))
                acquired = conn.execute(
                    ne_agent_run_enqueue_outbox.update()
                    .where(
                        ne_agent_run_enqueue_outbox.c.enqueue_id.in_(acquired_ids),
                        ne_agent_run_enqueue_outbox.c.claimed_at.is_(None),
                    )
                    .values(claimed_at=now)
                )
                if acquired.rowcount != len(acquired_ids):
                    raise RuntimeError("Run admission outbox was claimed concurrently")
                conn.execute(
                    ne_agent_run_workflow_claims.insert().values(
                        dispatch_id=dispatch_id,
                        run_id=run_id,
                        owner_principal_id=owner_principal_id,
                        attempt_id=attempt_id,
                        config_hash=config_hash,
                        created_at=now,
                    )
                )
                if initial_status is RunState.SUBMITTED:
                    queued = conn.execute(
                        ne_agent_runs.update()
                        .where(
                            ne_agent_runs.c.run_id == run_id,
                            ne_agent_runs.c.status == RunState.SUBMITTED.value,
                        )
                        .values(
                            status=RunState.QUEUED.value,
                            version=ne_agent_runs.c.version + 1,
                            updated_at=now,
                        )
                    )
                    if queued.rowcount != 1:
                        raise RuntimeError("Run admission changed while joining its worker")
                if initial_status in {RunState.SUBMITTED, RunState.QUEUED}:
                    running = conn.execute(
                        ne_agent_runs.update()
                        .where(
                            ne_agent_runs.c.run_id == run_id,
                            ne_agent_runs.c.status == RunState.QUEUED.value,
                        )
                        .values(
                            status=RunState.RUNNING.value,
                            version=ne_agent_runs.c.version + 1,
                            updated_at=now,
                        )
                    )
                    if running.rowcount != 1:
                        raise RuntimeError("Run admission changed before execution claim")
                claim_event_id = _append_safe_event(
                    conn,
                    run_id=run_id,
                    attempt_id=attempt_id,
                    event_kind="run.execution.claimed",
                    event_status=(
                        "running"
                        if initial_status in {RunState.SUBMITTED, RunState.QUEUED}
                        else RunState.CANCELLING.value
                    ),
                    safe_detail={"dispatch_id": dispatch_id, "lease_epoch": 1},
                    step_id="run-execution",
                )
                epoch = 1
                recovered = False
            else:
                if RunState(str(run.status)) in {
                    RunState.COMPLETED,
                    RunState.FAILED,
                    RunState.CANCELLED,
                    RunState.EXPIRED,
                    RunState.CONTEXT_EXHAUSTED,
                    RunState.BUDGET_EXHAUSTED,
                }:
                    raise ValueError("terminal Run workflow cannot acquire another lease")
                if RunState(str(run.status)) is RunState.WAITING_APPROVAL:
                    raise RuntimeError("waiting approval Run cannot replace its execution lease")
                if (
                    str(joined.dispatch_id) != dispatch_id
                    or str(joined.owner_principal_id) != owner_principal_id
                    or str(joined.config_hash) != config_hash
                ):
                    raise ValueError("Run workflow retry does not match its durable dispatch join")
                attempt_id = str(joined.attempt_id)
                if RunState(str(run.status)) is RunState.CANCELLING:
                    _require_exact_cancel_outbox(
                        conn,
                        run_id=run_id,
                        attempt_id=attempt_id,
                        run_version=int(run.version),
                    )
                prior_lease = conn.execute(
                    sa.select(ne_agent_run_workflow_leases).where(
                        ne_agent_run_workflow_leases.c.run_id == run_id
                    )
                ).one()
                prior_expiry = _utc(prior_lease.expires_at)
                if (
                    str(prior_lease.state) == "active"
                    and prior_expiry > now
                    and prior_expiry.year != _SETTLEMENT_FENCE_EXPIRES_AT.year
                ):
                    raise RuntimeError("Run workflow lease is still owned by an active worker")
                epoch = int(prior_lease.lease_epoch) + 1
                stages = set(
                    conn.execute(
                        sa.select(ne_agent_run_execution_checkpoints.c.stage).where(
                            ne_agent_run_execution_checkpoints.c.run_id == run_id,
                            ne_agent_run_execution_checkpoints.c.attempt_id == attempt_id,
                        )
                    ).scalars()
                )
                recovered = "execution_started" in stages and "execution_succeeded" not in stages
                claim_event_id = _append_safe_event(
                    conn,
                    run_id=run_id,
                    attempt_id=attempt_id,
                    event_kind="run.execution.recovered",
                    event_status="uncertain" if recovered else "retry",
                    safe_detail={"dispatch_id": dispatch_id, "lease_epoch": epoch},
                    step_id="run-execution",
                )

            token = str(uuid.uuid4())
            expires = now + timedelta(seconds=self.lease_seconds)
            lease = conn.execute(
                sa.select(ne_agent_run_workflow_leases.c.run_id).where(
                    ne_agent_run_workflow_leases.c.run_id == run_id
                )
            ).first()
            if lease is None:
                conn.execute(
                    ne_agent_run_workflow_leases.insert().values(
                        run_id=run_id,
                        dispatch_id=dispatch_id,
                        attempt_id=attempt_id,
                        lease_token=token,
                        lease_epoch=epoch,
                        state="active",
                        expires_at=expires,
                        updated_at=now,
                    )
                )
            else:
                conn.execute(
                    ne_agent_run_workflow_leases.update()
                    .where(ne_agent_run_workflow_leases.c.run_id == run_id)
                    .values(
                        lease_token=token,
                        lease_epoch=epoch,
                        state="active",
                        expires_at=expires,
                        updated_at=now,
                    )
                )
        return RunWorkflowClaim(
            dispatch_id=dispatch_id,
            run_id=run_id,
            owner_principal_id=owner_principal_id,
            attempt_id=attempt_id,
            lease_token=token,
            lease_epoch=epoch,
            recovered=recovered,
            claim_event_id=claim_event_id,
        )

    def terminal_publication(
        self,
        *,
        dispatch_id: str,
        run_id: str,
        owner_principal_id: str,
        config_hash: str,
    ) -> RunTerminalPublication | None:
        """Load an exact durable terminal result without changing publication state."""

        with self.engine.connect() as conn:
            joined = conn.execute(
                _terminal_publication_select()
                .add_columns(
                    ne_agent_run_workflow_claims.c.dispatch_id,
                    ne_agent_run_workflow_claims.c.config_hash,
                )
                .join(
                    ne_agent_run_workflow_claims,
                    sa.and_(
                        ne_agent_run_lifecycle_outbox.c.run_id
                        == ne_agent_run_workflow_claims.c.run_id,
                        ne_agent_run_lifecycle_outbox.c.attempt_id
                        == ne_agent_run_workflow_claims.c.attempt_id,
                    ),
                )
                .where(ne_agent_run_workflow_claims.c.run_id == run_id)
            ).one_or_none()
        if joined is None:
            return None
        if (
            str(joined.dispatch_id) != dispatch_id
            or str(joined.owner_principal_id) != owner_principal_id
            or str(joined.config_hash) != config_hash
        ):
            raise ValueError("terminal Run replay differs from its durable dispatch join")
        return _terminal_publication(joined)

    def get_terminal_publication(self, publication_id: str) -> RunTerminalPublication:
        """Load exactly one server-issued terminal publication identity."""

        with self.engine.connect() as conn:
            row = conn.execute(
                _terminal_publication_select().where(
                    ne_agent_run_lifecycle_outbox.c.publication_id == publication_id
                )
            ).one_or_none()
        if row is None:
            raise LookupError("Run terminal publication is unavailable")
        return _terminal_publication(row)

    def claim_terminal_publication(self, publication_id: str) -> RunTerminalPublication:
        """Acquire or take over the exact pending audit-publication intent."""

        with self.engine.begin() as conn:
            now = _database_now(conn)
            stale_before = now - timedelta(seconds=self.publication_claim_seconds)
            row = conn.execute(
                _terminal_publication_select()
                .where(ne_agent_run_lifecycle_outbox.c.publication_id == publication_id)
                .with_for_update()
            ).one_or_none()
            if row is None:
                raise LookupError("Run terminal publication is unavailable")
            publication = _terminal_publication(row)
            if publication.published_at is not None:
                return publication
            if publication.claimed_at is not None and publication.claimed_at > stale_before:
                raise RuntimeError("Run terminal publication is owned by an active publisher")
            condition = (
                ne_agent_run_lifecycle_outbox.c.claimed_at.is_(None)
                if publication.claimed_at is None
                else ne_agent_run_lifecycle_outbox.c.claimed_at == publication.claimed_at
            )
            claimed = conn.execute(
                ne_agent_run_lifecycle_outbox.update()
                .where(
                    ne_agent_run_lifecycle_outbox.c.publication_id == publication_id,
                    ne_agent_run_lifecycle_outbox.c.published_at.is_(None),
                    condition,
                )
                .values(claimed_at=now)
            )
            if claimed.rowcount != 1:
                raise RuntimeError("Run terminal publication changed during claim")
            updated = conn.execute(
                _terminal_publication_select().where(
                    ne_agent_run_lifecycle_outbox.c.publication_id == publication_id
                )
            ).one()
        return _terminal_publication(updated)

    def terminal_audit_event_id(
        self, publication: RunTerminalPublication, *, config_hash: str
    ) -> str | None:
        """Return only the exact terminal receipt written for this publication target."""

        receipt = self.terminal_audit_receipt(publication, config_hash=config_hash)
        return None if receipt is None else str(receipt["audit_event_id"])

    def terminal_audit_receipt(
        self, publication: RunTerminalPublication, *, config_hash: str
    ) -> dict[str, Any] | None:
        """Load the full exact terminal receipt needed to validate chain evidence."""

        with self.engine.connect() as conn:
            rows = list(
                conn.execute(
                    sa.select(ne_agent_run_audit_receipts).where(
                        ne_agent_run_audit_receipts.c.run_id == publication.run_id,
                        ne_agent_run_audit_receipts.c.attempt_id == publication.attempt_id,
                        ne_agent_run_audit_receipts.c.step_id == "run-terminal-publication",
                        ne_agent_run_audit_receipts.c.decision_kind
                        == RunAuditDecisionKind.TERMINAL.value,
                        ne_agent_run_audit_receipts.c.config_hash == config_hash,
                    )
                ).mappings()
            )
        if len(rows) > 1:
            raise RuntimeError("Run terminal publication has multiple audit receipts")
        return None if not rows else dict(rows[0])

    def complete_terminal_publication(
        self,
        publication: RunTerminalPublication,
        *,
        audit_event_id: str,
        config_hash: str,
    ) -> None:
        """Fence and mark a claimed publication only after its exact receipt exists."""

        if publication.claimed_at is None or not audit_event_id:
            raise ValueError("Run terminal publication completion lacks its claim or audit event")
        with self.engine.begin() as conn:
            now = _database_now(conn)
            receipt = conn.execute(
                sa.select(ne_agent_run_audit_receipts.c.audit_event_id).where(
                    ne_agent_run_audit_receipts.c.run_id == publication.run_id,
                    ne_agent_run_audit_receipts.c.attempt_id == publication.attempt_id,
                    ne_agent_run_audit_receipts.c.step_id == "run-terminal-publication",
                    ne_agent_run_audit_receipts.c.decision_kind
                    == RunAuditDecisionKind.TERMINAL.value,
                    ne_agent_run_audit_receipts.c.audit_event_id == audit_event_id,
                    ne_agent_run_audit_receipts.c.config_hash == config_hash,
                )
            ).one_or_none()
            if receipt is None:
                raise RuntimeError("Run terminal publication lacks its exact audit receipt")
            updated = conn.execute(
                ne_agent_run_lifecycle_outbox.update()
                .where(
                    ne_agent_run_lifecycle_outbox.c.publication_id == publication.publication_id,
                    ne_agent_run_lifecycle_outbox.c.claimed_at == publication.claimed_at,
                    ne_agent_run_lifecycle_outbox.c.published_at.is_(None),
                )
                .values(published_at=now, audit_event_id=audit_event_id)
            )
            if updated.rowcount != 1:
                raise RuntimeError("Run terminal publication claim is stale")

    def release_terminal_publication(self, publication: RunTerminalPublication) -> None:
        """Release only this publisher's still-pending fenced claim after failure."""

        if publication.claimed_at is None or publication.published_at is not None:
            return
        with self.engine.begin() as conn:
            conn.execute(
                ne_agent_run_lifecycle_outbox.update()
                .where(
                    ne_agent_run_lifecycle_outbox.c.publication_id == publication.publication_id,
                    ne_agent_run_lifecycle_outbox.c.claimed_at == publication.claimed_at,
                    ne_agent_run_lifecycle_outbox.c.published_at.is_(None),
                )
                .values(claimed_at=None)
            )

    def checkpoint(
        self,
        claim: RunWorkflowClaim,
        *,
        stage: str,
        safe_detail: Mapping[str, object],
        required_run_status: RunState | None = None,
    ) -> RunExecutionCheckpoint:
        if not stage or any(ch.isspace() for ch in stage):
            raise ValueError("Run execution checkpoint stage must be a non-empty token")
        forbidden = {"prompt", "output", "content", "arguments", "claim_text", "source_title"}
        if forbidden.intersection(safe_detail):
            raise ValueError("Run execution checkpoint cannot store protected content")
        serialized = _canonical(dict(safe_detail))
        intent_hash = _digest({"stage": stage, "safe_detail": dict(safe_detail)})
        with self.engine.begin() as conn:
            now = _database_now(conn)
            run_row = conn.execute(
                sa.select(ne_agent_runs.c.status, ne_agent_runs.c.version)
                .where(ne_agent_runs.c.run_id == claim.run_id)
                .with_for_update()
            ).one_or_none()
            if run_row is None or RunState(str(run_row.status)) in {
                RunState.COMPLETED,
                RunState.FAILED,
                RunState.CANCELLED,
                RunState.EXPIRED,
                RunState.CONTEXT_EXHAUSTED,
                RunState.BUDGET_EXHAUSTED,
            }:
                raise RuntimeError("Run checkpoint target is unavailable or terminal")
            locked_run_status = RunState(str(run_row.status))
            if locked_run_status is RunState.CANCELLING:
                _require_exact_cancel_outbox(
                    conn,
                    run_id=claim.run_id,
                    attempt_id=claim.attempt_id,
                    run_version=int(run_row.version),
                )
            if required_run_status is not None and locked_run_status is not required_run_status:
                raise RuntimeError("Run checkpoint lost its required lifecycle state")
            lease = conn.execute(
                sa.select(ne_agent_run_workflow_leases)
                .where(ne_agent_run_workflow_leases.c.run_id == claim.run_id)
                .with_for_update()
            ).one_or_none()
            if (
                lease is None
                or str(lease.dispatch_id) != claim.dispatch_id
                or str(lease.attempt_id) != claim.attempt_id
                or str(lease.lease_token) != claim.lease_token
                or int(lease.lease_epoch) != claim.lease_epoch
                or str(lease.state) != "active"
                or _utc(lease.expires_at) <= now
            ):
                raise RuntimeError("Run execution lease is stale or not joined to this dispatch")
            existing = conn.execute(
                sa.select(ne_agent_run_execution_checkpoints).where(
                    ne_agent_run_execution_checkpoints.c.run_id == claim.run_id,
                    ne_agent_run_execution_checkpoints.c.attempt_id == claim.attempt_id,
                    ne_agent_run_execution_checkpoints.c.stage == stage,
                )
            ).one_or_none()
            if existing is not None:
                if str(existing.intent_hash) != intent_hash:
                    raise ValueError("Run checkpoint was replayed under a different intent")
                return _checkpoint(existing)
            checkpoint_id = str(uuid.uuid4())
            conn.execute(
                ne_agent_run_execution_checkpoints.insert().values(
                    checkpoint_id=checkpoint_id,
                    run_id=claim.run_id,
                    attempt_id=claim.attempt_id,
                    stage=stage,
                    intent_hash=intent_hash,
                    safe_detail=serialized,
                    created_at=now,
                )
            )
            row = conn.execute(
                sa.select(ne_agent_run_execution_checkpoints).where(
                    ne_agent_run_execution_checkpoints.c.checkpoint_id == checkpoint_id
                )
            ).one()
        return _checkpoint(row)

    def get_checkpoint(
        self, *, run_id: str, attempt_id: str, stage: str
    ) -> RunExecutionCheckpoint | None:
        with self.engine.connect() as conn:
            row = conn.execute(
                sa.select(ne_agent_run_execution_checkpoints).where(
                    ne_agent_run_execution_checkpoints.c.run_id == run_id,
                    ne_agent_run_execution_checkpoints.c.attempt_id == attempt_id,
                    ne_agent_run_execution_checkpoints.c.stage == stage,
                )
            ).one_or_none()
        return None if row is None else _checkpoint(row)

    def has_issued_effect(self, claim: RunWorkflowClaim) -> bool:
        """Return whether this exact attempt durably crossed an effect issue edge."""

        with self.engine.connect() as conn:
            return (
                conn.execute(
                    sa.select(sa.func.count())
                    .select_from(ne_agent_run_execution_checkpoints)
                    .where(
                        ne_agent_run_execution_checkpoints.c.run_id == claim.run_id,
                        ne_agent_run_execution_checkpoints.c.attempt_id == claim.attempt_id,
                        ne_agent_run_execution_checkpoints.c.stage.like("effect_issued:%"),
                    )
                ).scalar_one()
                > 0
            )

    def has_resumed_approval(self, claim: RunWorkflowClaim) -> bool:
        """Return true only for this attempt's exact approved/resumed checkpoint join."""

        return self.resumed_approval_id(claim) is not None

    def resumed_approval_id(self, claim: RunWorkflowClaim) -> str | None:
        """The approval id of this attempt's exact approved/resumed continuation.

        Exactly one, or ``None``. This is the address of the ONE effect an
        approval resume is allowed to execute: it names the continuation
        checkpoint that pinned the withheld ``tool_call_id`` and the resume
        intent that recorded the human's approved decision.
        """

        from ne.runtime.run_checkpoint import (
            ne_agent_run_approval_joins,
            ne_agent_run_approval_resume_intents,
            ne_agent_run_continuation_checkpoints,
        )

        with self.engine.connect() as conn:
            rows = conn.execute(
                sa.select(ne_agent_run_approval_joins.c.approval_id)
                .select_from(
                    ne_agent_run_approval_joins.join(
                        ne_agent_run_continuation_checkpoints,
                        ne_agent_run_continuation_checkpoints.c.checkpoint_id
                        == ne_agent_run_approval_joins.c.checkpoint_id,
                    ).join(
                        ne_agent_run_approval_resume_intents,
                        ne_agent_run_approval_resume_intents.c.approval_id
                        == ne_agent_run_approval_joins.c.approval_id,
                    )
                )
                .where(
                    ne_agent_run_approval_joins.c.run_id == claim.run_id,
                    ne_agent_run_approval_joins.c.attempt_id == claim.attempt_id,
                    ne_agent_run_continuation_checkpoints.c.status == "resumed",
                    ne_agent_run_approval_resume_intents.c.decision == "approved",
                )
            ).all()
        if len(rows) > 1:
            # Structurally unreachable (`run_id` is UNIQUE on the join table),
            # but if it ever happens it means TWO approvals each authorize "one"
            # effect for the same attempt — refusing loudly beats silently
            # pretending no continuation exists and re-entering the agent loop.
            raise RuntimeError("Run attempt carries more than one resumed approval continuation")
        return None if not rows else str(rows[0].approval_id)

    def release(self, claim: RunWorkflowClaim) -> None:
        with self.engine.begin() as conn:
            now = _database_now(conn)
            result = conn.execute(
                ne_agent_run_workflow_leases.update()
                .where(
                    ne_agent_run_workflow_leases.c.run_id == claim.run_id,
                    ne_agent_run_workflow_leases.c.dispatch_id == claim.dispatch_id,
                    ne_agent_run_workflow_leases.c.attempt_id == claim.attempt_id,
                    ne_agent_run_workflow_leases.c.lease_token == claim.lease_token,
                    ne_agent_run_workflow_leases.c.lease_epoch == claim.lease_epoch,
                    ne_agent_run_workflow_leases.c.state == "active",
                )
                .values(state="released", updated_at=now)
            )
            if result.rowcount != 1:
                raise RuntimeError("Run workflow cannot release a stale lease")

    def probe(
        self, claim: RunWorkflowClaim, *, renew: bool = False, stage: str | None = None
    ) -> RunLeaseProbe:
        """Fence a step boundary and optionally renew the exact active lease.

        Expiry makes a lease eligible for takeover; it does not itself rotate
        the fenced identity.  An exact holder may therefore close a renewal
        lapse caused by scheduler delay until a takeover changes its token and
        epoch.  A read-only probe remains strict about the current deadline.
        """

        with self.engine.begin() as conn:
            now = _database_now(conn)
            run_row = conn.execute(
                sa.select(ne_agent_runs.c.status, ne_agent_runs.c.version)
                .where(ne_agent_runs.c.run_id == claim.run_id)
                .with_for_update()
            ).one_or_none()
            lease_identity = (
                ne_agent_run_workflow_leases.c.run_id == claim.run_id,
                ne_agent_run_workflow_leases.c.dispatch_id == claim.dispatch_id,
                ne_agent_run_workflow_leases.c.attempt_id == claim.attempt_id,
                ne_agent_run_workflow_leases.c.lease_token == claim.lease_token,
                ne_agent_run_workflow_leases.c.lease_epoch == claim.lease_epoch,
                ne_agent_run_workflow_leases.c.state == "active",
            )
            lease_fence = (
                lease_identity
                if renew
                else (
                    *lease_identity,
                    ne_agent_run_workflow_leases.c.expires_at > now,
                )
            )
            row = conn.execute(
                sa.select(ne_agent_run_workflow_leases.c.expires_at)
                .where(*lease_fence)
                .with_for_update()
            ).one_or_none()
            if run_row is None or row is None:
                raise RuntimeError("Run execution lease is stale or not joined to this dispatch")
            run_status = RunState(str(run_row.status))
            if run_status is RunState.CANCELLING:
                _require_exact_cancel_outbox(
                    conn,
                    run_id=claim.run_id,
                    attempt_id=claim.attempt_id,
                    run_version=int(run_row.version),
                )
            expires_at = _utc(row.expires_at)
            if renew:
                expires_at = (
                    _SETTLEMENT_FENCE_EXPIRES_AT
                    if expires_at.year == _SETTLEMENT_FENCE_EXPIRES_AT.year
                    else now + timedelta(seconds=self.lease_seconds)
                )
                updated = conn.execute(
                    ne_agent_run_workflow_leases.update()
                    .where(*lease_identity)
                    .values(expires_at=expires_at, updated_at=now)
                )
                if updated.rowcount != 1:
                    raise RuntimeError("Run execution lease became stale during renewal")
                if stage is not None and run_status not in _NON_ADMITTING_RUN_STATES:
                    # POST_DEMO item 12. Nested under `renew` DELIBERATELY: the
                    # comment below is only true of a renewal, and a read-only
                    # `probe(stage=...)` would otherwise write a span under a
                    # different fence than the one described. The invariant is now
                    # the code's, not a convention the next caller has to know.
                    #
                    # Written inside the renewal's own transaction and stamped with
                    # `now`, the database clock this transaction already took, so
                    # spans order by ONE clock rather than by whichever worker
                    # observed them. It commits with the renewal or not at all —
                    # no best-effort suppression.
                    #
                    # THE COUPLING RUNS BOTH WAYS, and round 12 was right that only
                    # one direction was written down. "No span without a renewal" is
                    # the direction above and it is the one that was argued. The
                    # converse holds in a NARROWER form that must be stated
                    # exactly, because a round-13 version of this comment said
                    # "NO RENEWAL WITHOUT A SPAN" and that is FALSE: a renewal
                    # with no stage writes no span, and those are the majority --
                    # the ~1 s poll loop passes no stage at all, and a run in
                    # `_NON_ADMITTING_RUN_STATES` is skipped by the guard above.
                    # What is true: A STAGED RENEWAL FAILS IF ITS SPAN INSERT
                    # FAILS. This INSERT is the last statement in a
                    # transaction already holding FOR UPDATE on the run and its
                    # lease, so if it fails the renewal fails with it -- and it
                    # fails as a DBAPI error, which `heartbeat` does not translate,
                    # so it does not even present as a lost lease.
                    #
                    # That makes an instrumentation table a liveness dependency of
                    # every run, which it was not before this increment. The table
                    # is append-only with a random-uuid PK, has no reaper and no
                    # retention path anywhere in this tree, and its only index is
                    # (run_id, attempt_id). Those two facts -- nothing prunes it,
                    # and it gates lease renewal -- are load-bearing TOGETHER, and
                    # were recorded apart. The trade is deliberate (a best-effort
                    # write would let the table disagree with the fence it claims to
                    # measure), but a reaper is owed before this table has a
                    # production lifetime measured in months.
                    #
                    # WHAT A SPAN DOES AND DOES NOT PROVE, stated because an earlier
                    # draft of this comment overclaimed and an adversarial pass
                    # caught it. It proves the boundary was crossed and the lease
                    # renewal succeeded. It does NOT prove the lease was unexpired:
                    # `renew=True` deliberately drops the `expires_at > now` fence
                    # (see this method's docstring) so an exact holder can close a
                    # renewal lapse, and such a boundary writes a span. A reader
                    # decomposing wall-clock from this table is reading when stages
                    # were REACHED, not a certificate that each was still entitled
                    # to run.
                    #
                    # It does refuse the one case it can see: a run already terminal
                    # or cancelling is rejected below, outside this transaction, so
                    # without this guard the span would commit and the boundary
                    # would then raise — recording a stage the platform refused.
                    conn.execute(
                        ne_agent_run_stage_spans.insert().values(
                            span_id=str(uuid.uuid4()),
                            run_id=claim.run_id,
                            attempt_id=claim.attempt_id,
                            stage=_validated_stage(stage),
                            observed_at=now,
                        )
                    )
        if run_status in {
            RunState.COMPLETED,
            RunState.FAILED,
            RunState.CANCELLED,
            RunState.EXPIRED,
            RunState.CONTEXT_EXHAUSTED,
            RunState.BUDGET_EXHAUSTED,
        }:
            raise RuntimeError("Run execution lease targets an already-terminal Run")
        return RunLeaseProbe(
            expires_at=expires_at,
            run_status=run_status,
            cancellation_requested=run_status is RunState.CANCELLING,
        )

    def renew(self, claim: RunWorkflowClaim, *, stage: str | None = None) -> RunLeaseProbe:
        return self.probe(claim, renew=True, stage=stage)

    def cancellation_requested(self, claim: RunWorkflowClaim) -> bool:
        """Read cancellation only through the still-current fenced lease."""
        return self.probe(claim).cancellation_requested

    def reconcile_approval_join(
        self,
        *,
        approval_id: str,
        action_hash: str,
        principal_id: str,
        policy_hash: str,
    ) -> RunTerminalPublication | None:
        """Reconcile an exact approval through the sole lease-fenced finalizer."""

        from ne.runtime.approval_store import ne_approval_requests
        from ne.runtime.run_checkpoint import (
            ne_agent_run_approval_joins,
            ne_agent_run_continuation_checkpoints,
        )

        with self.engine.begin() as conn:
            now = _database_now(conn)
            joined = conn.execute(
                sa.select(ne_agent_run_approval_joins).where(
                    ne_agent_run_approval_joins.c.approval_id == approval_id
                )
            ).one_or_none()
            if joined is None or not (
                hmac.compare_digest(str(joined.action_hash), action_hash)
                and hmac.compare_digest(str(joined.principal_id), principal_id)
                and hmac.compare_digest(str(joined.policy_hash), policy_hash)
            ):
                raise LookupError("Run approval lifecycle join unavailable")
            # The Run is the lifecycle linearization point. Approval/checkpoint
            # rows are immutable joins or are locked only after the Run, avoiding
            # the approval->Run reverse-lock order used by the retired writer.
            run_row = conn.execute(
                sa.select(ne_agent_runs)
                .where(ne_agent_runs.c.run_id == joined.run_id)
                .with_for_update()
            ).one()
            run_status = RunState(str(run_row.status))
            approval_status = str(
                conn.execute(
                    sa.select(ne_approval_requests.c.status)
                    .where(ne_approval_requests.c.request_id == joined.approval_id)
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
            # The existing durable approval-expiry workflow owns the request CAS.
            # Reconcile that committed decision into the Run checkpoint here, on
            # the exact immutable join, before choosing the primary Run winner.
            if approval_status == "expired" and checkpoint_status == "pending":
                reconciled = conn.execute(
                    ne_agent_run_continuation_checkpoints.update()
                    .where(
                        ne_agent_run_continuation_checkpoints.c.checkpoint_id
                        == joined.checkpoint_id,
                        ne_agent_run_continuation_checkpoints.c.status == "pending",
                    )
                    .values(status="expired", updated_at=now)
                )
                if reconciled.rowcount != 1:
                    raise RuntimeError("approval expiry lost its exact Run checkpoint CAS")
                checkpoint_status = "expired"
            exact_pairs = {
                ("pending", "pending"),
                ("pending_second", "pending"),
                ("approved", "approved"),
                ("approved", "resumed"),
                # `consumed`: the resumed continuation's ONE permitted execution
                # crossed its durable fence — the workflow (not this
                # reconciler) owns the terminal from here.
                ("approved", "consumed"),
                ("rejected", "rejected"),
                ("expired", "expired"),
                ("source_withheld", "source_withheld"),
            }
            if (approval_status, checkpoint_status) not in exact_pairs:
                raise RuntimeError("approval and Run checkpoint lifecycle states diverged")
            target: RunState | None = None
            outcome = ""
            if run_status is RunState.CANCELLING:
                target, outcome = RunState.CANCELLED, "cancelled"
            elif run_status is RunState.WAITING_APPROVAL and checkpoint_status == "expired":
                target, outcome = RunState.EXPIRED, "expired"
            elif run_status is RunState.WAITING_APPROVAL and checkpoint_status == "rejected":
                target, outcome = RunState.COMPLETED, "refused"
            elif (
                run_status is RunState.WAITING_APPROVAL
                and (
                    approval_status,
                    checkpoint_status,
                )
                in {
                    ("pending", "pending"),
                    ("pending_second", "pending"),
                    ("approved", "approved"),
                }
            ) or (
                run_status is RunState.RUNNING
                and (
                    approval_status,
                    checkpoint_status,
                )
                in {("approved", "resumed"), ("approved", "consumed")}
            ):
                return None
            elif run_status in {
                RunState.COMPLETED,
                RunState.FAILED,
                RunState.CANCELLED,
                RunState.EXPIRED,
                RunState.CONTEXT_EXHAUSTED,
                RunState.BUDGET_EXHAUSTED,
            }:
                existing = conn.execute(
                    _terminal_publication_select().where(
                        ne_agent_run_lifecycle_outbox.c.run_id == joined.run_id,
                        ne_agent_run_lifecycle_outbox.c.attempt_id == joined.attempt_id,
                    )
                ).one_or_none()
                if existing is None:
                    raise RuntimeError("terminal approval Run has no atomic lifecycle publication")
                return _terminal_publication(existing)
            else:
                raise RuntimeError("approval join does not match the primary Run lifecycle")
            assert target is not None
            if self._finalizer is None:
                raise RuntimeError("Run approval reconciliation has no atomic finalizer binding")
            finalizer_target, finalizer_lease = self._approval_finalizer_context(
                conn, joined=joined, now=now
            )
            receipt = self._finalizer.finalize_unavailable_in_transaction(
                conn,
                RunUnavailableTerminalCandidate(
                    target=finalizer_target,
                    status=target.value,  # type: ignore[arg-type]
                    outcome=outcome,  # type: ignore[arg-type]
                    failure_class={
                        "cancelled": "RunCancellationRequested",
                        "expired": "ApprovalExpired",
                        "refused": "ApprovalRefused",
                    }[outcome],
                ),
                lease=finalizer_lease,
            )
            published = conn.execute(
                _terminal_publication_select().where(
                    ne_agent_run_lifecycle_outbox.c.publication_id == receipt.publication_id
                )
            ).one()
            return _terminal_publication(published)

    def _approval_finalizer_context(
        self,
        conn: sa.Connection,
        *,
        joined: Any,
        now: datetime,
    ) -> tuple[RunTurnTarget, RunFinalizerLease]:
        """Load the server target and claim-or-renew the exact joined lease."""

        target_row = conn.execute(
            sa.select(ne_agent_run_turn_targets).where(
                ne_agent_run_turn_targets.c.run_id == joined.run_id,
                ne_agent_run_turn_targets.c.attempt_id == joined.attempt_id,
            )
        ).one_or_none()
        if target_row is None:
            raise RuntimeError("Run approval reconciliation has no server-authored turn target")
        target = RunTurnTarget(
            conversation_id=str(target_row.conversation_id),
            run_id=str(target_row.run_id),
            turn_id=str(target_row.turn_id),
            turn_sequence=int(target_row.turn_sequence),
            attachment_id=str(target_row.attachment_id),
            attempt_id=str(target_row.attempt_id),
        )
        workflow_join = conn.execute(
            sa.select(ne_agent_run_workflow_claims).where(
                ne_agent_run_workflow_claims.c.run_id == joined.run_id,
                ne_agent_run_workflow_claims.c.attempt_id == joined.attempt_id,
            )
        ).one_or_none()
        lease = conn.execute(
            sa.select(ne_agent_run_workflow_leases)
            .where(
                ne_agent_run_workflow_leases.c.run_id == joined.run_id,
                ne_agent_run_workflow_leases.c.attempt_id == joined.attempt_id,
            )
            .with_for_update()
        ).one_or_none()
        if workflow_join is None or lease is None:
            raise RuntimeError("Run approval reconciliation has no exact workflow lease join")
        expires_at = _utc(lease.expires_at)
        token = str(lease.lease_token)
        epoch = int(lease.lease_epoch)
        if str(lease.state) != "active" or expires_at <= now:
            token = str(uuid.uuid4())
            epoch += 1
            changed = conn.execute(
                ne_agent_run_workflow_leases.update()
                .where(
                    ne_agent_run_workflow_leases.c.run_id == joined.run_id,
                    ne_agent_run_workflow_leases.c.dispatch_id == workflow_join.dispatch_id,
                    ne_agent_run_workflow_leases.c.attempt_id == joined.attempt_id,
                    ne_agent_run_workflow_leases.c.lease_token == lease.lease_token,
                    ne_agent_run_workflow_leases.c.lease_epoch == lease.lease_epoch,
                    ne_agent_run_workflow_leases.c.state == lease.state,
                )
                .values(
                    lease_token=token,
                    lease_epoch=epoch,
                    state="active",
                    expires_at=now + timedelta(seconds=self.lease_seconds),
                    updated_at=now,
                )
            )
            if changed.rowcount != 1:
                raise RuntimeError("Run approval finalizer lost its exact lease takeover CAS")
        return target, RunFinalizerLease(
            dispatch_id=str(workflow_join.dispatch_id),
            run_id=str(joined.run_id),
            attempt_id=str(joined.attempt_id),
            lease_token=token,
            lease_epoch=epoch,
        )

    def approval_resume_fence(self, *, run_id: str, attempt_id: str) -> RunState:
        """Require the exact joined attempt to be waiting or freshly resumed."""

        with self.engine.connect() as conn:
            row = conn.execute(
                sa.select(ne_agent_runs.c.status)
                .join(
                    ne_agent_run_attempt_links,
                    sa.and_(
                        ne_agent_run_attempt_links.c.run_id == ne_agent_runs.c.run_id,
                        ne_agent_run_attempt_links.c.attempt_id == attempt_id,
                    ),
                )
                .where(ne_agent_runs.c.run_id == run_id)
            ).one_or_none()
        if row is None:
            raise LookupError("Run approval attempt join unavailable")
        status = RunState(str(row.status))
        if status not in {RunState.WAITING_APPROVAL, RunState.RUNNING}:
            raise RuntimeError("Run approval continuation is not schedulable")
        return status

    def terminalize(
        self,
        claim: RunWorkflowClaim,
        *,
        status: RunState,
        outcome: str,
        output_verdict: RunOutputVerdict = RunOutputVerdict.UNAVAILABLE,
    ) -> str:
        """Atomically publish one terminal primary state/event/outbox intent."""
        with self.engine.begin() as conn:
            return self.terminalize_in_transaction(
                conn,
                claim,
                status=status,
                outcome=outcome,
                output_verdict=output_verdict,
            )

    def terminalize_in_transaction(
        self,
        conn: sa.Connection,
        claim: RunWorkflowClaim,
        *,
        status: RunState,
        outcome: str,
        output_verdict: RunOutputVerdict = RunOutputVerdict.UNAVAILABLE,
    ) -> str:
        """Compose terminal publication inside an owning exact-lease transaction."""

        now = _database_now(conn)
        run_row = conn.execute(
            sa.select(ne_agent_runs.c.status, ne_agent_runs.c.version)
            .where(ne_agent_runs.c.run_id == claim.run_id)
            .with_for_update()
        ).one()
        lease = conn.execute(
            sa.select(ne_agent_run_workflow_leases)
            .where(ne_agent_run_workflow_leases.c.run_id == claim.run_id)
            .with_for_update()
        ).one_or_none()
        if (
            lease is None
            or str(lease.dispatch_id) != claim.dispatch_id
            or str(lease.attempt_id) != claim.attempt_id
            or str(lease.lease_token) != claim.lease_token
            or int(lease.lease_epoch) != claim.lease_epoch
            or str(lease.state) != "active"
            or _utc(lease.expires_at) <= now
        ):
            raise RuntimeError("Run lifecycle terminal writer lost its lease")
        if RunState(str(run_row.status)) is RunState.CANCELLING:
            _require_exact_cancel_outbox(
                conn,
                run_id=claim.run_id,
                attempt_id=claim.attempt_id,
                run_version=int(run_row.version),
            )
        return commit_run_terminal_in_transaction(
            conn,
            run_id=claim.run_id,
            attempt_id=claim.attempt_id,
            status=status,
            outcome=outcome,
            output_verdict=output_verdict,
            allowed_from={
                RunState.RUNNING: frozenset(
                    {
                        RunState.COMPLETED,
                        RunState.FAILED,
                        RunState.EXPIRED,
                        RunState.CONTEXT_EXHAUSTED,
                        RunState.BUDGET_EXHAUSTED,
                    }
                ),
                RunState.WAITING_APPROVAL: frozenset(
                    {
                        RunState.COMPLETED,
                        RunState.FAILED,
                        RunState.EXPIRED,
                        RunState.CONTEXT_EXHAUSTED,
                        RunState.BUDGET_EXHAUSTED,
                    }
                ),
                RunState.CANCELLING: frozenset({RunState.CANCELLED, RunState.FAILED}),
            },
            step_id="run-execution",
            now=now,
        )


class _LeaseExecutionBoundary(RunExecutionBoundary):
    """Async step fence that keeps one exact lease alive while an effect settles."""

    def __init__(self, state: RunWorkflowStateStore, claim: RunWorkflowClaim) -> None:
        self._state = state
        self._claim = claim
        self._poll_seconds = min(max(state.lease_seconds / 3.0, 0.01), 1.0)
        #: The tasks whose cancellation `await_inflight` swallowed, held so
        #: `_redeliver_cancellation` can ask whether it is still outstanding.
        self._cancelled_callers: set[asyncio.Task[Any]] = set()
        self._settlement_guard: _RunSettlementGuard | None = None

    def close(self) -> None:
        guard, self._settlement_guard = self._settlement_guard, None
        if guard is not None:
            guard.close()

    def open(self) -> None:
        if self._settlement_guard is None:
            self._settlement_guard = self._state.begin_effect_settlement(self._claim)

    def _redeliver_cancellation(self) -> bool:
        """Is a cancellation this boundary swallowed still OWED to somebody?

        ``await_inflight`` deliberately swallows a ``CancelledError`` so an
        already-admitted model/Tool/audit effect is never detached, and then
        re-delivers it — immediately from its own frame, and again from every
        later boundary — "so durable recovery observes one clear edge". That
        re-delivery must reach the adapter task too: an operator who pressed
        Cancel while the workflow task sat in the outer ``await_inflight`` must
        not have the agent loop issue a SECOND governed effect underneath them,
        which is why the sticky half is shared across the run's whole task
        nesting rather than being per-task.

        What the sticky half never asked is whether the cancellation it is
        re-delivering is still OUTSTANDING.

        ⛔ THE STRANDING (POST_DEMO item 3, two runs on 2026-09-10). The run's
        OWN wall clock (``runner.py``'s ``asyncio.timeout``) cancels the adapter
        task while it sits in ``await_inflight`` on a TOOL. The swallow sets the
        flag; the tool settles; ``await_inflight`` re-delivers once; that
        ``CancelledError`` reaches ``asyncio.timeout.__aexit__``, which spends
        exactly one ``uncancel()`` and converts it into the ``TimeoutError``
        that publishes ``budget-exhausted``/``wall-clock``. At that instant the
        cancellation is RETIRED — the scope that issued it has claimed it and
        balanced its own books. But the flag still said "cancelled", so the very
        next boundary (``profile:agentic-after``) raised a fresh raw
        ``CancelledError``, throwing away the terminal the runner had just
        computed; and the OUTER ``await_inflight`` — in the WORKFLOW task, which
        nothing had ever cancelled — read the same shared flag and raised
        another. ``CancelledError`` is a ``BaseException``: ``_execute``'s
        handlers caught none of it, the ``finally`` released the lease, and the
        run was left ``status=running``, ``terminal_at=NULL``, no lifecycle
        outbox row, DBOS ``PENDING``.

        ``Task.cancelling()`` is the stdlib's own ledger for exactly this
        question: cancellations delivered to a task that no scope has retired.
        ``cancel()`` increments it and ONLY ``uncancel()`` decrements it, and
        ``uncancel()`` is what ``asyncio.timeout.__aexit__`` spends when it
        converts its own deadline. So the boundary now remembers WHICH tasks it
        swallowed a cancellation for and re-delivers only while one of them is
        still owed one:

          * an operator cancel, a shutdown, a DBOS task cancel — nobody
            uncancels, the count stays positive, and the re-delivery reaches
            every frame of the run exactly as before. A caller cancellation is
            still never swallowed, and no new work starts under one;
          * the run's own wall clock — retired by the very ``asyncio.timeout``
            scope that issued it, count back to zero, nothing left to
            re-deliver. The run then reaches the SAME
            ``budget_exhausted``/wall-clock terminal a deadline landing outside
            this fence has always reached.

        ⚠️ THE TASKS ARE HELD, not just a boolean, and that is load-bearing in
        BOTH directions. Asking ``current_task().cancelling()`` instead would
        have let the cancelled workflow task's own instruction be ignored by the
        adapter task running underneath it, which is a live guard
        (``test_caller_cancellation_settles_admitted_effect_before_releasing_lease``
        measures it as a second model effect issued after Cancel). Asking only
        "was anything ever swallowed" is the stranding. The set is bounded by
        the run's task nesting and lives no longer than the boundary.

        ⚠️ AND A FINISHED TASK IS SKIPPED, which is not tidiness — it is the
        second half of the fix and it was found by measurement, not by reading.
        The frame that actually calls ``await_inflight`` for a model or Tool
        effect is not the adapter task: pydantic-graph runs the agent graph in
        its own tracked child task, and THAT is the task the swallow records.
        When the wall clock cancels the adapter task, asyncio cancels the graph
        task it is awaiting, and pydantic-graph re-delivers that cancellation in
        a loop — measured at ``cancelling() == 119424`` on a 0.6 s deadline, and
        nothing ever uncancels it, because the task simply dies. Its
        cancellation was consumed by its own termination; a task that will never
        run another line cannot still be owed one. Counting it kept every fenced
        deadline stranded even with the ledger check in place.
        """
        return any(not task.done() and task.cancelling() > 0 for task in self._cancelled_callers)

    async def heartbeat(self, stage: str) -> None:
        if not stage:
            raise ValueError("Run execution boundary stage must be non-empty")
        if self._redeliver_cancellation():
            raise asyncio.CancelledError
        try:
            # The stage name was always passed here and always discarded. It is the
            # only marker the platform has for where a Run's wall-clock goes, and
            # POST_DEMO item 12 needed exactly it.
            #
            # ⚠️ THIS COMMENT USED TO NAME THE WRONG REGION, and an adversarial pass
            # found it here -- at the mechanism every marker in this PR goes
            # through -- three rounds after the record had retracted it. It said
            # 7.6 s of the 10.2 s a reader waits falls between `profile:binding`
            # and `profile:agentic-before`. Measured over three live post-deploy
            # runs, everything before the agent loop starts totals 0.11 s
            # COMBINED, and the 7.67 s average sits in ONE later edge:
            # `profile:agentic-before` to `run-model:before:0`. The old sentence
            # was wrong by roughly seventy times, in the direction that would send
            # a reader to admission and profile binding, which are not the
            # problem. Deleted rather than annotated.
            probe = self._state.renew(self._claim, stage=stage)
        except RuntimeError as exc:
            raise RunLeaseLost("Run execution lease was lost at a step boundary") from exc
        if probe.cancellation_requested:
            raise RunCancellationRequested("Run cancellation reached a step boundary")

    async def checkpoint(self, *, stage: str, safe_detail: Mapping[str, object]) -> None:
        """Persist one safe, lease-fenced checkpoint before its governed effect."""
        if not stage:
            raise ValueError("Run execution boundary checkpoint stage must be non-empty")
        if self._redeliver_cancellation():
            raise asyncio.CancelledError
        try:
            self._state.checkpoint(
                self._claim,
                stage=stage,
                safe_detail=safe_detail,
                required_run_status=RunState.RUNNING,
            )
        except RuntimeError as exc:
            raise RunLeaseLost("Run execution lease was lost before a safe checkpoint") from exc

    async def await_inflight(self, operation: Any, *, stage: str) -> Any:
        if not stage:
            raise ValueError("Run execution boundary stage must be non-empty")
        self.open()
        effect_address = _effect_checkpoint_address(stage)
        if effect_address is not None:
            self._state.checkpoint(
                self._claim,
                stage=f"effect_issued:{effect_address}",
                safe_detail={"effect_stage": stage, "settlement": "issued"},
                required_run_status=RunState.RUNNING,
            )
        task = asyncio.ensure_future(operation)
        cancellation_seen = False
        caller_cancelled = False
        lease_error: RunLeaseLost | None = None
        while not task.done():
            try:
                done, _ = await asyncio.wait({task}, timeout=self._poll_seconds)
            except asyncio.CancelledError:
                # Once admitted, an audit/model/Tool operation is never detached.
                # Keep the exact lease alive until it settles, then propagate the
                # caller cancellation so durable recovery observes one clear edge.
                caller_cancelled = True
                cancelled_caller = asyncio.current_task()
                if cancelled_caller is not None:
                    self._cancelled_callers.add(cancelled_caller)
                continue
            if done:
                break
            if lease_error is None:
                try:
                    probe = self._state.renew(self._claim)
                    cancellation_seen = cancellation_seen or probe.cancellation_requested
                except RuntimeError as exc:
                    lease_error = RunLeaseLost(
                        "Run execution lease was lost while reconciling an in-flight effect"
                    )
                    lease_error.__cause__ = exc
        # An already-admitted external effect is never detached.  Its result is
        # reconciled locally, then discarded if cancellation/takeover won.
        operation_error: BaseException | None = None
        try:
            result = task.result()
        except BaseException as exc:
            operation_error = exc
            result = None
        if lease_error is not None:
            raise lease_error from operation_error
        try:
            probe = self._state.renew(self._claim)
        except RuntimeError as exc:
            raise RunLeaseLost(
                "Run execution lease was lost after an in-flight effect settled"
            ) from exc
        stream_continues = stage.startswith("run-model:stream-wait:") and operation_error is None
        if effect_address is not None and not stream_continues:
            self._state.checkpoint(
                self._claim,
                stage=f"effect_settled:{effect_address}",
                safe_detail={
                    "effect_stage": stage,
                    "settlement": "error" if operation_error is not None else "success",
                },
            )
        if cancellation_seen or probe.cancellation_requested:
            error = RunCancellationRequested(
                "Run cancellation won after an in-flight effect settled"
            )
            if operation_error is not None:
                raise error from operation_error
            raise error
        # The LOCAL half is ungated on purpose: this frame's own await was the
        # one cancelled, nothing has had the chance to retire it yet, and the
        # scope that issued it (`asyncio.timeout`, an outer `wait_for`, the
        # event loop) is entitled to receive it here and account for it. Only
        # the STICKY half — a cancellation swallowed in some earlier frame,
        # possibly in another task — has to prove it is still owed.
        if caller_cancelled or self._redeliver_cancellation():
            raise asyncio.CancelledError from operation_error
        if operation_error is not None:
            raise operation_error
        return result


async def drain_run_approval_audit_intent(
    *,
    checkpoints: Any,
    sink: AuditSink,
    audit_engine: sa.Engine,
    runtime_engine: sa.Engine,
    run_store: AgentRunStore,
    run_id: str,
    attempt_id: str,
    owner_principal_id: str,
    approval_id: str,
) -> None:
    """Drain one park's approval audit intent into its mandatory Run receipt.

    Shared by every seam that may need to terminalize a parked Run: the
    workflow (park leg AND every later execution of the attempt), the worker
    resume step, and the API decision routes.  See
    :meth:`RunWorkflowService.record_approval_audit_receipt` for why the
    receipt address is epoch-independent and why append precedes the outbox
    claim.  Idempotent everywhere: a replay lands on the SAME deterministic
    audit address and the chain returns the existing record.

    THE RECEIPT'S CONFIG HASH IS THE RUN'S PINNED HASH, read here from the
    accepted run row — NEVER a caller-supplied value (Sol cross-model review
    round 2, P1).  An earlier revision took the calling process's hash, and the
    one population this drain exists for — parks written BEFORE the drain
    deployed, backfilled AFTER the restart — is exactly the population decided
    under the OLD hash: ``RunFinalizer`` rejects any receipt whose hash differs
    from the accepted Run (run_finalizer "Run audit receipt config hash differs
    from the accepted Run"), so the process-hash backfill made those runs
    unterminalizable on the very upgrade that was meant to repair them.
    """

    intent_id = await checkpoints.pending_audit_intent(approval_id)
    if intent_id is None:
        return
    accepted = await run_store.get_run(run_id=run_id, owner_principal_id=owner_principal_id)
    if accepted is None or accepted.run_id != run_id:
        raise LookupError("Run approval audit drain cannot resolve its accepted Run")
    config_hash = str(accepted.config_hash)

    def _existing_approval_receipts() -> int:
        with runtime_engine.connect() as conn:
            return int(
                conn.execute(
                    sa.select(sa.func.count())
                    .select_from(ne_agent_run_audit_receipts)
                    .where(
                        ne_agent_run_audit_receipts.c.run_id == run_id,
                        ne_agent_run_audit_receipts.c.attempt_id == attempt_id,
                        ne_agent_run_audit_receipts.c.approval_id == approval_id,
                        ne_agent_run_audit_receipts.c.decision_kind
                        == RunAuditDecisionKind.APPROVAL.value,
                    )
                ).scalar_one()
            )

    # An unclaimed outbox row does NOT prove the receipt is missing — the
    # crash window between the append commit and `complete_audit_intent`
    # leaves exactly this state, and so does a receipt recorded out of band
    # at a different address.  `_observed_approvals` demands EXACTLY ONE
    # receipt per join, so:
    #   0 receipts -> append, then claim (below);
    #   1 receipt  -> the mandate is already satisfied; claim, never append;
    #   >1         -> the run is ALREADY unterminalizable and this seam cannot
    #                 know which receipt is genuine.  Refuse loudly and leave
    #                 the intent UNCLAIMED — claiming it would consume the one
    #                 durable marker pointing an operator at the corruption.
    #                 (docs/ops/approval-resume-repair.md: receipts > 1 needs
    #                 owner intervention before any decision on the approval.)
    existing_receipts = await run_store._call_db(_existing_approval_receipts)
    if existing_receipts == 1:
        await checkpoints.complete_audit_intent(intent_id, claimed_at=datetime.now(UTC))
        return
    if existing_receipts > 1:
        raise RuntimeError(
            "Run approval join carries "
            f"{existing_receipts} decision_kind='approval' audit receipts where exactly one "
            "is mandated; the drain refuses to claim its intent. OPERATOR INTERVENTION "
            "path, NOT a retry (coordinator ruling): resolve the duplicate with the owner "
            "before any decision on this approval — docs/ops/approval-resume-repair.md, "
            "'Step 4 — the four pre-deploy outbox rows: no manual write required'"
        )
    audit = RunAuditReceiptSink(
        sink,
        audit_engine=audit_engine,
        runtime_engine=runtime_engine,
        run_store=run_store,
        target=RunAuditTarget(
            run_id=run_id,
            attempt_id=attempt_id,
            step_id="run-approval",
            decision_kind=RunAuditDecisionKind.APPROVAL,
            event_ordinal=0,
        ),
        config_hash=config_hash,
    )
    await audit.for_target(
        step_id="run-approval",
        event_ordinal=0,
        decision_kind=RunAuditDecisionKind.APPROVAL,
        approval_id=approval_id,
    ).append(
        new_audit_event(
            action="run.approval.requested",
            resource=f"run:{run_id}",
            outcome=AuditOutcome.success,
            actor=owner_principal_id,
            detail="canonical Run parked for human approval; no effect was issued",
            config_hash=config_hash,
        )
    )
    await checkpoints.complete_audit_intent(intent_id, claimed_at=datetime.now(UTC))


class RunWorkflowService:
    """Execute one accepted Run through its durable join and bounded adapter."""

    def __init__(
        self,
        *,
        state: RunWorkflowStateStore,
        run_store: AgentRunStore,
        sink: AuditSink,
        audit_engine: sa.Engine,
        gateway: ModelGateway,
        executor: Any,
        adapter: RunExecutionAdapter,
        config_hash: str,
        checkpoints: Any | None = None,
        source_guard: Any | None = None,
    ) -> None:
        self._state = state
        self._runs = run_store
        self._sink = sink
        self._audit_engine = audit_engine
        self._gateway = gateway
        self._executor = executor
        self._adapter = adapter
        self._config_hash = config_hash
        self._finalizer = RunFinalizer(run_store)
        # The approval-continuation coordinator and the source-authority guard.
        # Both are required to RESUME an approved effect and to make an expired
        # authorization fail loud; a composition without them can still run a
        # Run that never parks, and refuses (fail closed) one that did.
        self._checkpoints = checkpoints
        self._source_guard = source_guard

    async def publish_terminal(self, publication: RunTerminalPublication) -> None:
        """Drain one content-free terminal audit intent with exact receipt fencing."""

        claimed = self._state.claim_terminal_publication(publication.publication_id)
        try:
            receipt = self._state.terminal_audit_receipt(claimed, config_hash=self._config_hash)
            audit_event_id = None if receipt is None else str(receipt["audit_event_id"])
            expected_provenance = {
                "terminal_event_id": claimed.event_id,
                "terminal_publication_id": claimed.publication_id,
                "terminal_intent_hash": claimed.terminal_intent_hash,
            }
            if audit_event_id is None:
                terminal_audit = RunAuditReceiptSink(
                    self._sink,
                    audit_engine=self._audit_engine,
                    runtime_engine=self._state.engine,
                    run_store=self._runs,
                    target=RunAuditTarget(
                        run_id=claimed.run_id,
                        attempt_id=claimed.attempt_id,
                        step_id="run-terminal-publication",
                        decision_kind=RunAuditDecisionKind.TERMINAL,
                        event_ordinal=0,
                    ),
                    config_hash=self._config_hash,
                )
                await terminal_audit.append(
                    new_audit_event(
                        action="run.lifecycle.terminal",
                        resource=f"run:{claimed.run_id}",
                        outcome=AuditOutcome.success,
                        actor=claimed.owner_principal_id,
                        detail="canonical Run terminal lifecycle publication committed",
                        config_hash=self._config_hash,
                        provenance=expected_provenance,
                    )
                )
                audit_event_id = self._state.terminal_audit_event_id(
                    claimed, config_hash=self._config_hash
                )
                receipt = self._state.terminal_audit_receipt(claimed, config_hash=self._config_hash)
            if audit_event_id is None:
                raise RuntimeError("Run terminal audit receipt is missing after publication")
            if receipt is None or str(receipt["audit_event_id"]) != audit_event_id:
                raise RuntimeError("Run terminal audit receipt is unavailable after publication")
            if claimed.published_at is not None and claimed.audit_event_id != audit_event_id:
                raise RuntimeError("published Run terminal outbox differs from its audit receipt")
            records = records_by_event_ids(self._audit_engine, [audit_event_id])
            if len(records) != 1:
                raise RuntimeError("Run terminal audit chain record is unavailable")
            payload = records[0].payload
            provenance = payload.get("provenance")
            expected_target = {
                "run_id": claimed.run_id,
                "attempt_id": claimed.attempt_id,
                "run_event_id": str(receipt["run_event_id"]),
                "step_id": "run-terminal-publication",
                "run_audit_decision_kind": RunAuditDecisionKind.TERMINAL.value,
            }
            if not isinstance(provenance, dict) or any(
                provenance.get(key) != value
                for key, value in {**expected_provenance, **expected_target}.items()
            ):
                raise RuntimeError("Run terminal audit receipt differs from its outbox intent")
            if (
                payload.get("action") != "run.lifecycle.terminal"
                or payload.get("resource") != f"run:{claimed.run_id}"
                or payload.get("actor") != claimed.owner_principal_id
                or payload.get("config_hash") != self._config_hash
                or payload.get("outcome") != AuditOutcome.success.value
                or payload.get("detail") != "canonical Run terminal lifecycle publication committed"
                or int(receipt["audit_sequence"]) != records[0].seq
                or str(receipt["audit_record_hash"]) != records[0].record_hash
            ):
                raise RuntimeError("Run terminal audit record differs from its lifecycle target")
            if claimed.published_at is None:
                self._state.complete_terminal_publication(
                    claimed,
                    audit_event_id=audit_event_id,
                    config_hash=self._config_hash,
                )
        except BaseException:
            self._state.release_terminal_publication(claimed)
            raise

    async def _terminalize_and_publish(
        self,
        claim: RunWorkflowClaim,
        *,
        target: RunTurnTarget,
        status: RunState,
        outcome: str,
        failure_class: str | None = None,
        delivery: object | None = None,
    ) -> RunWorkflowResult:
        from ne.runtime.source_results import RunDeliveryObservation

        observation = delivery if isinstance(delivery, RunDeliveryObservation) else None
        receipt = await self._finalizer.finalize_unavailable(
            RunUnavailableTerminalCandidate(
                target=target,
                status=status.value,  # type: ignore[arg-type]
                outcome=outcome,  # type: ignore[arg-type]
                failure_class=failure_class,
                delivery=observation,
            ),
            lease=_finalizer_lease(claim),
        )
        await self.publish_terminal(self._state.get_terminal_publication(receipt.publication_id))
        return _receipt_workflow_result(receipt, outcome=outcome)

    async def _resolve_effect_continuation(
        self, claim: RunWorkflowClaim, *, target: RunTurnTarget
    ) -> RunEffectContinuation | None:
        """The ONE approved effect this attempt must execute, or ``None``.

        Returns ``None`` for every ordinary Run — the agent loop is untouched.
        For an attempt carrying an approved, resumed continuation this
        re-evaluates the FULL governance floor before naming the effect:

        * the exact approval join, its action/principal/policy hashes and its
          continuation checkpoint must still agree (``load_checkpoint``);
        * every source-authority stamp behind the parked invocation is
          re-evaluated under the run owner's bound authenticated context
          (``authorize(operation="resume")``) — a permanent withholding
          terminalizes the Run and nothing executes, a retryable-unknown raises
          and leaves the continuation pending;
        * the re-authorized arguments must re-hash to the SAME ``action_hash``
          the human's approval was bound to.

        Nothing here is skipped or relaxed relative to the parked path. What is
        removed is the RE-DERIVATION: the effect is read back from the pinned
        checkpoint instead of being regenerated by a second model call.
        """

        approval_id = self._state.resumed_approval_id(claim)
        if approval_id is None:
            return None
        if self._checkpoints is None or self._source_guard is None:
            raise RuntimeError(
                "Run approval continuation cannot execute without its coordinator and guard"
            )
        joined = await self._checkpoints.get_join(approval_id)
        if joined is None or joined.run_id != claim.run_id or joined.attempt_id != claim.attempt_id:
            raise RuntimeError("Run approval continuation join differs from the active claim")
        checkpoint = await self._checkpoints.load_checkpoint(
            approval_id,
            expected_action_hash=joined.action_hash,
            principal_id=joined.principal_id,
            policy_hash=joined.policy_hash,
        )
        if checkpoint.status != "resumed":
            raise RuntimeError("Run approval continuation is not resumed for execution")
        if joined.principal_id != claim.owner_principal_id:
            raise RuntimeError("Run approval continuation principal differs from the Run owner")
        action = self._approval_action(approval_id, joined=joined)
        arguments = await self._checkpoints.authorize(
            approval_id,
            guard=self._source_guard,
            operation="resume",
            decided_at=datetime.now(UTC),
        )
        if arguments is None:
            # ``authorize`` already terminalized the request AND the Run inside
            # its own transaction (``reconcile_source_authority``). Raising here
            # keeps the workflow's failure path honest instead of executing an
            # effect whose evidence current authority now denies.
            raise SourceAuthorityWithheldError(
                "current source authority withheld the approved continuation"
            )
        from ne.runtime.approvals import canonical_args_hash

        if not hmac.compare_digest(canonical_args_hash(arguments), joined.action_hash):
            raise RuntimeError(
                "re-authorized Run continuation arguments differ from the approved action"
            )
        if target.attempt_id != claim.attempt_id:
            raise RuntimeError("Run approval continuation target differs from the active claim")
        return RunEffectContinuation(
            approval_id=approval_id,
            step_id=checkpoint.step_id,
            tool_call_id=checkpoint.tool_call_id,
            action=action,
            arguments=arguments,
        )

    async def _terminalize_authorization_expired(
        self, claim: RunWorkflowClaim
    ) -> RunWorkflowResult | None:
        """Atomically strand nothing: fail the attempt, publish, tell the truth.

        Returns ``None`` when this attempt has no parked continuation to
        terminalize — the caller then re-raises so the failure is still loud.
        """

        approval_id = self._state.resumed_approval_id(claim)
        if approval_id is None or self._checkpoints is None:
            return None
        joined = await self._checkpoints.get_join(approval_id)
        if joined is None:
            return None
        await self._checkpoints.terminalize_authorization_expired(
            approval_id, decided_at=datetime.now(UTC)
        )
        publication = self._state.reconcile_approval_join(
            approval_id=approval_id,
            action_hash=joined.action_hash,
            principal_id=joined.principal_id,
            policy_hash=joined.policy_hash,
        )
        if publication is None:
            return None
        await self.publish_terminal(publication)
        return _publication_workflow_result(publication)

    def _approval_action(self, approval_id: str, *, joined: Any) -> str:
        """The tool the human's approval NAMED — the IMMUTABLE park-time value.

        Sol slice 2 (P1): an earlier revision returned the action from the
        MUTABLE ``ne_approval_requests`` row, and the middleware's tool-name
        check compared the requested effect against that same row — so a
        post-decision ``UPDATE ne_approval_requests SET action = B`` moved BOTH
        sides of every comparison at once and a different registered tool with
        the same argument schema executed under the human's approval of A. The
        only trustworthy side is the insert-only park-time join. The mutable
        row is still read and must AGREE with the join (the same
        ``_require_parked_governance`` floor the decide path applies — drift
        refuses loudly, and the run fails instead of executing), but the value
        the continuation EXECUTES is the join's.
        """

        from ne.runtime.approval_store import ne_approval_requests
        from ne.runtime.run_checkpoint import _require_parked_governance

        with self._state.engine.connect() as conn:
            request = conn.execute(
                sa.select(ne_approval_requests.c.action, ne_approval_requests.c.tier).where(
                    ne_approval_requests.c.request_id == approval_id
                )
            ).one_or_none()
        if request is None or not request.action:
            raise RuntimeError("Run approval continuation names no action")
        _require_parked_governance(joined, tier=request.tier, action=request.action)
        return str(joined.action)

    async def _drain_attempt_approval_receipt(
        self, *, run: AgentRun, claim: RunWorkflowClaim
    ) -> None:
        """Drain the approval audit receipt for whatever wait this attempt holds.

        A no-op when the attempt parked no approval. Idempotent and
        epoch-stable, so it is safe to call at every seam that can precede a
        finalizer — and it must be called at every such seam, because
        ``RunFinalizer._observed_approvals`` refuses to compose ANY terminal for
        an attempt whose join lacks its exactly-one mandatory receipt.
        """

        if self._checkpoints is None:
            return
        parked_join = await self._checkpoints.find_join_for_attempt(
            run_id=run.run_id, attempt_id=claim.attempt_id
        )
        if parked_join is None:
            return
        await self.record_approval_audit_receipt(
            run_id=run.run_id,
            attempt_id=claim.attempt_id,
            owner_principal_id=run.owner_principal_id,
            approval_id=parked_join.approval_id,
        )

    async def _record_parked_approval_receipt(
        self, audit: RunAuditReceiptSink, *, claim: RunWorkflowClaim, approval_id: str
    ) -> None:
        del audit  # the drain builds its own sink so every drain site is identical
        await self.record_approval_audit_receipt(
            run_id=claim.run_id,
            attempt_id=claim.attempt_id,
            owner_principal_id=claim.owner_principal_id,
            approval_id=approval_id,
        )

    async def record_approval_audit_receipt(
        self,
        *,
        run_id: str,
        attempt_id: str,
        owner_principal_id: str,
        approval_id: str,
    ) -> None:
        """Drain the park's approval audit intent into its MANDATORY Run receipt.

        ``ne_agent_run_approval_audit_outbox`` was written by every park and
        drained by NOTHING, so no ``decision_kind='approval'`` Run audit receipt
        ever existed — and ``RunFinalizer._observed_approvals`` refuses to
        compose ANY terminal for a Run that ever parked an approval
        ("approval join lacks its exact mandatory audit receipt").  That is why
        a resumed run could not even FAIL cleanly: the terminalizer itself
        crashed and the run stayed ``running`` forever.

        The receipt address is EPOCH-INDEPENDENT (``event_ordinal=0``).  The
        dedup address embeds ``event_ordinal`` and lease recovery bumps the
        epoch, so a drain keyed on ``claim.lease_epoch`` was only idempotent
        within one lease: a crash between the append commit and
        ``complete_audit_intent`` re-drained under the NEXT epoch, minted a
        SECOND ``decision_kind='approval'`` receipt at a different address, and
        ``RunFinalizer._observed_approvals`` (which demands exactly one) could
        then never compose ANY terminal for the run.  With ordinal 0 the replay
        lands on the SAME address and the audit chain returns the existing
        record, so ordering stays safe: append first, then mark the outbox row
        claimed — a crash in between retries instead of losing the receipt.

        Callable from every seam that may need to terminalize a Run whose park
        predates the drain (the workflow itself, the worker resume step, and
        the API decision routes) — pre-deploy parks left undrained outbox rows
        that otherwise 500 every decision.
        """

        if self._checkpoints is None:
            return
        await drain_run_approval_audit_intent(
            checkpoints=self._checkpoints,
            sink=self._sink,
            audit_engine=self._audit_engine,
            runtime_engine=self._state.engine,
            run_store=self._runs,
            run_id=run_id,
            attempt_id=attempt_id,
            owner_principal_id=owner_principal_id,
            approval_id=approval_id,
        )

    async def _terminalize_generic_failure(
        self,
        exc: BaseException,
        *,
        request: RunWorkflowRequest,
        claim: RunWorkflowClaim,
        run: AgentRun,
        audit: RunAuditReceiptSink,
        delivery_observation: object | None,
    ) -> RunWorkflowResult | None:
        """Terminalize one unhandled execution failure; ``None`` means re-raise.

        Shared by the generic ``except Exception`` handler and the
        authorization-expiry handler's no-continuation leg — Python runs at
        most ONE handler per ``try``, so the expiry handler cannot "fall
        through" to its sibling and must call the same logic explicitly.
        """

        # A failure AFTER a standing-approval resume leaves the same undrained
        # join the success path guards against, and the terminal below runs the
        # same `_observed_approvals` mandate — so drain here too, before any
        # finalizer is asked to compose anything.
        await self._drain_attempt_approval_receipt(run=run, claim=claim)
        committed_terminal = self._state.terminal_publication(
            dispatch_id=request.dispatch_id,
            run_id=request.run_id,
            owner_principal_id=request.owner_principal_id,
            config_hash=request.config_hash,
        )
        if committed_terminal is not None:
            # The primary terminal transaction already won.  A later audit
            # publication outage remains retryable through its outbox and
            # must not be reinterpreted as a second failure/cancel race.
            return None
        await audit.for_target(step_id="run-execution", event_ordinal=claim.lease_epoch).append(
            new_audit_event(
                action="run.execution.failed",
                resource=f"run:{run.run_id}",
                outcome=AuditOutcome.error,
                actor=run.owner_principal_id,
                detail="execution failure observed before atomic finalization",
                config_hash=self._config_hash,
            )
        )
        if self._state.cancellation_requested(claim):
            self._state.checkpoint(
                claim,
                stage="cancellation_reconciled",
                safe_detail={"scheduled_new_work": False},
            )
            return await self._terminalize_and_publish(
                claim,
                target=request.target,
                status=RunState.CANCELLED,
                outcome="cancelled",
                delivery=delivery_observation,
            )
        # NE-SELECT-REPROBE: a typed governance refusal may publish a
        # STATIC, non-customer reason alongside its class, so a failed run
        # can say WHY instead of only naming an exception.  `safe_reason` is
        # opt-in at the raise site (see ne.api.model_selection and
        # ne.runtime.errors.AgenticNoProgressError), so an arbitrary
        # exception message can never reach this record.
        #
        # EVERY failed Run says why.  An unmarked exception used to leave
        # `error_reason` absent, which reached the reader as an alert with
        # nothing in it and no way to report what happened.  It now falls
        # through to the derived floor, which names the class and discloses
        # nothing else.  A marked reason is still published VERBATIM.
        failure_class = type(exc).__name__
        failure_detail: dict[str, str] = {
            "error_class": failure_class,
            "error_reason": published_failure_reason(
                safe_reason=getattr(exc, "safe_reason", None),
                failure_class=failure_class,
            ),
        }
        self._state.checkpoint(
            claim,
            stage="execution_failed",
            safe_detail=failure_detail,
        )
        await self._terminalize_and_publish(
            claim,
            target=request.target,
            status=RunState.FAILED,
            outcome="failed",
            failure_class=type(exc).__name__,
            delivery=delivery_observation,
        )
        return None

    async def execute(self, request: RunWorkflowRequest) -> RunWorkflowResult:
        if request.config_hash != self._config_hash:
            raise ValueError("Run workflow request config hash differs from worker composition")
        if request.target.run_id != request.run_id:
            raise ValueError("Run workflow target differs from its Run request")
        persisted_context = await self._runs.load_authenticated_context(
            run_id=request.run_id, owner_principal_id=request.owner_principal_id
        )
        if not hmac.compare_digest(
            persisted_context.context_hash, request.authenticated_context.context_hash
        ):
            raise ValueError("Run workflow authenticated context differs from admission")
        if request.authenticated_context.principal_id != request.owner_principal_id:
            raise ValueError("Run workflow authenticated principal differs from its owner")
        published = self._state.terminal_publication(
            dispatch_id=request.dispatch_id,
            run_id=request.run_id,
            owner_principal_id=request.owner_principal_id,
            config_hash=request.config_hash,
        )
        if published is not None:
            _require_publication_target(published, request.target)
            await self.publish_terminal(published)
            return _publication_workflow_result(published)
        claim = self._state.claim(
            dispatch_id=request.dispatch_id,
            run_id=request.run_id,
            owner_principal_id=request.owner_principal_id,
            config_hash=request.config_hash,
        )
        run = await self._runs.get_run(
            run_id=request.run_id, owner_principal_id=request.owner_principal_id
        )
        if run is None or run.status not in {RunState.RUNNING, RunState.CANCELLING}:
            raise RuntimeError("claimed Run is unavailable or not executable")
        if (
            request.target.conversation_id != run.conversation_id
            or request.target.attempt_id != claim.attempt_id
        ):
            raise ValueError("Run workflow target differs from the claimed canonical Run attempt")
        boundary = _LeaseExecutionBoundary(self._state, claim)
        boundary.open()
        audit = RunAuditReceiptSink(
            self._sink,
            audit_engine=self._audit_engine,
            runtime_engine=self._state.engine,
            run_store=self._runs,
            target=RunAuditTarget(
                run_id=run.run_id,
                attempt_id=claim.attempt_id,
                step_id="run-execution",
                decision_kind=RunAuditDecisionKind.STATE_TRANSITION,
                event_ordinal=0,
            ),
            config_hash=self._config_hash,
        )
        release_owned = True
        delivery_observation: object | None = None
        try:
            await boundary.heartbeat("run-execution:before-input")
            # BACKFILL THE MANDATORY APPROVAL RECEIPT FIRST.  A run parked
            # BEFORE the park-leg drain existed left an unclaimed
            # ``ne_agent_run_approval_audit_outbox`` row and NO
            # ``decision_kind='approval'`` receipt, so no terminal — success,
            # failure, expiry, anything — could ever compose for it.  Draining
            # here (idempotent, epoch-stable address) makes every path below
            # terminalizable before it is attempted.
            await self._drain_attempt_approval_receipt(run=run, claim=claim)
            succeeded = self._state.get_checkpoint(
                run_id=run.run_id,
                attempt_id=claim.attempt_id,
                stage="execution_succeeded",
            )
            if succeeded is not None:
                recovered_result = _checkpoint_execution_result(succeeded.safe_detail)
                recovered_outcome = recovered_result.outcome
                terminal_status = {
                    "context_exhausted": RunState.CONTEXT_EXHAUSTED,
                    "budget_exhausted": RunState.BUDGET_EXHAUSTED,
                }.get(recovered_outcome)
                if terminal_status is not None:
                    return await self._terminalize_and_publish(
                        claim,
                        target=request.target,
                        status=terminal_status,
                        outcome=recovered_outcome,
                    )
                raise RuntimeError(
                    "legacy execution-complete checkpoint has no atomic finalizer receipt"
                )
            input_bytes = await self._runs.materialize_run_input(
                run_id=run.run_id, owner_principal_id=run.owner_principal_id
            )
            self._state.checkpoint(
                claim,
                stage="input_loaded",
                safe_detail={"input_hash": hashlib.sha256(input_bytes).hexdigest()},
            )
            self._state.checkpoint(
                claim,
                stage="execution_started",
                safe_detail={"status": "started"},
            )
            if claim.recovered:
                await boundary.await_inflight(
                    audit.for_target(
                        step_id="run-execution-recovery",
                        event_ordinal=claim.lease_epoch,
                    ).append(
                        new_audit_event(
                            action="run.model.recovery",
                            resource=f"run:{run.run_id}",
                            outcome=AuditOutcome.error,
                            actor=run.owner_principal_id,
                            detail=(
                                "prior model attempt may have succeeded before checkpoint; "
                                "retry remains explicit"
                            ),
                            config_hash=self._config_hash,
                        )
                    ),
                    stage="run-execution:recovery-audit",
                )
                if self._state.has_issued_effect(claim) and not self._state.has_resumed_approval(
                    claim
                ):
                    return await self._terminalize_and_publish(
                        claim,
                        target=request.target,
                        status=RunState.FAILED,
                        outcome="failed",
                        failure_class="RunEffectSettlementUncertain",
                    )
            await boundary.await_inflight(
                audit.append(
                    new_audit_event(
                        action="run.execution.pre_effect",
                        resource=f"run:{run.run_id}",
                        outcome=AuditOutcome.success,
                        actor=run.owner_principal_id,
                        detail=(
                            "pre-effect audit intent; bounded adapter remains blocked until "
                            "its exact Run receipt persists"
                        ),
                        config_hash=self._config_hash,
                    )
                ),
                stage="run-execution:pre-effect-audit",
            )
            # THE APPROVAL RESUME.  A run whose attempt carries an approved,
            # resumed continuation is NOT re-run: it executes the ONE effect the
            # human approved and nothing else.  Re-entering the agent loop here
            # is what killed every resume this platform has ever attempted — the
            # re-called model regenerates the `step_intent` prose inside the
            # `agentic-intent:{ordinal}` checkpoint digest, the replay collides
            # with the parked attempt's row, and `checkpoint()` raises "Run
            # checkpoint was replayed under a different intent" BEFORE
            # finalization, leaving the run in `running` forever with the
            # approval already consumed.  Re-deriving the arguments would be
            # worse still: the human approved a specific message, not a tool.
            continuation = await self._resolve_effect_continuation(claim, target=request.target)
            tool_step_id = "run-tool" if continuation is None else continuation.step_id
            # SEED THE OBSERVER WITH WHAT THIS ATTEMPT ALREADY DELIVERED.  The
            # observer is process-local but the delivery receipts are durable
            # and keyed to the ATTEMPT: a resume (same attempt_id, new process)
            # opens a fresh observer while the parked attempt's pre-park
            # protected deliveries are already persisted.  Unseeded, the
            # snapshot can never equal the persisted set again, so the success
            # finalizer raises "did not capture the complete receipt set" AFTER
            # the effect — and because the boundary completed, the FAILED
            # finalizer then rejects the same snapshot too
            # ("unavailable terminal delivery observation is mismatched"),
            # which is exactly how three production sends terminalized nothing.
            persisted_delivery_ids = await self._runs.delivered_receipt_ids(
                run_id=run.run_id, attempt_id=claim.attempt_id
            )
            with (
                run_delivery_observation_scope(
                    run_id=run.run_id, attempt_id=claim.attempt_id
                ) as delivery_observer,
                run_observation_audit_scope(audit),
                model_call_scope(run_id=run.run_id, model_call_attempt_id=claim.attempt_id),
            ):
                for receipt_id in persisted_delivery_ids:
                    delivery_observer.record_receipt(
                        run_id=run.run_id,
                        attempt_id=claim.attempt_id,
                        receipt_id=receipt_id,
                    )
                result = await boundary.await_inflight(
                    self._adapter.execute(
                        RunExecutionContext(
                            run=run,
                            attempt_id=claim.attempt_id,
                            target=request.target,
                            input_bytes=input_bytes,
                            gateway=ReceiptGatedGateway(
                                self._gateway,
                                audit=audit,
                                owner_principal_id=run.owner_principal_id,
                                step_id="run-model",
                                boundary=boundary,
                            ),
                            executor=ReceiptGatedToolExecutor(
                                self._executor,
                                audit=audit,
                                run=run,
                                target=request.target,
                                owner_principal_id=run.owner_principal_id,
                                step_id=tool_step_id,
                                boundary=boundary,
                                pinned_tool_call_id=(
                                    None if continuation is None else continuation.tool_call_id
                                ),
                            ),
                            continuation=continuation,
                            audit_sink=audit,
                            recovered_uncertain_model_attempt=claim.recovered,
                            boundary=boundary,
                        )
                    ),
                    stage="run-execution:adapter-wait",
                )
                delivery_observation = delivery_observer.snapshot()
            await boundary.heartbeat("run-execution:adapter-complete")
            # AND AGAIN, because the attempt may have parked a wait DURING this
            # execution and satisfied it in the same frame (F1: a successor park
            # that consumed a standing approval never returns to the park leg,
            # which is the only other place this drains). `_observed_approvals`
            # demands EXACTLY ONE `decision_kind='approval'` receipt for every
            # join on the attempt, so without this the finalizer below would
            # refuse to compose ANY terminal for a run that resumed from a
            # carryover — the identical hang the pre-deploy parks produced.
            # Idempotent: an epoch-stable address, a no-op when the pre-scan
            # already drained it.
            await self._drain_attempt_approval_receipt(run=run, claim=claim)
            terminal_status = {
                "context_exhausted": RunState.CONTEXT_EXHAUSTED,
                "budget_exhausted": RunState.BUDGET_EXHAUSTED,
            }.get(result.outcome)
            if terminal_status is not None:
                # A run that died on a ceiling must say WHICH ceiling.  The
                # `execution_failed` checkpoint is already the ONE place a terminal
                # publishes an opt-in safe reason, and `run_projection` already
                # reads it for the exhausted statuses as well as the failed one --
                # only the writer was missing on this path, so an exhausted run
                # rendered a blank where the failure path renders a sentence.  The
                # reason stays OPT-IN: a producer that did not say leaves the
                # detail out entirely rather than have this seam invent one.
                exhausted_reason = getattr(result, "exhausted_reason", None)
                if isinstance(exhausted_reason, str) and exhausted_reason:
                    self._state.checkpoint(
                        claim,
                        stage="execution_failed",
                        safe_detail={"error_reason": exhausted_reason},
                    )
                return await self._terminalize_and_publish(
                    claim,
                    target=request.target,
                    status=terminal_status,
                    outcome=result.outcome,
                    delivery=delivery_observation,
                )
            if result.output_bytes is None or result.structured_envelope is None:
                raise ValueError("execution completed without a structured terminal envelope")
            observations = await self._finalizer.observe(
                target=request.target,
                delivery=delivery_observation,
                evidence=result.evidence_bindings,
                verifiers=result.verifier_bindings,
            )
            finalizer_receipt = await self._finalizer.finalize(
                RunTerminalCandidate(
                    observations=observations,
                    envelope=result.structured_envelope,
                    output_bytes=result.output_bytes,
                ),
                lease=_finalizer_lease(claim),
            )
            committed = self._state.get_terminal_publication(finalizer_receipt.publication_id)
            await self.publish_terminal(committed)
            # Read the outcome the finalizer COMMITTED rather than re-deriving it.
            # Re-deriving is how the first result came to disagree with the durable
            # terminal: the finalizer decides `approved_effect_complete` from the
            # continuation checkpoints, this seam cannot see them, and a replay —
            # which reads the outbox — would then return a different outcome for the
            # same run than its first completion did. One committed fact, read once.
            return _receipt_workflow_result(finalizer_receipt, outcome=committed.outcome)
        except RunApprovalWaiting as pending:
            if pending.joined.run_id != run.run_id or pending.joined.attempt_id != claim.attempt_id:
                raise RuntimeError(
                    "Run approval wait differs from the exact active workflow claim"
                ) from pending
            await self._record_parked_approval_receipt(
                audit, claim=claim, approval_id=pending.joined.approval_id
            )
            return RunWorkflowResult(
                run_id=run.run_id,
                attempt_id=claim.attempt_id,
                outcome="waiting_approval",
            )
        except AuthorizationExpiredError as exc:
            # The specification's missing half.  The bound snapshot is at or past
            # its verified expiry, so NOTHING executes and nothing is disclosed —
            # that part always held.  What never existed was the atomic
            # fail-loud: the error was raised into the void, the parked approval
            # stayed actionable and the Run hung in `running` while the approver
            # held a 200 APPROVED receipt for a continuation that was already
            # dead.  Reviving or rebinding the snapshot, or re-authorizing as the
            # service identity, are RED — so the ONLY correct
            # action is to say so, terminally.
            terminal = await self._terminalize_authorization_expired(claim)
            if terminal is not None:
                return terminal
            # No parked continuation to expire — an ORDINARY run's authorization
            # simply ran out mid-execution.  A dedicated handler that bare-raised
            # here skipped the sibling `except Exception` (one `try` runs at most
            # one handler), so the run stranded in `running` where it used to
            # reach a FAILED terminal.  Route it through the SAME generic
            # failure terminalization instead.
            handled = await self._terminalize_generic_failure(
                exc,
                request=request,
                claim=claim,
                run=run,
                audit=audit,
                delivery_observation=delivery_observation,
            )
            if handled is not None:
                return handled
            raise
        except RunCancellationRequested:
            await audit.for_target(step_id="run-execution", event_ordinal=claim.lease_epoch).append(
                new_audit_event(
                    action="run.execution.cancelled",
                    resource=f"run:{run.run_id}",
                    outcome=AuditOutcome.error,
                    actor=run.owner_principal_id,
                    detail="execution cancellation observed before atomic finalization",
                    config_hash=self._config_hash,
                )
            )
            self._state.checkpoint(
                claim,
                stage="cancellation_reconciled",
                safe_detail={"scheduled_new_work": False},
            )
            return await self._terminalize_and_publish(
                claim,
                target=request.target,
                status=RunState.CANCELLED,
                outcome="cancelled",
                delivery=delivery_observation,
            )
        except RunLeaseLost:
            release_owned = False
            raise
        except Exception as exc:
            handled = await self._terminalize_generic_failure(
                exc,
                request=request,
                claim=claim,
                run=run,
                audit=audit,
                delivery_observation=delivery_observation,
            )
            if handled is not None:
                return handled
            raise
        finally:
            # Release on both success and explicit failure. A process death before
            # this line leaves the durable active lease for the joined recovery.
            try:
                if release_owned:
                    self._state.release(claim)
            finally:
                boundary.close()


def _finalizer_lease(claim: RunWorkflowClaim) -> RunFinalizerLease:
    return RunFinalizerLease(
        dispatch_id=claim.dispatch_id,
        run_id=claim.run_id,
        attempt_id=claim.attempt_id,
        lease_token=claim.lease_token,
        lease_epoch=claim.lease_epoch,
    )


def _receipt_workflow_result(receipt: RunFinalizerReceipt, *, outcome: str) -> RunWorkflowResult:
    return RunWorkflowResult(
        run_id=receipt.run_id,
        attempt_id=receipt.attempt_id,
        outcome=_workflow_outcome(outcome),
        protected_result_ref=receipt.protected_output_ref,
        protected_result_hash=receipt.protected_output_hash,
        target_hash=receipt.target_hash,
        protected_output_kind=receipt.protected_output_kind,
        mapping_state=receipt.mapping_state,
        output_verdict=receipt.output_verdict,
        envelope_hash=receipt.envelope_hash,
        delivery_set_id=receipt.delivery_set_id,
        delivery_set_hash=receipt.delivery_set_hash,
        audit_receipt_set_hash=receipt.audit_receipt_set_hash,
        approval_set_hash=receipt.approval_set_hash,
        evidence_set_hash=receipt.evidence_set_hash,
        verifier_set_hash=receipt.verifier_set_hash,
    )


def _publication_workflow_result(publication: RunTerminalPublication) -> RunWorkflowResult:
    return RunWorkflowResult(
        run_id=publication.run_id,
        attempt_id=publication.attempt_id,
        outcome=_workflow_outcome(publication.outcome),
        protected_result_ref=publication.protected_output_ref,
        protected_result_hash=publication.protected_output_hash,
        target_hash=publication.target_hash,
        protected_output_kind=publication.protected_output_kind,
        mapping_state=publication.mapping_state,
        output_verdict=publication.output_verdict.value,
        envelope_hash=publication.envelope_hash,
        delivery_set_id=publication.delivery_set_id,
        delivery_set_hash=publication.delivery_set_hash,
        audit_receipt_set_hash=publication.audit_receipt_set_hash,
        approval_set_hash=publication.approval_set_hash,
        evidence_set_hash=publication.evidence_set_hash,
        verifier_set_hash=publication.verifier_set_hash,
    )


def _require_publication_target(publication: RunTerminalPublication, target: RunTurnTarget) -> None:
    expected = _digest({"schema": "ne.run-terminal-target.v1", **target.model_dump(mode="json")})
    if publication.target_hash is None or not hmac.compare_digest(
        publication.target_hash, expected
    ):
        raise RuntimeError("terminal Run replay is not bound to the exact canonical target")


def _checkpoint_outcome(
    value: object,
) -> Literal["execution_complete", "context_exhausted", "budget_exhausted"]:
    if value not in {"execution_complete", "context_exhausted", "budget_exhausted"}:
        raise RuntimeError("Run execution checkpoint carries an invalid outcome")
    return cast(
        Literal["execution_complete", "context_exhausted", "budget_exhausted"],
        value,
    )


def _checkpoint_execution_result(detail: Mapping[str, object]) -> RunExecutionResult:
    reference = detail.get("protected_result_ref")
    digest = detail.get("protected_result_hash")
    if reference is not None and not isinstance(reference, str):
        raise RuntimeError("Run execution checkpoint carries an invalid protected result ref")
    if digest is not None and not isinstance(digest, str):
        raise RuntimeError("Run execution checkpoint carries an invalid protected result hash")
    try:
        return RunExecutionResult(
            outcome=_checkpoint_outcome(detail.get("outcome")),
            protected_result_ref=reference,
            protected_result_hash=digest,
        )
    except ValueError as exc:
        raise RuntimeError("Run execution checkpoint carries an invalid protected result") from exc


def _workflow_outcome(
    value: object,
) -> Literal[
    "execution_complete",
    "approved_effect_complete",
    "context_exhausted",
    "budget_exhausted",
    "cancelled",
    "failed",
    "expired",
    "refused",
]:
    allowed = {
        "execution_complete",
        "approved_effect_complete",
        "context_exhausted",
        "budget_exhausted",
        "cancelled",
        "failed",
        "expired",
        "refused",
    }
    if value not in allowed:
        raise RuntimeError("Run terminal publication carries an invalid outcome")
    return cast(
        Literal[
            "execution_complete",
            "approved_effect_complete",
            "context_exhausted",
            "budget_exhausted",
            "cancelled",
            "failed",
            "expired",
            "refused",
        ],
        value,
    )
