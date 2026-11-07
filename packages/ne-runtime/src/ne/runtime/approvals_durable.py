# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""DBOS-durable approval lifecycle steps (NE-033-honest pattern).

Module-level, serializable-arg, SYNC functions decorated with the existing
``@ne_step`` / ``@ne_workflow`` wrappers. Under ``durability='off'`` (default,
CI) they are transparent passthroughs; under ``durability='dbos'`` the real
``DBOS.step`` / ``DBOS.workflow`` decorators are applied at DECORATION (import)
time — exactly the semantics of :mod:`ne.runtime.durable`. CI proves both the
passthrough behavior and the fake-DBOS decoration (``test_approvals_durable.py``,
mirroring ``test_durable.py``); the real DBOS-Postgres exercise is LIVE-PARKED
(``test_approvals_dbos_live.py``).

These are the durable *mechanics* only: ``create_approval_request_step`` inserts a
pending row (no audit-sink coupling — the async :class:`DurableApprovalGate` owns
the config_hash-stamped AuditEvent + outbox event); ``poll_approval_step`` reads a
status; ``expire_approval_step`` persists the terminal ``expired`` transition (CAS +
outbox ApprovalEvent + evidence row, one transaction); ``await_approval_workflow``
loops the poll until terminal, then at the deadline calls ``expire_approval_step`` so
expiry is DURABLY persisted before returning (an expired request is never grantable).
``time.sleep`` is the passthrough-mode wait; a real-DBOS composition root may
substitute ``DBOS.sleep`` at its own seam. Steps never call other steps — the expiry
step is invoked from the workflow only (step-in-step is illegal under real DBOS).
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa

from ne.audit import ensure_transactional_sqlite
from ne.audit.engine import create_engine_with_timeouts
from ne.events import append, new_approval_event
from ne.runtime.approval_store import (
    PENDING_STATUSES,
    TERMINAL_STATUSES,
    ensure_approval_schema,
    ne_approval_evidence,
    ne_approval_requests,
)
from ne.runtime.durable import ne_step, ne_workflow
from ne.runtime.run_checkpoint import (
    expire_canonical_run_approval_in_transaction,
    ne_agent_run_approval_joins,
)
from ne.schema import ApprovalDecision


@ne_step(name="approval.create")
def create_approval_request_step(database_url: str, request_json: str) -> str:
    """Insert a pending approval row from a JSON blob; return its request id.

    ``request_json`` = ``{"action", "tier", "principal_id", "payload", "sla_s",
    "expiry_s"}``. A durable, sink-free mirror of :meth:`DurableApprovalGate.submit`.
    """
    data = json.loads(request_json)
    request_id = str(uuid.uuid4())
    now = datetime.now(UTC)
    sla_deadline = now + timedelta(seconds=float(data["sla_s"]))
    expires_at = now + timedelta(seconds=float(data["expiry_s"]))
    engine = create_engine_with_timeouts(database_url)
    ensure_transactional_sqlite(engine)
    try:
        ensure_approval_schema(engine)
        with engine.begin() as conn:
            conn.execute(
                ne_approval_requests.insert().values(
                    request_id=request_id,
                    action=data["action"],
                    tier=int(data["tier"]),
                    principal_id=data["principal_id"],
                    payload=json.dumps(data.get("payload", {})),
                    status="pending",
                    requested_at=now,
                    sla_deadline=sla_deadline,
                    expires_at=expires_at,
                )
            )
    finally:
        engine.dispose()
    return request_id


@ne_step(name="approval.poll")
def poll_approval_step(database_url: str, request_id: str) -> str:
    """Return the current lifecycle status string for ``request_id`` (or 'missing')."""
    engine = create_engine_with_timeouts(database_url)
    ensure_transactional_sqlite(engine)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                sa.select(ne_approval_requests.c.status).where(
                    ne_approval_requests.c.request_id == request_id
                )
            ).first()
    finally:
        engine.dispose()
    return str(row.status) if row is not None else "missing"


