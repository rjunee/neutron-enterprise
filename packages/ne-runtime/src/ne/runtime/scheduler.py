# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""CronScheduler — governed agent and deterministic sole-tool scheduling.

Ordinary scheduled jobs run as their OWN isolated governed agent under a
:class:`~ne.runtime.toolset.ToolsetScopedExecutor` gated to that job's granted
toolset (per-job toolset gating, ENFORCED not advisory), so every cron tool call
still routes through the ONE :class:`GovernedToolExecutor` (C6). The
cron-as-isolated-agent + per-job-gating + schedule/store/run-log factoring is the
OpenClaw (MIT) pattern adopted here — PATTERN ONLY, zero TypeScript code copied.

Definitions may explicitly opt a maintenance job into ``sole-tool`` execution.
That path invokes its exact singleton grant once through the same governed
executor without model arbitration. Connector sync jobs use this mode so source
freshness cannot depend on a model choosing whether or how often to call the only
available tool; all other scheduled definitions remain model-driven by default.

Sync-protocol auditing (the NE-035 precedent): the frozen ``Scheduler`` protocol
is SYNC but the platform ``AuditSink`` is async. :meth:`schedule` / :meth:`cancel`
therefore write the job row AND its typed ``AuditEvent`` to the ne-events OUTBOX in
ONE transaction (``ne.events.append(conn, event)``) — no event without its row, no
row without its event. Do NOT ``asyncio.run`` inside the sync methods.

