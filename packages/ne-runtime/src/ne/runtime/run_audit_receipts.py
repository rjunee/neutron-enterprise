# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Exact-target Run audit wrapper and mandatory receipt gate (PRE-010B).

The ordinary append-only sink remains the sole audit log.  This wrapper adds a
server-owned Run target to each event's hash-covered provenance, reads back the
exact committed chain row, and only then persists 009A's ``RunAuditReceipt``.
Callers cannot pre-populate or override any target key.  A missing chain row,
payload drift, or receipt-store failure raises ``RuntimeAuditError``; callers must
not proceed with the governed effect.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock
from typing import Any

import sqlalchemy as sa
from pydantic import BaseModel

from ne.audit import records_by_event_ids
from ne.audit.engine import call_db, default_offload
from ne.contracts.events import AuditEventLike
from ne.contracts.governance import AuditSink, AuditVerification
from ne.events import new_audit_event
from ne.runtime.errors import RuntimeAuditError
from ne.runtime.run_store import (
    AgentRun,
    AgentRunStore,
    RunAuditDecisionKind,
    RunAuditReceipt,
    ne_agent_delivery_receipts,
    ne_agent_evidence_records,
    ne_agent_run_attempt_links,
    ne_agent_run_event_seq,
    ne_agent_run_events,
)
from ne.runtime.store import metadata
from ne.schema import AuditOutcome

__all__ = [
    "AgenticPlanStepAnnouncement",
    "ExistingRunEvidenceContext",
    "ExistingRunEvidenceProvider",
    "RunAuditReceiptSink",
    "RunAuditTarget",
    "RunToolCallContext",
    "agentic_plan_step_scope",
    "current_agentic_plan_step",
    "current_run_observation_audit",
    "current_run_tool_call",
    "ensure_run_audit_intent_schema",
    "run_observation_audit_scope",
    "run_tool_call_scope",
]


_TARGET_KEYS = frozenset(
    {
        "run_id",
        "attempt_id",
        "run_event_id",
        "step_id",
        "tool_call_id",
        "approval_id",
        "run_audit_decision_kind",
    }
)
_EVENT_NAMESPACE = uuid.UUID("981f7f5c-9e4b-4b15-8225-5ad15d23a42b")

ne_agent_run_audit_intents = sa.Table(
    "ne_agent_run_audit_intents",
    metadata,
    sa.Column("address_hash", sa.String(64), primary_key=True),
    sa.Column("event_intent_hash", sa.String(64), nullable=False),
    sa.Column("run_id", sa.String, nullable=False),
    sa.Column("attempt_id", sa.String, nullable=False),
    sa.Column("run_event_id", sa.String, nullable=False, unique=True),
    sa.Column("audit_event_id", sa.String, nullable=False, unique=True),
    sa.Column("step_id", sa.String, nullable=True),
    sa.Column("tool_call_id", sa.String, nullable=True),
    sa.Column("approval_id", sa.String, nullable=True),
    sa.Column("decision_kind", sa.String, nullable=False),
    sa.Column("event_ordinal", sa.Integer, nullable=False),
    sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(
        ["attempt_id", "run_id"],
        ["ne_agent_run_attempt_links.attempt_id", "ne_agent_run_attempt_links.run_id"],
        name="fk_run_audit_intent_attempt",
    ),
    sa.ForeignKeyConstraint(
        ["run_event_id", "run_id", "attempt_id"],
        [
            "ne_agent_run_events.event_id",
            "ne_agent_run_events.run_id",
            "ne_agent_run_events.attempt_id",
        ],
        name="fk_run_audit_intent_event",
    ),
    sa.CheckConstraint("length(event_intent_hash) = 64", name="ck_run_audit_event_intent"),
    sa.CheckConstraint("event_ordinal >= 0", name="ck_run_audit_event_ordinal"),
)


