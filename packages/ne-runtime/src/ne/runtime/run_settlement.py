# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Administrative settlement for canonical Runs stranded without a terminal.

Two operator-invoked repairs, both of which only make already-recorded truth
VISIBLE — neither invents an outcome, revives an authorization, or executes
anything:

1. :func:`drain_parked_approval_audit_outbox` — the BACKFILL for parks that
   predate the outbox drain.  ``RunApprovalCheckpointStore._create_wait_sync``
   writes ``ne_agent_run_approval_audit_outbox`` at every park; the drain
   (``RunWorkflowService._record_parked_approval_receipt``) runs only in the
   ``except RunApprovalWaiting`` leg of a NEW park, so a Run parked before the
   drain existed has an unclaimed outbox row, no ``decision_kind='approval'``
   Run audit receipt — and ``RunFinalizer._observed_approvals`` therefore
   refuses to compose ANY terminal for it, ever ("approval join lacks its
   exact mandatory audit receipt").  Approve, Deny, expiry and source
   withholding all dead-end on that check.  The backfill records the mandatory
   receipt for the park that really happened (the outbox row is the park's own
   durable intent), says in the receipt that it was recorded by backfill, and
   only then marks the intent claimed — the same crash-safe ordering as the
   live drain.

2. :func:`settle_stranded_runs` — the SWEEPER for runs stuck in ``running``
   with no live worker.  It composes an honest FAILED terminal through the
   ordinary atomic finalizer (never a hand-written status flip): the published
   reason says the run was settled administratively, and a run whose record
   shows an issued governed effect — or a resumed approval continuation, whose
   one pinned effect may have executed — is classed
   ``RunEffectSettlementUncertain`` rather than pretending either success or
   clean failure.  A run that is merely parked (``waiting_approval``) is NOT
   touched: its terminal belongs to a real decision or to the approval-expiry
   workflow, both of which work once the backfill above has run.

Both functions take their composed dependencies explicitly (the same objects
``build_platform`` produces) so they run identically under the test harness
and the operator CLI (``python -m ne.worker.settle``).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import sqlalchemy as sa

from ne.events import new_audit_event
from ne.runtime.run_audit_receipts import RunAuditReceiptSink, RunAuditTarget
from ne.runtime.run_checkpoint import (
    ne_agent_run_approval_audit_outbox,
    ne_agent_run_continuation_checkpoints,
)
from ne.runtime.run_finalizer import (
    RunFinalizer,
    RunUnavailableTerminalCandidate,
)
from ne.runtime.run_store import (
    AgentRunStore,
    RunAuditDecisionKind,
    RunState,
    ne_agent_run_audit_receipts,
    ne_agent_runs,
)
from ne.runtime.run_workflow import (
    RunWorkflowService,
    RunWorkflowStateStore,
    _canonical,
    _database_now,
    _digest,
    _utc,
    ne_agent_run_execution_checkpoints,
    ne_agent_run_workflow_claims,
    ne_agent_run_workflow_leases,
)
from ne.schema import AuditOutcome

__all__ = [
    "DrainedApprovalIntent",
    "StrandedRunSettlement",
    "drain_parked_approval_audit_outbox",
    "settle_stranded_runs",
]

#: The failure class for a stranded run whose record shows an issued governed
#: effect (or a resumed approval continuation, whose pinned effect may have
#: executed).  The SAME class the live recovery path publishes for the same
#: uncertainty, so readers learn one name for one situation.
EFFECT_UNCERTAIN_CLASS = "RunEffectSettlementUncertain"
EFFECT_UNCERTAIN_REASON = (
    "Settled administratively: the run was stranded in running with no terminal "
    "state after a governed effect was issued; whether that effect completed "
    "cannot be determined from the record."
)

#: The failure class for a stranded run with NO recorded effect issue: its
#: worker abandoned it before any external consequence.
STRANDED_CLASS = "RunStrandedNoTerminal"
STRANDED_REASON = (
    "Settled administratively: the run was stranded in running with no terminal "
    "state and no record of an issued governed effect; its worker never "
    "finalized the attempt."
)


@dataclass(frozen=True, slots=True)
class DrainedApprovalIntent:
    """One outbox row's settlement: what existed, and what this pass did."""

    intent_id: str
    approval_id: str
    run_id: str
    attempt_id: str
    #: ``receipt_recorded`` — the mandatory receipt was written by this pass;
    #: ``already_recorded`` — a receipt existed (a crash between the live
    #: drain's append and its claim), only the intent was marked claimed;
    #: ``would_record`` — dry run.
    action: str


@dataclass(frozen=True, slots=True)
class StrandedRunSettlement:
    """One candidate run's settlement outcome (or the reason it was skipped)."""

    run_id: str
    attempt_id: str
    #: ``settled`` | ``would_settle`` (dry run) | ``skipped:<why>`` |
    #: ``settled_unpublished:<why>`` (terminal committed, audit publication
    #: still pending in the lifecycle outbox) | ``error:<class>``.
    disposition: str
    failure_class: str | None = None


@dataclass(frozen=True, slots=True)
class _AttemptRef:
    """The two fields ``_approval_finalizer_context`` actually reads."""

    run_id: str
    attempt_id: str


async def drain_parked_approval_audit_outbox(
    *,
    engine: sa.Engine,
    run_store: AgentRunStore,
    sink: Any,
    audit_engine: sa.Engine,
    run_id: str | None = None,
    limit: int = 100,
    dry_run: bool = False,
) -> tuple[DrainedApprovalIntent, ...]:
    """Backfill the mandatory approval audit receipt for undrained parks.

    Every row processed here was written by a REAL park inside
    ``create_wait``'s own transaction — the receipt records that park, nothing
    else, and the event detail says it was recorded by backfill.  The receipt
    is written under the RUN's accepted ``config_hash`` (from its durable
    workflow claim), because the finalizer refuses any receipt whose hash
    differs from the accepted Run — the currently deployed hash is irrelevant
    to a run admitted under an older one.
    """

    if limit < 1 or limit > 1000:
        raise ValueError("approval outbox drain limit must be between 1 and 1000")
    with engine.connect() as conn:
        query = (
            sa.select(ne_agent_run_approval_audit_outbox)
            .where(ne_agent_run_approval_audit_outbox.c.claimed_at.is_(None))
            .order_by(ne_agent_run_approval_audit_outbox.c.created_at)
            .limit(limit)
        )
        if run_id is not None:
            query = query.where(ne_agent_run_approval_audit_outbox.c.run_id == run_id)
        pending = conn.execute(query).all()
    drained: list[DrainedApprovalIntent] = []
    for row in pending:
        intent_id = str(row.intent_id)
        approval_id = str(row.approval_id)
        intent_run_id = str(row.run_id)
        attempt_id = str(row.attempt_id)
        with engine.connect() as conn:
            claim = conn.execute(
                sa.select(
                    ne_agent_run_workflow_claims.c.config_hash,
                    ne_agent_run_workflow_claims.c.owner_principal_id,
                ).where(
                    ne_agent_run_workflow_claims.c.run_id == intent_run_id,
                    ne_agent_run_workflow_claims.c.attempt_id == attempt_id,
                )
            ).one()
            existing = conn.execute(
                sa.select(sa.func.count())
                .select_from(ne_agent_run_audit_receipts)
                .where(
                    ne_agent_run_audit_receipts.c.run_id == intent_run_id,
                    ne_agent_run_audit_receipts.c.attempt_id == attempt_id,
                    ne_agent_run_audit_receipts.c.approval_id == approval_id,
                    ne_agent_run_audit_receipts.c.decision_kind
                    == RunAuditDecisionKind.APPROVAL.value,
                )
            ).scalar_one()
        if dry_run:
            drained.append(
                DrainedApprovalIntent(
                    intent_id=intent_id,
                    approval_id=approval_id,
                    run_id=intent_run_id,
                    attempt_id=attempt_id,
                    action="already_recorded" if int(existing) else "would_record",
                )
            )
            continue
        if int(existing) == 0:
            config_hash = str(claim.config_hash)
            audit = RunAuditReceiptSink(
                sink,
                audit_engine=audit_engine,
                runtime_engine=engine,
                run_store=run_store,
                target=RunAuditTarget(
                    run_id=intent_run_id,
                    attempt_id=attempt_id,
                    step_id="run-approval",
                    decision_kind=RunAuditDecisionKind.APPROVAL,
                    approval_id=approval_id,
                    # Ordinal 0 can never collide with a live-drain receipt:
                    # the park path uses ``claim.lease_epoch`` (>= 1).  The
                    # address is deterministic, so a crash between append and
                    # claim retries into the same idempotent intent.
                    event_ordinal=0,
                ),
                config_hash=config_hash,
            )
            await audit.append(
                new_audit_event(
                    action="run.approval.requested",
                    resource=f"run:{intent_run_id}",
                    outcome=AuditOutcome.success,
                    actor=str(claim.owner_principal_id),
                    detail=(
                        "canonical Run parked for human approval; no effect was issued "
                        "(mandatory receipt recorded by administrative backfill; the "
                        "park predates the outbox drain)"
                    ),
                    config_hash=config_hash,
                )
            )
        with engine.begin() as conn:
            conn.execute(
                ne_agent_run_approval_audit_outbox.update()
                .where(
                    ne_agent_run_approval_audit_outbox.c.intent_id == intent_id,
                    ne_agent_run_approval_audit_outbox.c.claimed_at.is_(None),
                )
                .values(claimed_at=datetime.now(UTC))
            )
        drained.append(
            DrainedApprovalIntent(
                intent_id=intent_id,
                approval_id=approval_id,
                run_id=intent_run_id,
                attempt_id=attempt_id,
                action="already_recorded" if int(existing) else "receipt_recorded",
            )
        )
    return tuple(drained)


async def settle_stranded_runs(
    *,
    state: RunWorkflowStateStore,
    run_store: AgentRunStore,
    sink: Any,
    audit_engine: sa.Engine,
    stale_after: timedelta,
    limit: int = 50,
    dry_run: bool = False,
) -> tuple[StrandedRunSettlement, ...]:
    """Compose honest FAILED terminals for runs abandoned in ``running``.

    A candidate is a ``running`` run whose run row AND workflow lease have both
    been untouched for at least ``stale_after``.  Each settlement additionally
    requires the per-run settlement advisory guard (the same fence a live
    worker holds while an effect settles) and re-checks status under the run
    row lock, so a run that is actually being executed is never touched.

    The terminal goes through :meth:`RunFinalizer.finalize_unavailable_in_transaction`
    — the SOLE canonical terminal writer — with an explicit ``execution_failed``
    reason naming the administrative settlement, and its delivery binding in
    the honest "unknown" state (``delivery=None``): this pass observed nothing
    and says so.  ``waiting_approval`` runs are deliberately out of scope.
    """

    if stale_after < timedelta(0):
        raise ValueError("stranded-run staleness horizon must not be negative")
    if limit < 1 or limit > 1000:
        raise ValueError("stranded-run settlement limit must be between 1 and 1000")
    engine = state.engine
    with engine.connect() as conn:
        now = _database_now(conn)
        rows = conn.execute(
            sa.select(
                ne_agent_runs.c.run_id,
                ne_agent_runs.c.updated_at,
                ne_agent_run_workflow_claims.c.attempt_id,
                ne_agent_run_workflow_claims.c.config_hash,
                ne_agent_run_workflow_leases.c.updated_at.label("lease_updated_at"),
            )
            .select_from(
                ne_agent_runs.join(
                    ne_agent_run_workflow_claims,
                    ne_agent_run_workflow_claims.c.run_id == ne_agent_runs.c.run_id,
                ).join(
                    ne_agent_run_workflow_leases,
                    sa.and_(
                        ne_agent_run_workflow_leases.c.run_id == ne_agent_runs.c.run_id,
                        ne_agent_run_workflow_leases.c.attempt_id
                        == ne_agent_run_workflow_claims.c.attempt_id,
                    ),
                )
            )
            .where(ne_agent_runs.c.status == RunState.RUNNING.value)
            .order_by(ne_agent_runs.c.created_at)
        ).all()
    horizon = now - stale_after
    settlements: list[StrandedRunSettlement] = []
    for row in rows:
        if len(settlements) >= limit:
            break
        run_id = str(row.run_id)
        attempt_id = str(row.attempt_id)
        last_touched = max(_utc(row.updated_at), _utc(row.lease_updated_at))
        if last_touched > horizon:
            continue
        try:
            settlements.append(
                await _settle_one(
                    state=state,
                    run_store=run_store,
                    sink=sink,
                    audit_engine=audit_engine,
                    run_id=run_id,
                    attempt_id=attempt_id,
                    config_hash=str(row.config_hash),
                    horizon=horizon,
                    dry_run=dry_run,
                )
            )
        except Exception as exc:
            settlements.append(
                StrandedRunSettlement(
                    run_id=run_id,
                    attempt_id=attempt_id,
                    disposition=f"error:{type(exc).__name__}",
                )
            )
    return tuple(settlements)


async def _settle_one(
    *,
    state: RunWorkflowStateStore,
    run_store: AgentRunStore,
    sink: Any,
    audit_engine: sa.Engine,
    run_id: str,
    attempt_id: str,
    config_hash: str,
    horizon: datetime,
    dry_run: bool,
) -> StrandedRunSettlement:
    engine = state.engine
    with engine.connect() as conn:
        effect_issued = (
            int(
                conn.execute(
                    sa.select(sa.func.count())
                    .select_from(ne_agent_run_execution_checkpoints)
                    .where(
                        ne_agent_run_execution_checkpoints.c.run_id == run_id,
                        ne_agent_run_execution_checkpoints.c.attempt_id == attempt_id,
                        ne_agent_run_execution_checkpoints.c.stage.like("effect_issued:%"),
                    )
                ).scalar_one()
            )
            > 0
        )
        resumed_continuation = (
            int(
                conn.execute(
                    sa.select(sa.func.count())
                    .select_from(ne_agent_run_continuation_checkpoints)
                    .where(
                        ne_agent_run_continuation_checkpoints.c.run_id == run_id,
                        ne_agent_run_continuation_checkpoints.c.status == "resumed",
                    )
                ).scalar_one()
            )
            > 0
        )
    uncertain = effect_issued or resumed_continuation
    failure_class = EFFECT_UNCERTAIN_CLASS if uncertain else STRANDED_CLASS
    reason = EFFECT_UNCERTAIN_REASON if uncertain else STRANDED_REASON
    if dry_run:
        return StrandedRunSettlement(
            run_id=run_id,
            attempt_id=attempt_id,
            disposition="would_settle",
            failure_class=failure_class,
        )
    # Any undrained park intent must be backfilled first, or the finalizer's
    # mandatory-approval-receipt check refuses this run's terminal too.
    await drain_parked_approval_audit_outbox(
        engine=engine,
        run_store=run_store,
        sink=sink,
        audit_engine=audit_engine,
        run_id=run_id,
    )
    guard = state._acquire_settlement_guard(run_id, blocking=False)
    if guard is None:
        return StrandedRunSettlement(
            run_id=run_id, attempt_id=attempt_id, disposition="skipped:active-worker"
        )
    with guard, engine.begin() as conn:
        now = _database_now(conn)
        run = conn.execute(
            sa.select(ne_agent_runs.c.status, ne_agent_runs.c.updated_at)
            .where(ne_agent_runs.c.run_id == run_id)
            .with_for_update()
        ).one()
        if str(run.status) != RunState.RUNNING.value:
            return StrandedRunSettlement(
                run_id=run_id,
                attempt_id=attempt_id,
                disposition="skipped:already-terminal",
            )
        if _utc(run.updated_at) > horizon:
            return StrandedRunSettlement(
                run_id=run_id, attempt_id=attempt_id, disposition="skipped:recently-active"
            )
        recorded = conn.execute(
            sa.select(ne_agent_run_execution_checkpoints.c.checkpoint_id).where(
                ne_agent_run_execution_checkpoints.c.run_id == run_id,
                ne_agent_run_execution_checkpoints.c.attempt_id == attempt_id,
                ne_agent_run_execution_checkpoints.c.stage == "execution_failed",
            )
        ).one_or_none()
        if recorded is None:
            safe_detail = {"error_class": failure_class, "error_reason": reason}
            conn.execute(
                ne_agent_run_execution_checkpoints.insert().values(
                    checkpoint_id=str(uuid.uuid4()),
                    run_id=run_id,
                    attempt_id=attempt_id,
                    stage="execution_failed",
                    intent_hash=_digest({"stage": "execution_failed", "safe_detail": safe_detail}),
                    safe_detail=_canonical(safe_detail),
                    created_at=now,
                )
            )
        target, lease = state._approval_finalizer_context(
            conn, joined=_AttemptRef(run_id=run_id, attempt_id=attempt_id), now=now
        )
        receipt = RunFinalizer(run_store).finalize_unavailable_in_transaction(
            conn,
            RunUnavailableTerminalCandidate(
                target=target,
                status="failed",
                outcome="failed",
                failure_class=failure_class,
                delivery=None,
            ),
            lease=lease,
        )
    try:
        publication = state.get_terminal_publication(receipt.publication_id)
        await _publication_service(
            state=state,
            run_store=run_store,
            sink=sink,
            audit_engine=audit_engine,
            config_hash=config_hash,
        ).publish_terminal(publication)
    except Exception as exc:
        return StrandedRunSettlement(
            run_id=run_id,
            attempt_id=attempt_id,
            disposition=f"settled_unpublished:{type(exc).__name__}",
            failure_class=failure_class,
        )
    return StrandedRunSettlement(
        run_id=run_id,
        attempt_id=attempt_id,
        disposition="settled",
        failure_class=failure_class,
    )


class _SettlementUnused:
    """A loud placeholder for service seams settlement never touches.

    ``RunWorkflowService.publish_terminal`` reads only the state store, the
    audit sink/engine, the run store and the config hash; the gateway,
    executor and adapter exist for execution, which settlement never performs.
    Any attribute access is a bug and raises rather than silently executing.
    """

    def __getattr__(self, name: str) -> Any:
        raise RuntimeError(f"run settlement must never use the execution seam {name!r}")


def _publication_service(
    *,
    state: RunWorkflowStateStore,
    run_store: AgentRunStore,
    sink: Any,
    audit_engine: sa.Engine,
    config_hash: str,
) -> RunWorkflowService:
    """The REAL terminal publication protocol, bound to the RUN's config hash."""

    unused = _SettlementUnused()
    return RunWorkflowService(
        state=state,
        run_store=run_store,
        sink=sink,
        audit_engine=audit_engine,
        gateway=cast(Any, unused),
        executor=unused,
        adapter=cast(Any, unused),
        config_hash=config_hash,
    )