DUAL-APPEND (Checkpoint-D boundary-audit finding #4, C6 audit completeness): every
cron governance lifecycle event — ``job.schedule`` / ``job.cancel`` / ``job.run`` —
is written to BOTH stores in the SAME transaction: ``ne.events.append`` (the mutable
OUTBOX = export transport) AND ``ne.audit.append_record`` (the trigger-protected,
hash-chained ``ne_audit_log`` = authoritative tamper-evident store), per the plan
§1.4 event-spine rule. The SAME event object goes to both, so both carry the same
``event_id`` and the composed ``config_hash`` (alignment, not duplication). Writing
only to the mutable outbox meant these facts could be silently mutated/deleted while
``verify_chain()`` stayed green; the chain append closes that gap. Fail-closed: a
chain-append failure raises ``RuntimeAuditError`` and the whole cron mutation rolls
back — nothing persisted without its chain record.

The sole exception is manual-request lease recovery after an audit-path failure:
the audited terminal transaction rolls back, then a metadata-only fallback marks
that manual claim failed and releases its lease before propagating the audit error.
This cannot report success and never retries the external action; ordinary scheduled
runs retain the established unfinished-on-audit-failure guarantee.

Cron semantics: expressions are evaluated in UTC via ``cronsim`` (BSD-3-Clause).
:func:`cronsim.CronSim` yields the next match STRICTLY AFTER the reference time, so
computing the next run from NOW collapses any number of missed windows into a
SINGLE run (no catch-up storm).

Durability + multi-worker (NE-070): the due-job claim is a compare-and-set UPDATE
(rowcount-checked) mirroring the approval store — single-writer-safe. Real
multi-worker double-fire safety under concurrent Postgres workers, and the live
DBOS-Postgres tick (an ``@ne_workflow``-wrapped :meth:`run_due_jobs` on
``DBOS.scheduled`` with ``durability='dbos'``), are LIVE-PARKED to NE-070 — the
README shows the composition; there is deliberately NO DBOS import here.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import sqlalchemy as sa
from cronsim import CronSim, CronSimError
from sqlalchemy.exc import IntegrityError

from ne.audit import append_record, ensure_audit_schema, ensure_transactional_sqlite
from ne.contracts import get_config_hash
from ne.contracts.governance import AuditSink, PolicyDecisionPoint
from ne.contracts.models import ModelGateway
from ne.contracts.runtime import ScheduledJob, ToolSpec
from ne.contracts.types import Principal
from ne.events import append, ensure_outbox_schema, new_audit_event
from ne.govern.errors import AuditEmissionError
from ne.runtime.config import RuntimeSettings
from ne.runtime.dispatch import AgentDefinition
from ne.runtime.errors import RuntimeAuditError, SchedulingError, ToolExecutionError
from ne.runtime.middleware import GovernedToolExecutor, scoped_tool_executor
from ne.runtime.principals import AgentPrincipalProvider
from ne.runtime.processes import ProcessRegistry
from ne.runtime.runner import run_isolated_agent
from ne.runtime.toolset import ToolsetScopedExecutor
from ne.schema import AuditEvent, AuditOutcome

_LOG = logging.getLogger(__name__)

scheduler_metadata = sa.MetaData()

ne_scheduled_jobs = sa.Table(
    "ne_scheduled_jobs",
    scheduler_metadata,
    sa.Column("job_id", sa.String, primary_key=True),
    sa.Column("name", sa.String, nullable=False, unique=True),
    sa.Column("cron", sa.String, nullable=False),
    sa.Column("agent", sa.String, nullable=False),
    # JSON array of granted tool names (portable Text; the per-job authoritative grant).
    sa.Column("toolset", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=False, index=True),
)

ne_job_runs = sa.Table(
    "ne_job_runs",
    scheduler_metadata,
    sa.Column("run_id", sa.String, primary_key=True),
    sa.Column("job_id", sa.String, nullable=False, index=True),
    sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("succeeded", sa.Boolean, nullable=True),
    sa.Column("error_class", sa.String, nullable=True),
    sa.Column("output", sa.Text, nullable=True),
    sa.Column("claim_owner", sa.String(36), nullable=True),
    sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
)

ne_job_requests = sa.Table(
    "ne_job_requests",
    scheduler_metadata,
    sa.Column("request_id", sa.String(36), primary_key=True),
    sa.Column("job_id", sa.String, nullable=False, index=True),
    sa.Column("job_name", sa.String(256), nullable=False),
    sa.Column("source", sa.String(128), nullable=False),
    sa.Column("requested_action", sa.String(256), nullable=False),
    sa.Column("tool_name", sa.String(256), nullable=False),
    sa.Column("requester_id", sa.String(512), nullable=False),
    sa.Column("requester_groups", sa.Text, nullable=False),
    sa.Column("config_hash", sa.String(128), nullable=False),
    sa.Column("policy_evidence", sa.String(64), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False, index=True),
    sa.Column("status", sa.String(16), nullable=False, index=True),
    sa.Column("run_id", sa.String, nullable=True, index=True),
    sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("succeeded", sa.Boolean, nullable=True),
    sa.Column("error_class", sa.String(128), nullable=True),
    sa.Column("claim_owner", sa.String(36), nullable=True),
    sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
)
_active_request_predicate = ne_job_requests.c.status.in_(("queued", "running"))
sa.Index(
    "uq_ne_job_requests_active_job",
    ne_job_requests.c.job_id,
    unique=True,
    sqlite_where=_active_request_predicate,
    postgresql_where=_active_request_predicate,
)

_PROVENANCE_SOURCE = {"source": "ne-runtime", "component": "scheduler"}
_SCHEDULER_ACTOR = "ne-runtime.scheduler"


def _to_aware(value: datetime) -> datetime:
    """Normalize SQLite's naive DateTime reads to UTC-aware values."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


_POSTGRES_SCHEMA_LOCK_SQL = "SELECT pg_advisory_xact_lock(5645354348454)"
_SQLITE_SCHEMA_LOCK = threading.Lock()
_LEASE_COLUMNS = (
    (ne_job_runs.name, "claim_owner"),
    (ne_job_runs.name, "lease_expires_at"),
    (ne_job_requests.name, "claim_owner"),
    (ne_job_requests.name, "lease_expires_at"),
)


def _lease_column_ddl(dialect: str, table_name: str, column_name: str) -> str:
    if table_name not in {ne_job_runs.name, ne_job_requests.name}:
        raise SchedulingError(f"unsupported scheduler lease table {table_name!r}")
    if column_name == "claim_owner":
        column_sql = "claim_owner VARCHAR(36)"
    elif column_name == "lease_expires_at":
        column_sql = (
            "lease_expires_at TIMESTAMP WITH TIME ZONE"
            if dialect == "postgresql"
            else "lease_expires_at TIMESTAMP"
        )
    else:
        raise SchedulingError(f"unsupported scheduler lease column {column_name!r}")
    return f"ALTER TABLE {table_name} ADD COLUMN {column_sql}"


def _assert_lease_column_shape(conn: sa.Connection, table_name: str, column_name: str) -> None:
    columns = {str(column["name"]): column for column in sa.inspect(conn).get_columns(table_name)}
    column = columns.get(column_name)
    if column is None:
        raise SchedulingError(
            f"scheduler lease migration did not create {table_name}.{column_name}"
        )
    column_type = column["type"]
    valid_type = (
        isinstance(column_type, sa.String) and column_type.length == 36
        if column_name == "claim_owner"
        else isinstance(column_type, sa.DateTime)
        and (conn.dialect.name != "postgresql" or getattr(column_type, "timezone", False) is True)
    )
    if not valid_type or column.get("nullable") is not True:
        raise SchedulingError(
            f"scheduler lease column {table_name}.{column_name} has an incompatible shape"
        )


def _initialize_scheduler_schema(conn: sa.Connection) -> None:
    """Create and upgrade scheduler tables while the caller holds the schema lock."""
    scheduler_metadata.create_all(conn)
    for table_name, column_name in _LEASE_COLUMNS:
        columns = {str(column["name"]) for column in sa.inspect(conn).get_columns(table_name)}
        if column_name not in columns:
            conn.execute(sa.text(_lease_column_ddl(conn.dialect.name, table_name, column_name)))
        _assert_lease_column_shape(conn, table_name, column_name)


def ensure_scheduler_schema(engine: sa.Engine) -> None:
    """Create the scheduler tables, the ne-events outbox, AND the audit chain (idempotent).

    The outbox rides the same engine as the job rows so each job-row change and its
    typed ``AuditEvent`` commit atomically (mirrors ``ensure_approval_schema``).
    ``ensure_audit_schema`` additionally creates ``ne_audit_log`` / ``ne_audit_head``,
    seeds the genesis head, and installs the append-only triggers on this SAME engine,
    so every cron mutation, its outbox event, and its tamper-evident chain record
    commit in ONE transaction. The composition root MUST hand the SAME engine to the
    scheduler and its ``PostgresAuditSink`` — otherwise the chain would live in a
    second store and the same-transaction dual-append would not hold.
    """
    dialect = engine.dialect.name
    if dialect == "postgresql":
        # Both process roots use this transaction-scoped DB lock. Table creation,
        # inspection, additive migration, and structural postconditions therefore
        # observe one serial schema history on the SAME connection/transaction.
        with engine.begin() as conn:
            conn.exec_driver_sql(_POSTGRES_SCHEMA_LOCK_SQL)
            _initialize_scheduler_schema(conn)
    elif dialect == "sqlite":
        # SQLite is the single-process dev/test runtime. Serialize independent
        # engines in this process around one normal repository transaction. Do not
        # issue BEGIN IMMEDIATE here: ensure_transactional_sqlite installs its own
        # explicit BEGIN hook, and stacking the two double-starts the transaction.
        with _SQLITE_SCHEMA_LOCK, engine.begin() as conn:
            _initialize_scheduler_schema(conn)
    else:
        raise SchedulingError(
            f"scheduler schema initialization does not support dialect {dialect!r}"
        )
    # Additive migration semantics: a NULL lease in an upgraded row is deliberately
    # interpreted as an expired legacy claim during reconciliation.
    ensure_outbox_schema(engine)
    ensure_audit_schema(engine)


def _chain_append(conn: sa.Connection, event: AuditEvent) -> None:
    """Append ``event`` to the tamper-evident hash chain on the CALLER's transaction.

    Fail closed: any failure raises :class:`RuntimeAuditError` so the enclosing cron
    mutation (job row + outbox event) rolls back — nothing persisted without its
    chain record (C6; boundary-audit finding #4).
    """
    try:
        append_record(conn, event)
    except Exception as exc:
        raise RuntimeAuditError(
            f"audit-chain append failed for {event.action!r} on {event.resource!r}; "
            "the cron mutation rolls back (C6 fail closed)"
        ) from exc


def _terminalize_claim(
    engine: sa.Engine,
    *,
    request_id: str | None,
    run_id: str,
    job_id: str,
    job_name: str,
    actor: str,
    now: datetime,
    error_class: str,
    with_audit: bool,
) -> bool:
    """Close one abandoned claim; optionally append its terminal audit atomically."""
    event = new_audit_event(
        action="job.run",
        resource=f"job:{job_name}",
        outcome=AuditOutcome.error,
        actor=actor,
        detail=json.dumps({"name": job_name, "run_id": run_id}),
        provenance={**_PROVENANCE_SOURCE, "reconciled": "true"},
    )
    with engine.begin() as conn:
        existing = conn.execute(
            sa.select(ne_job_runs.c.finished_at).where(ne_job_runs.c.run_id == run_id)
        ).first()
        if existing is None:
            conn.execute(
                ne_job_runs.insert().values(
                    run_id=run_id,
                    job_id=job_id,
                    started_at=now,
                    finished_at=now,
                    succeeded=False,
                    error_class=error_class,
                    output=f"ERROR: {error_class}",
                    claim_owner=run_id,
                    lease_expires_at=now,
                )
            )
        elif existing.finished_at is None:
            conn.execute(
                ne_job_runs.update()
                .where(ne_job_runs.c.run_id == run_id, ne_job_runs.c.finished_at.is_(None))
                .values(
                    finished_at=now,
                    succeeded=False,
                    error_class=error_class,
                    output=f"ERROR: {error_class}",
                    lease_expires_at=now,
                )
            )
        else:
            return False
        request_where = (
            ne_job_requests.c.request_id == request_id
            if request_id is not None
            else ne_job_requests.c.run_id == run_id
        )
        conn.execute(
            ne_job_requests.update()
            .where(request_where, ne_job_requests.c.status.in_(("queued", "running")))
            .values(
                status="failed",
                run_id=run_id,
                finished_at=now,
                succeeded=False,
                error_class=error_class,
                lease_expires_at=now,
            )
        )
        if with_audit:
            try:
                append(conn, event)
                append_record(conn, event)
            except Exception as exc:
                raise RuntimeAuditError(
                    f"terminal reconciliation audit append failed for job {job_name!r}"
                ) from exc
    return True


def _reconcile_expired_claims(engine: sa.Engine, now: datetime) -> None:
    """Terminalize expired/null-lease claims without ever rerunning their side effect."""
    with engine.connect() as conn:
        expired_runs = conn.execute(
            sa.select(
                ne_job_runs.c.run_id,
                ne_job_runs.c.job_id,
                ne_scheduled_jobs.c.name,
                ne_job_requests.c.request_id,
                ne_job_requests.c.requester_id,
            )
            .select_from(
                ne_job_runs.outerjoin(
                    ne_scheduled_jobs, ne_scheduled_jobs.c.job_id == ne_job_runs.c.job_id
                ).outerjoin(ne_job_requests, ne_job_requests.c.run_id == ne_job_runs.c.run_id)
            )
            .where(
                ne_job_runs.c.finished_at.is_(None),
                sa.or_(
                    ne_job_runs.c.lease_expires_at.is_(None),
                    ne_job_runs.c.lease_expires_at <= now,
                ),
            )
        ).all()

    for row in expired_runs:
        run_id = str(row.run_id)
        job_id = str(row.job_id)
        job_name = str(row.name or job_id)
        request_id = None if row.request_id is None else str(row.request_id)
        actor = _SCHEDULER_ACTOR if row.requester_id is None else str(row.requester_id)
        try:
            _terminalize_claim(
                engine,
                request_id=request_id,
                run_id=run_id,
                job_id=job_id,
                job_name=job_name,
                actor=actor,
                now=now,
                error_class="InterruptedJobRun",
                with_audit=True,
            )
        except RuntimeAuditError as exc:
            if request_id is None:
                # Ordinary scheduled runs retain C6: the failed audited transaction
                # rolled back, so this row remains unfinished. Its expired/null lease
                # is already excluded by both claim overlap guards, allowing the next
                # cron window without rewriting history through a no-audit fallback.
                _LOG.error(
                    "scheduled run reconciliation audit failed for job %s; "
                    "the run remains unfinished",
                    job_name,
                )
                continue
            _terminalize_claim(
                engine,
                request_id=request_id,
                run_id=run_id,
                job_id=job_id,
                job_name=job_name,
                actor=actor,
                now=now,
                error_class="InterruptedJobRun",
                with_audit=False,
            )
            raise RuntimeAuditError(
                f"interrupted manual run {run_id!r} was released without terminal audit"
            ) from exc

    with engine.connect() as conn:
        stranded_requests = conn.execute(
            sa.select(ne_job_requests).where(
                sa.or_(
                    sa.and_(
                        ne_job_requests.c.status == "queued",
                        ne_job_requests.c.expires_at <= now,
                    ),
                    sa.and_(
                        ne_job_requests.c.status == "running",
                        sa.or_(
                            ne_job_requests.c.lease_expires_at.is_(None),
                            ne_job_requests.c.lease_expires_at <= now,
                        ),
                    ),
                )
            )
        ).all()

    for request in stranded_requests:
        run_id = str(request.run_id or request.request_id)
        error_class = "ManualRequestExpired" if request.status == "queued" else "InterruptedJobRun"
        try:
            _terminalize_claim(
                engine,
                request_id=str(request.request_id),
                run_id=run_id,
                job_id=str(request.job_id),
                job_name=str(request.job_name),
                actor=str(request.requester_id),
                now=now,
                error_class=error_class,
                with_audit=True,
            )
        except RuntimeAuditError as exc:
            _terminalize_claim(
                engine,
                request_id=str(request.request_id),
                run_id=run_id,
                job_id=str(request.job_id),
                job_name=str(request.job_name),
                actor=str(request.requester_id),
                now=now,
                error_class=error_class,
                with_audit=False,
            )
            raise RuntimeAuditError(
                f"expired request {request.request_id!r} was released without terminal audit"
            ) from exc


@dataclass(frozen=True)
class JobRunOutcome:
    """The outcome of running one due job (returned by :meth:`CronScheduler.run_due_jobs`)."""

    run_id: str
    job_id: str
    name: str
    succeeded: bool
    output: str


ManualJobStatus = Literal["queued", "running", "succeeded", "failed"]

_MANUAL_REQUEST_TTL = timedelta(minutes=5)
# Cron expressions have one-minute granularity.  A killed worker must lose its
# claim before the next window, while a genuinely live worker renews often
# enough to tolerate two missed heartbeats without permitting overlap.
_RUN_LEASE = timedelta(seconds=45)
_LEASE_RENEW_INTERVAL_S = 15.0
_MAX_REQUESTER_GROUPS = 64
_MAX_GROUP_CHARS = 256


@dataclass(frozen=True)
class ManualJobRequest:
    """One bounded request to execute an existing job's sole governed tool."""

    request_id: str
    job_id: str
    name: str
    source: str
    requested_action: str
    tool_name: str
    requester_id: str
    requester_groups: tuple[str, ...]
    config_hash: str
    policy_evidence: str
    created_at: datetime
    expires_at: datetime
    status: ManualJobStatus
    run_id: str | None
    claim_owner: str | None
    lease_expires_at: datetime | None
    succeeded: bool | None
    error_class: str | None
    output: str | None
    created: bool = False


def _request_policy_evidence(
    *,
    source: str,
    requested_action: str,
    tool_name: str,
    principal: Principal,
    config_hash: str,
) -> str:
    """Bind the accepted request facts without storing PDP reason/body text."""
    payload = {
        "source": source,
        "requested_action": requested_action,
        "tool_name": tool_name,
        "requester_id": principal.id,
        "requester_groups": list(principal.groups),
        "config_hash": config_hash,
        "accepted_via": "governed-tool-executor",
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class ManualJobRequestStore:
    """Cross-process request/read seam on the existing scheduler database.

    Per-source dedupe is atomic because the scheduled-job row is locked before
    checking or inserting a request. Consequently the live queue is bounded by
    the finite scheduled-job registry: at most one queued/running request per job.
    """

    def __init__(self, engine: sa.Engine, *, clock: Callable[[], datetime] | None = None) -> None:
        self._engine = engine
        self._clock = clock or (lambda: datetime.now(UTC))
        ensure_scheduler_schema(engine)

    def request(
        self,
        name: str,
        *,
        source: str,
        requested_action: str,
        tool_name: str,
        principal: Principal,
        config_hash: str,
    ) -> ManualJobRequest:
        """Queue once or join the source's existing queued/running execution."""
        self._validate_request_fields(
            source=source,
            requested_action=requested_action,
            tool_name=tool_name,
            principal=principal,
            config_hash=config_hash,
        )
        now = self._clock()
        _reconcile_expired_claims(self._engine, now)
        with self._engine.begin() as conn:
            job = conn.execute(
                sa.select(ne_scheduled_jobs)
                .where(ne_scheduled_jobs.c.name == name)
                .with_for_update()
            ).first()
            if job is None:
                raise SchedulingError(f"no scheduled job named {name!r}")
            granted = tuple(json.loads(job.toolset))
            if granted != (tool_name,):
                raise SchedulingError(
                    f"scheduled job {name!r} does not grant exactly {tool_name!r}"
                )

            existing = conn.execute(
                sa.select(ne_job_requests)
                .where(
                    ne_job_requests.c.job_id == job.job_id,
                    ne_job_requests.c.status.in_(("queued", "running")),
                )
                .order_by(ne_job_requests.c.created_at, ne_job_requests.c.request_id)
                .limit(1)
            ).first()
            if existing is not None:
                return self._view(conn, existing, created=False)

            request_id = str(uuid.uuid4())
            status: ManualJobStatus = "queued"
            evidence = _request_policy_evidence(
                source=source,
                requested_action=requested_action,
                tool_name=tool_name,
                principal=principal,
                config_hash=config_hash,
            )
            try:
                with conn.begin_nested():
                    conn.execute(
                        ne_job_requests.insert().values(
                            request_id=request_id,
                            job_id=job.job_id,
                            job_name=name,
                            source=source,
                            requested_action=requested_action,
                            tool_name=tool_name,
                            requester_id=principal.id,
                            requester_groups=json.dumps(list(principal.groups)),
                            config_hash=config_hash,
                            policy_evidence=evidence,
                            created_at=now,
                            expires_at=now + _MANUAL_REQUEST_TTL,
                            status=status,
                            run_id=None,
                        )
                    )
            except IntegrityError:
                # A concurrent API process may have inserted after our first
                # active-row read (notably on SQLite, where FOR UPDATE is a
                # no-op). The partial unique index is the final overlap guard;
                # serve the winner as the same joined in-progress request.
                concurrent = conn.execute(
                    sa.select(ne_job_requests)
                    .where(
                        ne_job_requests.c.job_id == job.job_id,
                        ne_job_requests.c.status.in_(("queued", "running")),
                    )
                    .order_by(ne_job_requests.c.created_at, ne_job_requests.c.request_id)
                    .limit(1)
                ).first()
                if concurrent is None:
                    raise
                return self._view(conn, concurrent, created=False)
            event = new_audit_event(
                action="job.request",
                resource=f"job:{name}",
                outcome=AuditOutcome.success,
                actor=principal.id,
                detail=json.dumps(
                    {
                        "name": name,
                        "request_id": request_id,
                        "source": source,
                        "status": status,
                        "expires_at": (now + _MANUAL_REQUEST_TTL).isoformat(),
                        "config_hash": config_hash,
                        "policy_evidence": evidence,
                    }
                ),
                provenance=dict(_PROVENANCE_SOURCE),
            )
            append(conn, event)
            _chain_append(conn, event)
            row = conn.execute(
                sa.select(ne_job_requests).where(ne_job_requests.c.request_id == request_id)
            ).one()
            return self._view(conn, row, created=True)

    def get(self, request_id: str) -> ManualJobRequest | None:
        """Read one opaque request and its counts-only terminal output."""
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(ne_job_requests).where(ne_job_requests.c.request_id == request_id)
            ).first()
            if row is None:
                return None
            return self._view(conn, row, created=False)

    @staticmethod
    def _validate_request_fields(
        *,
        source: str,
        requested_action: str,
        tool_name: str,
        principal: Principal,
        config_hash: str,
    ) -> None:
        if not source or len(source) > 128:
            raise SchedulingError("manual job source must be 1..128 characters")
        if not requested_action or len(requested_action) > 256:
            raise SchedulingError("manual job action must be 1..256 characters")
        if not tool_name or len(tool_name) > 256:
            raise SchedulingError("manual job tool name must be 1..256 characters")
        if not principal.id or len(principal.id) > 512:
            raise SchedulingError("manual job requester id must be 1..512 characters")
        if len(principal.groups) > _MAX_REQUESTER_GROUPS or any(
            len(group) > _MAX_GROUP_CHARS for group in principal.groups
        ):
            raise SchedulingError("manual job requester groups exceed the bounded policy context")
        if not config_hash or len(config_hash) > 128:
            raise SchedulingError("manual job config hash must be 1..128 characters")

    @staticmethod
    def _view(
        conn: sa.Connection,
        row: sa.Row[Any],
        *,
        created: bool,
    ) -> ManualJobRequest:
        output: str | None = None
        if row.run_id is not None:
            output = conn.execute(
                sa.select(ne_job_runs.c.output).where(ne_job_runs.c.run_id == row.run_id)
            ).scalar_one_or_none()
        groups = tuple(str(group) for group in json.loads(row.requester_groups))
        return ManualJobRequest(
            request_id=str(row.request_id),
            job_id=str(row.job_id),
            name=str(row.job_name),
            source=str(row.source),
            requested_action=str(row.requested_action),
            tool_name=str(row.tool_name),
            requester_id=str(row.requester_id),
            requester_groups=groups,
            config_hash=str(row.config_hash),
            policy_evidence=str(row.policy_evidence),
            created_at=row.created_at,
            expires_at=row.expires_at,
            status=str(row.status),  # type: ignore[arg-type]
            run_id=None if row.run_id is None else str(row.run_id),
            claim_owner=None if row.claim_owner is None else str(row.claim_owner),
            lease_expires_at=row.lease_expires_at,
            succeeded=row.succeeded,
            error_class=None if row.error_class is None else str(row.error_class),
            output=output,
            created=created,
        )


class _ToolErrorTrackingExecutor:
    """Scheduler-local view that remembers audited handler failures.

    The agent loop deliberately converts a ``ToolExecutionError`` into a safe
    ``BLOCKED`` tool result so the model can terminate coherently.  That model
    behavior must not turn the durable scheduler terminal green: a job whose
    handler errored failed, even if the model subsequently returned prose.
    Governance refusals remain recoverable and are not tracked here.
    """

    def __init__(self, inner: ToolsetScopedExecutor) -> None:
        self._inner = inner
        self.error_class: str | None = None

    @property
    def tool_specs(self) -> tuple[ToolSpec, ...]:
        return self._inner.tool_specs

    async def execute(self, tool: str, args: Mapping[str, Any], *, principal: Principal) -> str:
        try:
            return await self._inner.execute(tool, args, principal=principal)
        except ToolExecutionError as exc:
            self.error_class = type(exc).__name__
            raise


class CronScheduler:
    """Cron scheduler with server-owned agent or governed sole-tool execution."""

    def __init__(
        self,
        *,
        engine: sa.Engine,
        definitions: Mapping[str, AgentDefinition],
        executor: GovernedToolExecutor,
        pdp: PolicyDecisionPoint,
        gateway: ModelGateway,
        registry: ProcessRegistry,
        sink: AuditSink,
        settings: RuntimeSettings,
        principals: AgentPrincipalProvider,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if sink is None:
            raise TypeError("CronScheduler requires a non-None audit sink (C6)")
        if pdp is None:
            raise TypeError("CronScheduler requires a non-None pdp (C6)")
        ensure_transactional_sqlite(engine)
        self._engine = engine
        self._definitions = dict(definitions)
        self._executor = executor
        self._pdp = pdp
        self._gateway = gateway
        self._registry = registry
        self._sink = sink
        self._settings = settings
        self._principals = principals
        self._clock = clock or (lambda: datetime.now(UTC))

    # -- sync registration (frozen Scheduler protocol) ----------------------

    def schedule(self, job: ScheduledJob) -> str:
        """Validate + persist a job AND dual-append its ``job.schedule`` event atomically.

        The SAME ``job.schedule`` event is written to BOTH the ne-events outbox and the
        tamper-evident ``ne_audit_log`` hash chain on ONE transaction (Checkpoint-D
        finding #4), so a chain-append failure rolls the whole job insert back.

        Fail loud (nothing persisted, no event) on: an invalid cron expression, an
        unknown agent type, or a toolset naming a tool the executor does not expose.
        A duplicate ``name`` raises :class:`SchedulingError` (the row's UNIQUE
        constraint), leaving no partial state.
        """
        now = self._clock()
        try:
            next_run_at = next(CronSim(job.cron, now))
        except CronSimError as exc:
            raise SchedulingError(f"invalid cron expression {job.cron!r}: {exc}") from exc

        if job.agent not in self._definitions:
            raise SchedulingError(f"unknown agent type {job.agent!r} for job {job.name!r}")
        definition = self._definitions[job.agent]

        if definition.scheduled_execution == "sole-tool" and (
            len(job.toolset) != 1 or tuple(job.toolset) != definition.toolset
        ):
            raise SchedulingError(
                f"sole-tool job {job.name!r} must grant exactly its definition's one tool"
            )

        available = {s.name for s in self._executor.tool_specs}
        missing = [name for name in job.toolset if name not in available]
        if missing:
            raise SchedulingError(
                f"job {job.name!r} grants unregistered tools {missing!r}; a job may not "
                "silently lose tools (fail loud)"
            )

        job_id = str(uuid.uuid4())
        event = new_audit_event(
            action="job.schedule",
            resource=f"job:{job.name}",
            outcome=AuditOutcome.success,
            actor=_SCHEDULER_ACTOR,
            detail=json.dumps(
                {
                    "name": job.name,
                    "cron": job.cron,
                    "agent": job.agent,
                    "toolset": list(job.toolset),
                }
            ),
            provenance=dict(_PROVENANCE_SOURCE),
        )
        try:
            with self._engine.begin() as conn:
                conn.execute(
                    ne_scheduled_jobs.insert().values(
                        job_id=job_id,
                        name=job.name,
                        cron=job.cron,
                        agent=job.agent,
                        toolset=json.dumps(list(job.toolset)),
                        created_at=now,
                        next_run_at=next_run_at,
                    )
                )
                append(conn, event)
                _chain_append(conn, event)
        except IntegrityError as exc:
            raise SchedulingError(f"a job named {job.name!r} already exists") from exc
        return job_id

    def cancel(self, job_id: str) -> None:
        """Delete a job AND dual-append its ``job.cancel`` event atomically (unknown id → error).

        The SAME ``job.cancel`` event lands in BOTH the ne-events outbox and the
        tamper-evident ``ne_audit_log`` hash chain on ONE transaction (Checkpoint-D
        finding #4); a chain-append failure rolls the delete back.
        """
        with self._engine.begin() as conn:
            row = conn.execute(
                sa.select(ne_scheduled_jobs.c.name).where(ne_scheduled_jobs.c.job_id == job_id)
            ).first()
            if row is None:
                raise SchedulingError(f"no scheduled job with id {job_id!r}")
            conn.execute(ne_scheduled_jobs.delete().where(ne_scheduled_jobs.c.job_id == job_id))
            event = new_audit_event(
                action="job.cancel",
                resource=f"job:{row.name}",
                outcome=AuditOutcome.success,
                actor=_SCHEDULER_ACTOR,
                detail=json.dumps({"name": row.name, "job_id": job_id}),
                provenance=dict(_PROVENANCE_SOURCE),
            )
            append(conn, event)
            _chain_append(conn, event)

    def list_jobs(self) -> tuple[ScheduledJob, ...]:
        """All persisted jobs as :class:`ScheduledJob` (with ``job_id`` + toolset set)."""
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.select(ne_scheduled_jobs).order_by(ne_scheduled_jobs.c.created_at)
            ).all()
        return tuple(
            ScheduledJob(
                name=row.name,
                cron=row.cron,
                agent=row.agent,
                toolset=tuple(json.loads(row.toolset)),
                job_id=row.job_id,
            )
            for row in rows
        )

    # -- async due-job execution -------------------------------------------

    async def run_due_jobs(self, *, now: datetime | None = None) -> tuple[JobRunOutcome, ...]:
        """Run every due job through its server-owned governed execution mode.

        Each due job is CLAIMED by a compare-and-set advance of ``next_run_at`` to the
        next match after ``now`` (missed windows collapse to ONE run); a lost race
        (rowcount 0) skips the job. The started/finished ``ne_job_runs`` rows and the
        ``job.run`` event are written transactionally. If terminal audit persistence
        fails, the audited finish rolls back and the scheduled run remains unfinished.
        Once its lease expires it no longer suppresses later cron windows; unlike a
        manual request, scheduled history is never terminalized without audit.
        """
        now = now or self._clock()
        _reconcile_expired_claims(self._engine, now)
        with self._engine.connect() as conn:
            due = conn.execute(
                sa.select(ne_scheduled_jobs).where(ne_scheduled_jobs.c.next_run_at <= now)
            ).all()

        outcomes: list[JobRunOutcome] = []
        for job in due:
            claimed = self._claim(job, now)
            if claimed is None:
                continue  # lost the race to another worker (single-writer CI: never)
            run_id = claimed
            outcome = await self._run_claimed_job(job, run_id, now)
            outcomes.append(outcome)
        return tuple(outcomes)

    async def run_requested_jobs(self, *, now: datetime | None = None) -> tuple[JobRunOutcome, ...]:
        """Run queued manual requests through this scheduler's governed executor.

        Unlike cron-as-agent, the manual path invokes the job's sole granted tool
        exactly once. The request already names the action the user authorized;
        asking a model whether to call it would make Sync now nondeterministic.
        """
        now = now or self._clock()
        _reconcile_expired_claims(self._engine, now)
        with self._engine.connect() as conn:
            requests = conn.execute(
                sa.select(ne_job_requests)
                .where(ne_job_requests.c.status == "queued")
                .order_by(ne_job_requests.c.created_at, ne_job_requests.c.request_id)
            ).all()

        outcomes: list[JobRunOutcome] = []
        for request in requests:
            claimed = self._claim_request(request, now)
            if claimed is None:
                continue
            job, current, run_id = claimed
            outcomes.append(await self._run_claimed_job_direct(job, current, run_id, now))
        return tuple(outcomes)

    def _claim_request(
        self, request: sa.Row[Any], now: datetime
    ) -> tuple[sa.Row[Any], sa.Row[Any], str] | None:
        """Atomically claim one request, joining an execution already in flight."""
        with self._engine.begin() as conn:
            current = conn.execute(
                sa.select(ne_job_requests)
                .where(ne_job_requests.c.request_id == request.request_id)
                .with_for_update()
            ).first()
            if current is None or current.status != "queued":
                return None
            job = conn.execute(
                sa.select(ne_scheduled_jobs)
                .where(ne_scheduled_jobs.c.job_id == current.job_id)
                .with_for_update()
            ).first()
            if job is None:
                run_id = str(current.request_id)
                conn.execute(
                    ne_job_runs.insert().values(
                        run_id=run_id,
                        job_id=current.job_id,
                        started_at=now,
                        finished_at=now,
                        succeeded=False,
                        error_class="SchedulingError",
                        output="ERROR: SchedulingError",
                    )
                )
                conn.execute(
                    ne_job_requests.update()
                    .where(ne_job_requests.c.request_id == current.request_id)
                    .values(
                        status="failed",
                        run_id=run_id,
                        finished_at=now,
                        succeeded=False,
                        error_class="SchedulingError",
                    )
                )
                event = new_audit_event(
                    action="job.run",
                    resource=f"job:{current.job_name}",
                    outcome=AuditOutcome.error,
                    actor=str(current.requester_id),
                    detail=json.dumps({"name": str(current.job_name), "run_id": run_id}),
                    provenance=dict(_PROVENANCE_SOURCE),
                )
                append(conn, event)
                _chain_append(conn, event)
                return None
            active = conn.execute(
                sa.select(ne_job_runs)
                .where(
                    ne_job_runs.c.job_id == job.job_id,
                    ne_job_runs.c.finished_at.is_(None),
                    ne_job_runs.c.lease_expires_at > now,
                )
                .order_by(ne_job_runs.c.started_at, ne_job_runs.c.run_id)
                .limit(1)
                .with_for_update()
            ).first()
            if active is not None:
                # Keep the request queued. The next existing cron tick retries
                # after the active run finishes; the manual request never joins a
                # model-driven cron run that may terminate without calling its tool.
                return None

            run_id = str(current.request_id)
            conn.execute(
                ne_job_runs.insert().values(
                    run_id=run_id,
                    job_id=job.job_id,
                    started_at=now,
                    claim_owner=run_id,
                    lease_expires_at=now + _RUN_LEASE,
                )
            )
            conn.execute(
                ne_job_requests.update()
                .where(ne_job_requests.c.request_id == current.request_id)
                .values(
                    status="running",
                    run_id=run_id,
                    claim_owner=run_id,
                    lease_expires_at=now + _RUN_LEASE,
                )
            )
            return job, current, run_id

    def _claim(self, job: sa.Row[Any], now: datetime) -> str | None:
        """Suppress active overlap, then CAS-advance and insert in ONE transaction.

        Returns the new ``run_id``, or ``None`` if another worker claimed it first
        (the CAS UPDATE matched zero rows) or a live lease still owns the job. An
        active lease does not consume the due window: if that owner dies, its
        sub-minute lease expires before the next cron window and the next tick can
        claim the still-due job. Computing the new ``next_run_at`` from NOW collapses
        missed windows to a single run. Multi-worker TOCTOU validation under real
        concurrent Postgres workers is deferred to NE-070 (single-writer-safe here).
        """
        new_next = next(CronSim(job.cron, now))
        run_id = str(uuid.uuid4())
        with self._engine.begin() as conn:
            locked_job_id = conn.execute(
                sa.select(ne_scheduled_jobs.c.job_id)
                .where(ne_scheduled_jobs.c.job_id == job.job_id)
                .with_for_update()
            ).scalar_one_or_none()
            if locked_job_id is None:
                return None
            active_run_id = conn.execute(
                sa.select(ne_job_runs.c.run_id)
                .where(
                    ne_job_runs.c.job_id == job.job_id,
                    ne_job_runs.c.finished_at.is_(None),
                    ne_job_runs.c.lease_expires_at > now,
                )
                .limit(1)
            ).scalar_one_or_none()
            if active_run_id is not None:
                # Preserve next_run_at so the first tick after a killed owner's
                # lease expires can claim this same due window.
                return None
            result = conn.execute(
                ne_scheduled_jobs.update()
                .where(
                    ne_scheduled_jobs.c.job_id == job.job_id,
                    ne_scheduled_jobs.c.next_run_at == job.next_run_at,
                )
                .values(next_run_at=new_next)
            )
            if result.rowcount != 1:
                return None
            conn.execute(
                ne_job_runs.insert().values(
                    run_id=run_id,
                    job_id=job.job_id,
                    started_at=now,
                    claim_owner=run_id,
                    lease_expires_at=now + _RUN_LEASE,
                )
            )
        return run_id

    async def _renew_lease(self, run_id: str) -> None:
        """Keep one genuinely active claim leased; stop when ownership is lost."""
        while True:
            await asyncio.sleep(_LEASE_RENEW_INTERVAL_S)
            now = self._clock()
            with self._engine.begin() as conn:
                renewed = conn.execute(
                    ne_job_runs.update()
                    .where(
                        ne_job_runs.c.run_id == run_id,
                        ne_job_runs.c.claim_owner == run_id,
                        ne_job_runs.c.finished_at.is_(None),
                    )
                    .values(lease_expires_at=now + _RUN_LEASE)
                )
                if renewed.rowcount != 1:
                    return
                conn.execute(
                    ne_job_requests.update()
                    .where(
                        ne_job_requests.c.run_id == run_id,
                        ne_job_requests.c.claim_owner == run_id,
                        ne_job_requests.c.status == "running",
                    )
                    .values(lease_expires_at=now + _RUN_LEASE)
                )

    def _release_after_audit_failure(
        self,
        job: sa.Row[Any],
        run_id: str,
        actor: str,
        now: datetime,
        error_class: str,
    ) -> None:
        """Release a manual claim without another audit dependency after audit failure."""
        _terminalize_claim(
            self._engine,
            request_id=None,
            run_id=run_id,
            job_id=str(job.job_id),
            job_name=str(job.name),
            actor=actor,
            now=now,
            error_class=error_class,
            with_audit=False,
        )

    async def _run_claimed_job_direct(
        self,
        job: sa.Row[Any],
        request: sa.Row[Any],
        run_id: str,
        now: datetime,
    ) -> JobRunOutcome:
        """Execute one request under its original principal and policy binding."""
        succeeded = False
        error_class: str | None = None
        output = ""
        principal = Principal(
            id=str(request.requester_id),
            groups=tuple(str(group) for group in json.loads(request.requester_groups)),
        )
        actor = principal.id
        heartbeat = asyncio.create_task(self._renew_lease(run_id))
        try:
            try:
                if _to_aware(request.expires_at) <= _to_aware(now):
                    raise SchedulingError("manual job request expired before execution")
                if str(request.config_hash) != get_config_hash():
                    raise SchedulingError("manual job request config hash no longer matches")
                expected_evidence = _request_policy_evidence(
                    source=str(request.source),
                    requested_action=str(request.requested_action),
                    tool_name=str(request.tool_name),
                    principal=principal,
                    config_hash=str(request.config_hash),
                )
                if expected_evidence != str(request.policy_evidence):
                    raise SchedulingError("manual job request policy evidence is invalid")
                granted = tuple(json.loads(job.toolset))
                if granted != (str(request.tool_name),):
                    raise SchedulingError("manual job tool grant changed after acceptance")

                # This execute performs a fresh PDP/guardrail decision using the
                # original requester. Revocation therefore fails closed here.
                output = await self._execute_sole_tool(
                    job,
                    principal=principal,
                    granted=granted,
                    scope=f"manual:{job.name}",
                )
                succeeded = True
            except (RuntimeAuditError, AuditEmissionError) as exc:
                self._release_after_audit_failure(job, run_id, actor, now, type(exc).__name__)
                raise
            except Exception as exc:
                error_class = type(exc).__name__
                output = f"ERROR: {type(exc).__name__}"
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat

        self._finish_run(
            job,
            run_id,
            actor,
            succeeded,
            error_class,
            output,
            now,
            release_manual_claim_on_audit_failure=True,
        )
        return JobRunOutcome(
            run_id=run_id,
            job_id=job.job_id,
            name=job.name,
            succeeded=succeeded,
            output=output,
        )

    async def _execute_sole_tool(
        self,
        job: sa.Row[Any],
        *,
        principal: Principal,
        granted: tuple[str, ...],
        scope: str,
    ) -> str:
        """Invoke one exact governed tool without model arbitration."""
        if len(granted) != 1:
            raise SchedulingError("sole-tool execution requires exactly one granted tool")
        scoped = scoped_tool_executor(self._executor, granted=granted, scope=scope)
        record = self._registry.register(kind="cron-job", name=job.name, principal_id=principal.id)
        succeeded = False
        try:
            output = await scoped.execute(granted[0], {}, principal=principal)
            succeeded = True
            return output
        finally:
            self._registry.finish(record.process_id, succeeded=succeeded)

    async def _run_claimed_scheduled_sole_tool(
        self,
        job: sa.Row[Any],
        run_id: str,
        now: datetime,
        *,
        definition: AgentDefinition,
    ) -> JobRunOutcome:
        """Run an explicitly opted-in scheduled job's sole governed tool once."""
        actor = _SCHEDULER_ACTOR
        succeeded = False
        error_class: str | None = None
        output = ""
        heartbeat = asyncio.create_task(self._renew_lease(run_id))
        try:
            try:
                granted = tuple(str(name) for name in json.loads(job.toolset))
                if len(definition.toolset) != 1 or granted != definition.toolset:
                    raise SchedulingError(
                        f"sole-tool job {job.name!r} no longer matches its current definition"
                    )
                principal = self._principals.principal_for(job.agent)
                actor = principal.id
                output = await self._execute_sole_tool(
                    job,
                    principal=principal,
                    granted=granted,
                    scope=f"cron:{job.name}",
                )
                succeeded = True
            except (RuntimeAuditError, AuditEmissionError):
                # Scheduled execution retains the established atomic audit contract:
                # the run stays unfinished when audit emission fails.
                raise
            except Exception as exc:
                error_class = type(exc).__name__
                output = f"ERROR: {type(exc).__name__}"
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat

        self._finish_run(job, run_id, actor, succeeded, error_class, output, now)
        return JobRunOutcome(
            run_id=run_id,
            job_id=job.job_id,
            name=job.name,
            succeeded=succeeded,
            output=output,
        )

    async def _run_claimed_job(self, job: sa.Row[Any], run_id: str, now: datetime) -> JobRunOutcome:
        """Run a claimed job in isolation, then finish its run row + ``job.run`` event."""
        definition = self._definitions.get(str(job.agent))
        if definition is not None and definition.scheduled_execution == "sole-tool":
            return await self._run_claimed_scheduled_sole_tool(
                job,
                run_id,
                now,
                definition=definition,
            )

        actor = _SCHEDULER_ACTOR
        succeeded = False
        error_class: str | None = None
        output = ""
        record = None
        heartbeat = asyncio.create_task(self._renew_lease(run_id))
        try:
            try:
                principal = self._principals.principal_for(job.agent)
                actor = principal.id
                definition = self._definitions[job.agent]
                scoped = ToolsetScopedExecutor(
                    self._executor,
                    granted=tuple(json.loads(job.toolset)),
                    sink=self._sink,
                    scope=f"cron:{job.name}",
                    pdp=self._pdp,
                )
                tracked = _ToolErrorTrackingExecutor(scoped)
                record = self._registry.register(
                    kind="cron-job", name=job.name, principal_id=principal.id
                )
                prompt = f"{definition.instructions}\n\nScheduled task: {job.name}"
                try:
                    output = await run_isolated_agent(
                        task=prompt,
                        principal=principal,
                        executor=tracked,
                        gateway=self._gateway,
                        settings=self._settings,
                    )
                    if tracked.error_class is None:
                        succeeded = True
                    else:
                        error_class = tracked.error_class
                        output = f"ERROR: {tracked.error_class}"
                finally:
                    self._registry.finish(record.process_id, succeeded=succeeded)
            except (RuntimeAuditError, AuditEmissionError):
                # Ordinary scheduled execution preserves the established atomic
                # audit contract: the run stays unfinished when audit emission fails.
                raise
            except LookupError as exc:
                # No provisioned identity for the agent — fail the run, audited as scheduler.
                error_class = type(exc).__name__
                output = f"ERROR: {type(exc).__name__}"
            except Exception as exc:
                error_class = type(exc).__name__
                output = f"ERROR: {type(exc).__name__}"
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat

        self._finish_run(job, run_id, actor, succeeded, error_class, output, now)
        return JobRunOutcome(
            run_id=run_id, job_id=job.job_id, name=job.name, succeeded=succeeded, output=output
        )

    def _finish_run(
        self,
        job: sa.Row[Any],
        run_id: str,
        actor: str,
        succeeded: bool,
        error_class: str | None,
        output: str,
        now: datetime,
        *,
        release_manual_claim_on_audit_failure: bool = False,
    ) -> None:
        """UPDATE the run row + dual-append the ``job.run`` event in ONE transaction (fail closed).

        The SAME ``job.run`` event lands in BOTH the ne-events outbox and the
        tamper-evident ``ne_audit_log`` hash chain (Checkpoint-D finding #4). A chain-
        OR outbox-append failure rolls the audited transaction back. A separate
        Manual requests additionally need their short lease released without relying
        on the failed audit path, so only their caller opts into a metadata-only
        terminal fallback. Ordinary scheduled runs retain the established unfinished
        state when the audited transaction rolls back.
        """
        event = new_audit_event(
            action="job.run",
            resource=f"job:{job.name}",
            outcome=AuditOutcome.success if succeeded else AuditOutcome.error,
            actor=actor,
            detail=json.dumps({"name": job.name, "run_id": run_id}),
            provenance=dict(_PROVENANCE_SOURCE),
        )
        try:
            with self._engine.begin() as conn:
                finished = conn.execute(
                    ne_job_runs.update()
                    .where(
                        ne_job_runs.c.run_id == run_id,
                        ne_job_runs.c.claim_owner == run_id,
                        ne_job_runs.c.finished_at.is_(None),
                    )
                    .values(
                        finished_at=now,
                        succeeded=succeeded,
                        error_class=error_class,
                        output=output,
                        lease_expires_at=now,
                    )
                )
                if finished.rowcount != 1:
                    return
                conn.execute(
                    ne_job_requests.update()
                    .where(
                        ne_job_requests.c.run_id == run_id,
                        ne_job_requests.c.claim_owner == run_id,
                        ne_job_requests.c.status == "running",
                    )
                    .values(
                        status="succeeded" if succeeded else "failed",
                        finished_at=now,
                        succeeded=succeeded,
                        error_class=error_class,
                        lease_expires_at=now,
                    )
                )
                append(conn, event)
                # Dual-append RAW (not _chain_append): this method's own
                # ``except Exception → RuntimeAuditError`` wrapper already fail-closes
                # a chain-append failure — avoid double-wrapping.
                append_record(conn, event)
        except Exception as exc:
            if release_manual_claim_on_audit_failure:
                self._release_after_audit_failure(job, run_id, actor, now, "RuntimeAuditError")
                disposition = "the manual claim was terminally released"
            else:
                disposition = "the scheduled run remains unfinished"
            raise RuntimeAuditError(
                f"finish-audit append failed for run {run_id!r}; {disposition} (C6 fail closed)"
            ) from exc