@dataclass(frozen=True)
class RunAuditTarget:
    run_id: str
    attempt_id: str
    step_id: str
    decision_kind: RunAuditDecisionKind = RunAuditDecisionKind.STATE_TRANSITION
    tool_call_id: str | None = None
    approval_id: str | None = None
    event_ordinal: int = 0
    run_event_id: str | None = None

    def __post_init__(self) -> None:
        if not self.run_id or not self.attempt_id or not self.step_id:
            raise ValueError("Run audit target identity must be complete")
        if self.event_ordinal < 0:
            raise ValueError("Run audit target event ordinal must not be negative")
        if self.run_event_id is not None and self.decision_kind not in {
            RunAuditDecisionKind.DELIVERY,
            RunAuditDecisionKind.EVIDENCE,
        }:
            raise ValueError("only delivery/evidence audit targets may bind an existing Run event")


@dataclass(frozen=True)
class _ClaimedAuditIntent:
    run_event_id: str
    audit_event_id: str
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class ExistingRunEvidenceContext:
    """Non-authoring identity for one read in an existing Run Tool call."""

    run_id: str
    step_id: str
    tool_call_id: str
    read_ordinal: int


class ExistingRunEvidenceProvider:
    """Allocate read ordinals only from canonical store-returned records."""

    _run_id: str
    _step_id: str
    _tool_call_id: str
    _next_ordinal: int
    _lock: Lock

    def __init__(self, *_: object, **__: object) -> None:
        raise TypeError("use ExistingRunEvidenceProvider.from_existing")

    @classmethod
    def from_existing(
        cls, *, run: AgentRun, receipt: RunAuditReceipt
    ) -> ExistingRunEvidenceProvider:
        if type(run) is not AgentRun or type(receipt) is not RunAuditReceipt:
            raise TypeError("existing Run evidence requires canonical store records")
        if (
            receipt.run_id != run.run_id
            or not receipt.step_id
            or not receipt.tool_call_id
            or not receipt.run_event_id
        ):
            raise ValueError("audit receipt is not an exact existing Run tool-call context")
        instance = object.__new__(cls)
        instance._run_id = run.run_id
        instance._step_id = receipt.step_id
        instance._tool_call_id = receipt.tool_call_id
        instance._next_ordinal = 0
        instance._lock = Lock()
        return instance

    def next_read(self) -> ExistingRunEvidenceContext:
        """Return the next exact identity without authoring evidence."""
        with self._lock:
            ordinal = self._next_ordinal
            self._next_ordinal += 1
        return ExistingRunEvidenceContext(
            run_id=self._run_id,
            step_id=self._step_id,
            tool_call_id=self._tool_call_id,
            read_ordinal=ordinal,
        )


@dataclass(frozen=True, slots=True)
class RunToolCallContext:
    """Server-only canonical Run Tool-call carrier; contains no payload bytes."""

    run: AgentRun
    receipt: RunAuditReceipt
    conversation_id: str
    turn_id: str
    turn_sequence: int
    attachment_id: str
    evidence: ExistingRunEvidenceProvider
    served_protected_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.run) is not AgentRun or type(self.receipt) is not RunAuditReceipt:
            raise TypeError("Run Tool-call context requires canonical store records")
        if (
            self.receipt.run_id != self.run.run_id
            or not self.receipt.attempt_id
            or not self.receipt.step_id
            or not self.receipt.tool_call_id
            or self.conversation_id != self.run.conversation_id
            or not self.turn_id
            or self.turn_sequence < 1
            or not self.attachment_id
        ):
            raise ValueError("Run Tool-call context identity is incomplete or inconsistent")