@ne_step(name="approval.expire")
def expire_approval_step(database_url: str, request_id: str) -> str:
    """Durably persist the terminal ``expired`` transition; return the final status.

    An atomic compare-and-set (``UPDATE ... WHERE status still pending`` +
    ``rowcount`` check) inside ONE transaction flips the row to ``expired`` and, only
    on the winning update, appends the outbox ``ApprovalEvent(expired)`` and an
    evidence row. If a concurrent approve/reject already landed, the CAS matches zero
    rows and the actual terminal status is returned with NO side effects (expiry can
    never clobber a decision). Returns ``"missing"`` if the request id is unknown.

    The outbox ``ApprovalEvent(expired)`` committed here IS the durable expiry record;
    the sink ``AuditEvent`` remains the async gate's job (steps are sink-free by
    design). ``new_approval_event`` stamps the process config_hash, so the composition
    root MUST have frozen it before this step runs (plan §1.4, non-optional).
    """
    engine = create_engine_with_timeouts(database_url)
    ensure_transactional_sqlite(engine)
    now = datetime.now(UTC)
    try:
        has_run_approval_schema = sa.inspect(engine).has_table(ne_agent_run_approval_joins.name)
        with engine.begin() as conn:
            row = conn.execute(
                sa.select(
                    ne_approval_requests.c.action,
                    ne_approval_requests.c.tier,
                ).where(ne_approval_requests.c.request_id == request_id)
            ).first()
            if row is None:
                return "missing"
            if has_run_approval_schema:
                canonical = expire_canonical_run_approval_in_transaction(
                    conn,
                    approval_id=request_id,
                    decided_at=now,
                    event=new_approval_event(
                        approval_id=request_id,
                        tier=int(row.tier),
                        decision=ApprovalDecision.expired,
                        subject_ref=f"tool:{row.action}",
                        actor="system",
                    ),
                )
                if canonical is not None:
                    return canonical
            result = conn.execute(
                ne_approval_requests.update()
                .where(
                    ne_approval_requests.c.request_id == request_id,
                    ne_approval_requests.c.status.in_(PENDING_STATUSES),
                )
                .values(status="expired", decided_at=now)
            )
            if result.rowcount != 1:
                current = conn.execute(
                    sa.select(ne_approval_requests.c.status).where(
                        ne_approval_requests.c.request_id == request_id
                    )
                ).first()
                return str(current.status) if current is not None else "missing"
            append(
                conn,
                new_approval_event(
                    approval_id=request_id,
                    tier=int(row.tier),
                    decision=ApprovalDecision.expired,
                    subject_ref=f"tool:{row.action}",
                    actor="system",
                ),
            )
            conn.execute(
                ne_approval_evidence.insert().values(
                    request_id=request_id,
                    action=str(row.action),
                    tier=int(row.tier),
                    outcome="expired",
                    approver=None,
                    decided_at=now,
                    clean=False,
                )
            )
    finally:
        engine.dispose()
    return "expired"


@ne_workflow(name="approval.await")
def await_approval_workflow(
    database_url: str, request_id: str, poll_interval_s: float, deadline_iso: str
) -> str:
    """Loop ``poll_approval_step`` until terminal, persisting expiry at the deadline.

    Returns the store's terminal status (including 'source_withheld'). Once the deadline
    passes, ``expire_approval_step`` DURABLY persists status='expired' (CAS + outbox
    ApprovalEvent + evidence) BEFORE returning — the row is never left 'pending' while
    the workflow reports 'expired'. If an approve/reject landed concurrently at the
    deadline, that terminal status is honestly reported instead; 'missing' maps to
    'expired'. The expiry logic lives in its own step, invoked from the WORKFLOW only
    (steps never call steps under real DBOS).
    """
    deadline = datetime.fromisoformat(deadline_iso)
    while True:
        status = poll_approval_step(database_url, request_id)
        # Derived from the store's own terminal set, never a literal tuple: a row a
        # genuine source-authority decision made terminal 'source_withheld' (the
        # run-bound coordinator's owner-context withhold, or the store CAS a future
        # standalone owner-context reconciliation drives — never a reader's denial,
        # U2 ruling) must STOP this loop and be reported as itself. A hardcoded
        # tuple would poll it to the deadline and then relabel it 'expired' — a
        # deadline it never reached.
        if status in TERMINAL_STATUSES:
            return status
        if datetime.now(UTC) >= deadline:
            final = expire_approval_step(database_url, request_id)
            return final if final in TERMINAL_STATUSES else "expired"
        time.sleep(poll_interval_s)