@dataclass(frozen=True, slots=True)
class AgenticPlanStepAnnouncement:
    """The plan ordinal the agentic planner is executing RIGHT NOW.

    Set by the planner (`ne.runtime.runner._RecordedExecutor`) around exactly
    one governed `execute` call, after it has durably checkpointed
    ``agentic-intent:{ordinal}`` for the same call. The receipt-gated executor
    reads it in the SAME frame that mints the step's ``tool_call_id`` and
    checkpoints the ``ordinal -> tool_call_id`` binding
    (``agentic-call:{ordinal}``). The binding is therefore stated by the one
    place that knows both identifiers -- never inferred by counting two
    independent counters, which drift the moment a call is blocked between the
    planner's checkpoint and the executor's receipt.
    """

    ordinal: int
    tool: str


_AGENTIC_PLAN_STEP: ContextVar[AgenticPlanStepAnnouncement | None] = ContextVar(
    "ne_runtime_agentic_plan_step", default=None
)


@contextmanager
def agentic_plan_step_scope(step: AgenticPlanStepAnnouncement) -> Iterator[None]:
    if not isinstance(step, AgenticPlanStepAnnouncement):
        raise TypeError("agentic plan step scope requires a canonical announcement")
    token = _AGENTIC_PLAN_STEP.set(step)
    try:
        yield
    finally:
        _AGENTIC_PLAN_STEP.reset(token)


def current_agentic_plan_step() -> AgenticPlanStepAnnouncement | None:
    step = _AGENTIC_PLAN_STEP.get()
    return step if isinstance(step, AgenticPlanStepAnnouncement) else None


_RUN_TOOL_CALL: ContextVar[RunToolCallContext | None] = ContextVar(
    "ne_runtime_run_tool_call", default=None
)
_RUN_OBSERVATION_AUDIT: ContextVar[Any] = ContextVar(
    "ne_runtime_run_observation_audit", default=None
)


@contextmanager
def run_tool_call_scope(context: RunToolCallContext) -> Iterator[RunToolCallContext]:
    if not isinstance(context, RunToolCallContext):
        raise TypeError("Run Tool-call scope requires a canonical context")
    token = _RUN_TOOL_CALL.set(context)
    try:
        yield context
    finally:
        _RUN_TOOL_CALL.reset(token)


def current_run_tool_call() -> RunToolCallContext | None:
    context = _RUN_TOOL_CALL.get()
    return context if isinstance(context, RunToolCallContext) else None


@contextmanager
def run_observation_audit_scope(
    sink: RunAuditReceiptSink,
) -> Iterator[RunAuditReceiptSink]:
    """Bind the exact Run sink that receipts source observations before disclosure."""

    if not isinstance(sink, RunAuditReceiptSink):
        raise TypeError("Run observation audit scope requires a canonical Run audit sink")
    token = _RUN_OBSERVATION_AUDIT.set(sink)
    try:
        yield sink
    finally:
        _RUN_OBSERVATION_AUDIT.reset(token)


def current_run_observation_audit() -> RunAuditReceiptSink | None:
    sink = _RUN_OBSERVATION_AUDIT.get()
    return sink if isinstance(sink, RunAuditReceiptSink) else None


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _claim_sequence(conn: sa.Connection, run_id: str) -> int:
    """Claim the Run's next dense event sequence in ONE round trip.

    A single upsert-RETURNING (both dialects) replaces the old three-statement
    insert-if-absent / increment / read-back dance: the Run hot path pays this
    on every event write, so the two extra round trips were pure per-event tax
    (perf/audit-intent-path). The row lock the UPDATE arm takes serializes
    concurrent claimants exactly as the old UPDATE did.
    """
    if conn.dialect.name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    elif conn.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:  # pragma: no cover
        raise RuntimeError(f"Run audit receipts do not support {conn.dialect.name!r}")
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


def ensure_run_audit_intent_schema(engine: sa.Engine) -> None:
    ne_agent_run_audit_intents.create(engine, checkfirst=True)


class RunAuditReceiptSink:
    """One receipt-producing sink bound to a server-owned Run target."""

    def __init__(
        self,
        inner: AuditSink,
        *,
        audit_engine: sa.Engine,
        runtime_engine: sa.Engine,
        run_store: AgentRunStore,
        target: RunAuditTarget,
        config_hash: str,
    ) -> None:
        if len(config_hash) != 64:
            raise ValueError("Run audit sink config_hash must be SHA-256")
        self._inner = inner
        self._audit_engine = audit_engine
        self._runtime_engine = runtime_engine
        self._run_store = run_store
        self._target = target
        self._config_hash = config_hash
        # Off-loop policy for this sink's own sync DB bodies (P2-16): offload to
        # a worker thread unless EITHER engine is sqlite (pysqlite connections
        # are thread-bound, so dev/CI sqlite stays on-loop by design — the same
        # rule as ne.audit.engine.default_offload).
        self._offload = default_offload(runtime_engine) and default_offload(audit_engine)

    def for_target(
        self,
        *,
        step_id: str,
        event_ordinal: int,
        decision_kind: RunAuditDecisionKind = RunAuditDecisionKind.STATE_TRANSITION,
        tool_call_id: str | None = None,
        approval_id: str | None = None,
    ) -> RunAuditReceiptSink:
        return RunAuditReceiptSink(
            self._inner,
            audit_engine=self._audit_engine,
            runtime_engine=self._runtime_engine,
            run_store=self._run_store,
            target=RunAuditTarget(
                run_id=self._target.run_id,
                attempt_id=self._target.attempt_id,
                step_id=step_id,
                decision_kind=decision_kind,
                tool_call_id=tool_call_id,
                approval_id=approval_id,
                event_ordinal=event_ordinal,
            ),
            config_hash=self._config_hash,
        )

    def _for_existing_event(
        self,
        *,
        run_event_id: str,
        step_id: str,
        event_ordinal: int,
        decision_kind: RunAuditDecisionKind,
        tool_call_id: str | None = None,
    ) -> RunAuditReceiptSink:
        return RunAuditReceiptSink(
            self._inner,
            audit_engine=self._audit_engine,
            runtime_engine=self._runtime_engine,
            run_store=self._run_store,
            target=RunAuditTarget(
                run_id=self._target.run_id,
                attempt_id=self._target.attempt_id,
                step_id=step_id,
                decision_kind=decision_kind,
                tool_call_id=tool_call_id,
                event_ordinal=event_ordinal,
                run_event_id=run_event_id,
            ),
            config_hash=self._config_hash,
        )

    async def append_delivery_receipt(
        self, *, delivery_receipt_id: str, actor: str
    ) -> RunAuditReceipt:
        """Audit one canonical Run-bound delivery before its bytes leave the seam."""

        if not delivery_receipt_id or not actor:
            raise RuntimeAuditError("delivery audit identity is incomplete")
        row = await call_db(self._offload, self._load_delivery_observation, delivery_receipt_id)
        if (
            row is None
            or row.event_id is None
            or str(row.event_kind) != "delivery.decided"
            or str(row.event_status) != str(row.decision)
        ):
            raise RuntimeAuditError("delivery receipt is not an exact canonical Run observation")
        sink = self._for_existing_event(
            run_event_id=str(row.event_id),
            step_id="run-delivery",
            event_ordinal=int(row.sequence),
            decision_kind=RunAuditDecisionKind.DELIVERY,
        )
        return await sink.append_receipt(
            new_audit_event(
                action="run.delivery.decided",
                resource=f"run:{self._target.run_id}",
                outcome=AuditOutcome.success,
                actor=actor,
                detail="canonical Run delivery observation persisted before disclosure",
                config_hash=self._config_hash,
            )
        )

    def _load_delivery_observation(self, delivery_receipt_id: str) -> Any:
        with self._runtime_engine.connect() as conn:
            return conn.execute(
                sa.select(
                    ne_agent_delivery_receipts.c.delivery_receipt_id,
                    ne_agent_delivery_receipts.c.decision,
                    ne_agent_delivery_receipts.c.event_id,
                    ne_agent_run_events.c.sequence,
                    ne_agent_run_events.c.event_kind,
                    ne_agent_run_events.c.event_status,
                )
                .join(
                    ne_agent_run_events,
                    ne_agent_run_events.c.event_id == ne_agent_delivery_receipts.c.event_id,
                )
                .where(
                    ne_agent_delivery_receipts.c.delivery_receipt_id == delivery_receipt_id,
                    ne_agent_delivery_receipts.c.run_id == self._target.run_id,
                    ne_agent_delivery_receipts.c.receiving_attempt_id == self._target.attempt_id,
                    ne_agent_run_events.c.run_id == self._target.run_id,
                    ne_agent_run_events.c.attempt_id == self._target.attempt_id,
                )
            ).one_or_none()

    async def append_evidence_receipt(
        self, *, producing_event_id: str, actor: str
    ) -> RunAuditReceipt:
        """Audit one canonical Run evidence record before its reference is returned."""

        if not producing_event_id or not actor:
            raise RuntimeAuditError("evidence audit identity is incomplete")
        row = await call_db(self._offload, self._load_evidence_observation, producing_event_id)
        if (
            row is None
            or str(row.event_kind) != "evidence.produced"
            or str(row.event_status) != "preterminal"
        ):
            raise RuntimeAuditError("evidence record is not an exact canonical Run observation")
        sink = self._for_existing_event(
            run_event_id=str(row.producing_event_id),
            step_id=str(row.step_id),
            tool_call_id=str(row.tool_call_id),
            event_ordinal=int(row.sequence),
            decision_kind=RunAuditDecisionKind.EVIDENCE,
        )
        return await sink.append_receipt(
            new_audit_event(
                action="run.evidence.produced",
                resource=f"run:{self._target.run_id}",
                outcome=AuditOutcome.success,
                actor=actor,
                detail="canonical Run evidence observation persisted before publication",
                config_hash=self._config_hash,
            )
        )

    def _load_evidence_observation(self, producing_event_id: str) -> Any:
        with self._runtime_engine.connect() as conn:
            return conn.execute(
                sa.select(
                    ne_agent_evidence_records.c.producing_event_id,
                    ne_agent_evidence_records.c.step_id,
                    ne_agent_evidence_records.c.tool_call_id,
                    ne_agent_run_events.c.sequence,
                    ne_agent_run_events.c.event_kind,
                    ne_agent_run_events.c.event_status,
                )
                .join(
                    ne_agent_run_events,
                    ne_agent_run_events.c.event_id
                    == ne_agent_evidence_records.c.producing_event_id,
                )
                .where(
                    ne_agent_evidence_records.c.producing_event_id == producing_event_id,
                    ne_agent_evidence_records.c.run_id == self._target.run_id,
                    ne_agent_evidence_records.c.attempt_id == self._target.attempt_id,
                    ne_agent_run_events.c.run_id == self._target.run_id,
                    ne_agent_run_events.c.attempt_id == self._target.attempt_id,
                )
            ).one_or_none()

    async def append(self, event: AuditEventLike) -> None:
        await self._persist(event, return_receipt=False)

    async def append_receipt(self, event: AuditEventLike) -> RunAuditReceipt:
        """Persist and return the exact canonical receipt before an effect proceeds."""
        receipt = await self._persist(event, return_receipt=True)
        if receipt is None:  # pragma: no cover - guarded by the exact flag above
            raise RuntimeAuditError("canonical Run audit receipt was not returned")
        return receipt

    async def _persist(
        self, event: AuditEventLike, *, return_receipt: bool
    ) -> RunAuditReceipt | None:
        if not isinstance(event, BaseModel) or not hasattr(event, "model_copy"):
            raise RuntimeAuditError("Run audit sink requires a concrete typed audit event")
        if event.config_hash != self._config_hash:
            raise RuntimeAuditError("Run audit event config hash does not match its accepted Run")
        provenance = dict(event.provenance)
        collisions = _TARGET_KEYS.intersection(provenance)
        if collisions:
            raise RuntimeAuditError(
                "Run audit event attempted to supply server-owned target provenance"
            )
        intent = await call_db(self._offload, self._claim_intent, event)
        stamped_provenance = {
            **provenance,
            "run_id": self._target.run_id,
            "attempt_id": self._target.attempt_id,
            "run_event_id": intent.run_event_id,
            "step_id": self._target.step_id,
            "run_audit_decision_kind": self._target.decision_kind.value,
        }
        if self._target.tool_call_id is not None:
            stamped_provenance["tool_call_id"] = self._target.tool_call_id
        if self._target.approval_id is not None:
            stamped_provenance["approval_id"] = self._target.approval_id
        stamped = event.model_copy(
            update={
                "event_id": intent.audit_event_id,
                "occurred_at": intent.occurred_at,
                "provenance": stamped_provenance,
            }
        )
        try:
            await self._inner.append(stamped)
            # ONE off-loop hop for the whole post-append tail: read back the exact
            # committed chain row, compare it to the stamped intent, and record the
            # receipt — whose canonical stored row (owner enforced in the recording
            # transaction) comes back directly, replacing the old separate
            # ``get_audit_receipt`` re-read (perf/audit-intent-path).
            receipt = await call_db(self._offload, self._verify_and_record, stamped, intent)
            if (
                receipt.attempt_id != self._target.attempt_id
                or receipt.run_event_id != intent.run_event_id
                or receipt.step_id != self._target.step_id
                or receipt.tool_call_id != self._target.tool_call_id
            ):
                raise RuntimeError("canonical Run audit receipt differs from its exact target")
            return receipt if return_receipt else None
        except RuntimeAuditError:
            raise
        except Exception as exc:
            raise RuntimeAuditError(
                "mandatory Run audit receipt could not be persisted; effect remains blocked"
            ) from exc

    def _verify_and_record(self, stamped: Any, intent: _ClaimedAuditIntent) -> RunAuditReceipt:
        """Sync post-append tail: exact chain read-back, then the receipt write.

        Runs in one ``call_db`` hop. The read-back-before-receipt ordering is
        PRE-010B's guarantee and is unchanged — the receipt is still recorded
        only after the exact committed chain row is read back and matches the
        stamped Run intent byte for byte.
        """
        records = records_by_event_ids(self._audit_engine, [intent.audit_event_id])
        if len(records) != 1 or records[0].payload != stamped.model_dump(mode="json"):
            raise RuntimeError("exact audit chain row is missing or differs from the Run intent")
        record = records[0]
        return self._run_store.record_audit_receipt_sync(
            run_id=self._target.run_id,
            attempt_id=self._target.attempt_id,
            run_event_id=intent.run_event_id,
            step_id=self._target.step_id,
            tool_call_id=self._target.tool_call_id,
            approval_id=self._target.approval_id,
            decision_kind=self._target.decision_kind,
            audit_event_id=intent.audit_event_id,
            audit_sequence=record.seq,
            audit_record_hash=record.record_hash,
            config_hash=self._config_hash,
            owner_principal_id=str(stamped.actor),
        )

    async def verify(
        self, *, start: Any | None = None, end: Any | None = None
    ) -> AuditVerification:
        return await self._inner.verify(start=start, end=end)

    def _claim_intent(self, event: BaseModel) -> _ClaimedAuditIntent:
        typed_event: Any = event
        payload = event.model_dump(mode="json")
        event_intent = {
            key: value
            for key, value in payload.items()
            if key not in {"event_id", "occurred_at", "provenance"}
        }
        event_intent["provenance"] = dict(typed_event.provenance)
        event_intent_hash = _digest(event_intent)
        address = {
            "run_id": self._target.run_id,
            "attempt_id": self._target.attempt_id,
            "run_event_id": self._target.run_event_id,
            "step_id": self._target.step_id,
            "tool_call_id": self._target.tool_call_id,
            "approval_id": self._target.approval_id,
            "decision_kind": self._target.decision_kind.value,
            "event_ordinal": self._target.event_ordinal,
            "action": getattr(event, "action", None),
            "resource": getattr(event, "resource", None),
        }
        address_hash = _digest(address)
        audit_event_id = str(uuid.uuid5(_EVENT_NAMESPACE, address_hash))
        now = datetime.now(UTC)
        with self._runtime_engine.begin() as conn:
            conn.execute(
                sa.select(ne_agent_run_attempt_links.c.attempt_id)
                .where(
                    ne_agent_run_attempt_links.c.run_id == self._target.run_id,
                    ne_agent_run_attempt_links.c.attempt_id == self._target.attempt_id,
                )
                .with_for_update()
            ).one()
            existing = conn.execute(
                sa.select(ne_agent_run_audit_intents).where(
                    ne_agent_run_audit_intents.c.address_hash == address_hash
                )
            ).one_or_none()
            if existing is not None:
                if str(existing.event_intent_hash) != event_intent_hash:
                    raise RuntimeAuditError(
                        "Run audit target was replayed with a different event intent"
                    )
                occurred = existing.occurred_at
                if occurred.tzinfo is None:
                    occurred = occurred.replace(tzinfo=UTC)
                return _ClaimedAuditIntent(
                    run_event_id=str(existing.run_event_id),
                    audit_event_id=str(existing.audit_event_id),
                    occurred_at=occurred,
                )
            run_event_id = self._target.run_event_id
            if run_event_id is None:
                safe_detail = _canonical(
                    {
                        "audit_action_hash": _digest(getattr(event, "action", None)),
                        "audit_resource_hash": _digest(getattr(event, "resource", None)),
                        "event_intent_hash": event_intent_hash,
                    }
                )
                run_event_id = str(uuid.uuid4())
                conn.execute(
                    ne_agent_run_events.insert().values(
                        event_id=run_event_id,
                        run_id=self._target.run_id,
                        attempt_id=self._target.attempt_id,
                        sequence=_claim_sequence(conn, self._target.run_id),
                        event_kind="run.audit.intent",
                        event_status="receipt_pending",
                        parent_event_id=None,
                        step_id=self._target.step_id,
                        safe_detail=safe_detail,
                        detail_hash=hashlib.sha256(safe_detail.encode("utf-8")).hexdigest(),
                        created_at=now,
                    )
                )
            else:
                observed = conn.execute(
                    sa.select(ne_agent_run_events.c.event_kind).where(
                        ne_agent_run_events.c.event_id == run_event_id,
                        ne_agent_run_events.c.run_id == self._target.run_id,
                        ne_agent_run_events.c.attempt_id == self._target.attempt_id,
                    )
                ).one_or_none()
                expected_kind = {
                    RunAuditDecisionKind.DELIVERY: "delivery.decided",
                    RunAuditDecisionKind.EVIDENCE: "evidence.produced",
                }.get(self._target.decision_kind)
                if observed is None or str(observed.event_kind) != expected_kind:
                    raise RuntimeAuditError(
                        "existing Run observation differs from its mandatory audit target"
                    )
            conn.execute(
                ne_agent_run_audit_intents.insert().values(
                    address_hash=address_hash,
                    event_intent_hash=event_intent_hash,
                    run_id=self._target.run_id,
                    attempt_id=self._target.attempt_id,
                    run_event_id=run_event_id,
                    audit_event_id=audit_event_id,
                    step_id=self._target.step_id,
                    tool_call_id=self._target.tool_call_id,
                    approval_id=self._target.approval_id,
                    decision_kind=self._target.decision_kind.value,
                    event_ordinal=self._target.event_ordinal,
                    occurred_at=now,
                )
            )
        return _ClaimedAuditIntent(run_event_id, audit_event_id, now)
