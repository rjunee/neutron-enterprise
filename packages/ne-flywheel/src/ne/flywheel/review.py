# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ProposalReviewService — the propose-then-approve gate (NE-066).

This is the human-review boundary between "the flywheel SUGGESTS a fix" and "a
change LANDS". It is an APPEND-ONLY, audited state machine over NE-065's typed
:class:`~ne.flywheel.proposals.StoredFixProposal` rows:

    proposed --approve--> approved --apply--> applied
       \\                     /
        \\--reject-->  rejected  <--reject--/

``rejected`` and ``applied`` are TERMINAL. ``proposed -> applied`` is
deliberately ILLEGAL — human approval is a distinct, separately-recorded act, so
a change can never reach "applied" without a human having first approved it.

v1 SCOPE (SPEC §5 row, hard): **propose + TRACKED MANUAL apply ONLY.** There is
no auto-apply and no auto-re-index here — those are v1.1 and are deliberately
absent. :meth:`ProposalReviewService.record_apply` RECORDS a human-performed
apply (WHO / WHEN / ``applied_ref``); it NEVER performs the apply. The system
renders a reviewable diff (:mod:`ne.flywheel.diffs`) and TRACKS apply status; a
human applies through the normal git-PR / OPA-bundle-deploy flow.

Write surface (the structural safety invariant): this unit's COMPLETE write
surface is ``ne_proposal_reviews`` + (on a rejection) ``ne_correction_signals``
+ ``ne_outbox_events`` + the ``ne_audit_log``/``ne_audit_head`` chain. It NEVER
UPDATEs or DELETEs anything — ``ne_fix_proposals`` is never touched (its
``status`` column stays ``"proposed"``; the current review status is a FOLD over
the append-only review log, exactly the "NE-066 records its lifecycle elsewhere"
contract NE-065 wrote into its store). The module issues no SQL UPDATE or DELETE
anywhere — INSERT-only (structurally tested).

Rejection -> signal feedback (loop closed): every reject ATOMICALLY appends a
typed, provenance-carrying ``thumbs_down`` :class:`~ne.schema.CorrectionSignal`
(``subject_ref = "proposal:<id>"``; provenance carries ``rejected_proposal_id`` /
``proposal_lane`` / ``rejection_code`` / ``review_id``) into the NE-064 store +
outbox, in the SAME transaction as the review row and the audit event — a
rejected fix is itself a correction signal ("this proposed fix was wrong"), so
the flywheel learns. The kind is ``thumbs_down`` (honest "explicit negative
feedback on an output" semantics) and DELIBERATELY NOT ``approval_rejection``:
``approval_rejection`` would route every rejection into the POLICY lane via
``lanes.route_cluster``, mis-attributing content/retrieval/schema rejections.
Known v1 wrinkle: these fed-back ``proposal:<id>`` signals may themselves be
clustered by a future curation run into content-lane meta-proposals about
proposals; retuning/consumption of the feedback is explicitly LATER work (per
``lanes.py``) — this unit closes the loop, it does not tune the loop.

Audit (C6, fail-closed): every transition dual-appends a ``proposal.<action>``
:class:`~ne.schema.AuditEvent` to BOTH the ne-events outbox and the hash-chained
``ne_audit_log`` in the SAME transaction as the review row (and any rejection
signal). A chain-append failure raises :class:`ReviewAuditError` and the WHOLE
transition rolls back — nothing persists without its tamper-evident record. This
follows NE-065's ``_chain_append`` pattern verbatim; the known pysqlite
BEGIN-recipe gap (SPEC §11) belongs to NE-CLEAN2 and is NOT patched here (tests
use the conftest BEGIN-recipe engine so the fail-closed assertions are faithful;
production is Postgres).

Concurrency (P2-13 fix): concurrent transitions are serialized by a
``SELECT ... FOR UPDATE`` row lock on the proposal (:func:`_proposal_lock_select`)
taken BEFORE the status fold, inside the SAME transition transaction. A racing
approve/reject on Postgres therefore orders strictly: the loser blocks on the
lock, then folds the winner's COMMITTED status and either raises
:class:`InvalidTransitionError` (e.g. approve-after-reject: ``rejected`` is
terminal) or records a LEGAL follow-on transition with an honest ``from_status``
(e.g. reject-after-approve = ``approved -> rejected``, never a silently-folded
stale ``proposed``). A steward-rejected proposal can therefore NEVER fold to
``approved`` or reach ``applied`` — never a silent double transition. The unique
``(proposal_id, action)`` index stays as belt-and-braces behind the row lock
against same-action duplicates. ``FOR UPDATE`` is a no-op on SQLite (CI/dev-only;
the theoretical race persists there, accepted exactly like the SPEC §11 pysqlite
stance); the live multi-worker proof runs in the env-gated Postgres interleave
tests (pending-live-verification ledger).

Same-engine requirement: :func:`ensure_review_schema` creates the review,
proposal, correction-signal, outbox, AND audit tables on ONE engine — the
composition root MUST hand this SAME engine to the signal store and the audit
sink, or the same-transaction dual-append cannot hold.
"""

from __future__ import annotations

import enum
import json
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import IntegrityError

from ne.audit import append_record, ensure_audit_schema, ensure_transactional_sqlite
from ne.contracts import get_config_hash
from ne.events import append as outbox_append
from ne.events import ensure_outbox_schema, new_audit_event, new_correction_signal
from ne.flywheel.errors import (
    InvalidTransitionError,
    ReviewAuditError,
    ReviewError,
    UnknownProposalError,
)
from ne.flywheel.hygiene import scrub_text
from ne.flywheel.proposals import ensure_proposal_schema, ne_fix_proposals
from ne.flywheel.signals import SignalProvenance
from ne.flywheel.store import SqlAlchemyCorrectionSignalStore, ensure_flywheel_schema
from ne.schema import AuditEvent, AuditOutcome, CorrectionKind

_PROVENANCE_SOURCE = {"source": "ne-flywheel", "component": "review"}


class ProposalStatus(enum.StrEnum):
    """The lifecycle status of a proposal, folded from the review log."""

    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    APPLIED = "applied"


class ReviewAction(enum.StrEnum):
    """A human review action that drives a lifecycle transition."""

    APPROVE = "approve"
    REJECT = "reject"
    APPLY = "apply"


class RejectionCode(enum.StrEnum):
    """Why a proposal was rejected (a typed why-code carried on the feedback signal)."""

    INCORRECT = "incorrect"
    UNSAFE = "unsafe"
    DUPLICATE = "duplicate"
    STALE = "stale"
    OUT_OF_SCOPE = "out_of_scope"
    OTHER = "other"


#: The complete, documented transition table (first/only match per action).
#: ``rejected`` and ``applied`` are terminal; ``proposed -> applied`` is
#: deliberately absent (human approval is a distinct recorded act).
_ALLOWED: Mapping[ReviewAction, Mapping[ProposalStatus, ProposalStatus]] = {
    ReviewAction.APPROVE: {ProposalStatus.PROPOSED: ProposalStatus.APPROVED},
    ReviewAction.REJECT: {
        ProposalStatus.PROPOSED: ProposalStatus.REJECTED,
        ProposalStatus.APPROVED: ProposalStatus.REJECTED,
    },
    ReviewAction.APPLY: {ProposalStatus.APPROVED: ProposalStatus.APPLIED},
}


# Its OWN MetaData — independent of every other table's, so
# ``ensure_review_schema`` stays composable.
review_metadata = sa.MetaData()

ne_proposal_reviews = sa.Table(
    "ne_proposal_reviews",
    review_metadata,
    # BigInteger on Postgres (Identity); Integer on SQLite so it aliases the
    # rowid and autoincrements (SQLite only autoincrements INTEGER PRIMARY KEY).
    sa.Column(
        "id",
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.Identity(),
        primary_key=True,
    ),
    sa.Column("review_id", sa.String, nullable=False, unique=True),
    sa.Column("proposal_id", sa.String, nullable=False),
    sa.Column("action", sa.String, nullable=False),
    sa.Column("from_status", sa.String, nullable=False),
    sa.Column("to_status", sa.String, nullable=False),
    sa.Column("actor", sa.String, nullable=False),
    sa.Column("surface", sa.String, nullable=False),
    sa.Column("session_id", sa.String, nullable=False),
    sa.Column("rejection_code", sa.String, nullable=True),
    sa.Column("note", sa.Text, nullable=True),
    sa.Column("applied_ref", sa.String, nullable=True),
    sa.Column("config_hash", sa.String, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    # DB-level belt-and-braces against double-approve/reject/apply (a given
    # proposal may carry AT MOST one row per action).
    sa.Index(
        "ux_ne_proposal_reviews_proposal_action",
        "proposal_id",
        "action",
        unique=True,
    ),
    sa.Index("ix_ne_proposal_reviews_proposal_id", "proposal_id"),
)


def ensure_review_schema(engine: sa.Engine) -> None:
    """Create the review table + the full same-engine closure it composes with.

    Creates ``ne_proposal_reviews`` plus the proposal, correction-signal,
    ne-events outbox, and ne-audit chain tables on ONE engine, mirroring
    ``ensure_curation_schema``. Idempotent. The composition root MUST hand this
    SAME engine to the signal store and the audit sink, or the same-transaction
    dual-append (review row + rejection signal + audit event) cannot hold.
    """
    review_metadata.create_all(engine)
    ensure_proposal_schema(engine)
    ensure_flywheel_schema(engine)
    ensure_outbox_schema(engine)
    ensure_audit_schema(engine)


def _as_utc(dt: datetime) -> datetime:
    """Normalize a datetime to tz-aware UTC (SQLite reads back naive)."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


class StoredReviewEvent(BaseModel):
    """A frozen, fully-materialized review-log row as stored + read back."""

    model_config = ConfigDict(frozen=True)

    id: int
    review_id: str
    proposal_id: str
    action: str
    from_status: str
    to_status: str
    actor: str
    surface: str
    session_id: str
    rejection_code: str | None
    note: str | None
    applied_ref: str | None
    config_hash: str
    created_at: datetime


def _proposal_lock_select(proposal_id: str) -> sa.Select[Any]:
    """Select the proposal's lane WITH a row lock — P2-13 (lock-before-fold).

    Take this row lock BEFORE folding status so concurrent transitions serialize.
    Renders ``... FOR UPDATE`` on the Postgres dialect (a racing reject/approve
    blocks here until the in-flight transition commits, so the fold below always
    sees the winner's committed latest status). The SQLite dialect SILENTLY OMITS
    ``FOR UPDATE`` (a no-op) — sqlite is CI/dev-only, production is Postgres; this
    known-and-accepted sqlite gap is tracked exactly like the SPEC §11 pysqlite
    BEGIN-recipe stance. Not exported in ``__all__`` (internal).
    """
    return (
        sa.select(ne_fix_proposals.c.lane)
        .where(ne_fix_proposals.c.proposal_id == proposal_id)
        .with_for_update()
    )


def _row_to_stored(row: sa.Row[Any]) -> StoredReviewEvent:
    return StoredReviewEvent(
        id=int(row.id),
        review_id=str(row.review_id),
        proposal_id=str(row.proposal_id),
        action=str(row.action),
        from_status=str(row.from_status),
        to_status=str(row.to_status),
        actor=str(row.actor),
        surface=str(row.surface),
        session_id=str(row.session_id),
        rejection_code=str(row.rejection_code) if row.rejection_code is not None else None,
        note=str(row.note) if row.note is not None else None,
        applied_ref=str(row.applied_ref) if row.applied_ref is not None else None,
        config_hash=str(row.config_hash),
        created_at=_as_utc(row.created_at),
    )


class ProposalReviewService:
    """The append-only, audited propose-then-approve review gate (NE-066).

    Public API mirrors ``SqlAlchemyProposalStore``'s sync style: short-lived
    transactions on the injected engine, no network I/O. Every transition
    resolves the config_hash BEFORE opening its transaction (so
    ``ConfigHashNotFrozenError`` precedes any write) and commits the review row,
    any rejection feedback signal, and the dual-appended audit event in ONE
    transaction.
    """

    def __init__(
        self,
        engine: sa.Engine,
        *,
        signal_store: SqlAlchemyCorrectionSignalStore | None = None,
        max_text_chars: int = 4000,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        ensure_transactional_sqlite(engine)
        self._engine = engine
        self._signal_store = signal_store or SqlAlchemyCorrectionSignalStore(engine)
        self._max_text_chars = max_text_chars
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def engine(self) -> sa.Engine:
        return self._engine

    # -- public transitions -------------------------------------------------

    def approve(
        self,
        proposal_id: str,
        provenance: SignalProvenance,
        *,
        note: str | None = None,
    ) -> StoredReviewEvent:
        """Record a human approval (``proposed -> approved``)."""
        return self._transition(ReviewAction.APPROVE, proposal_id, provenance, note=note)

    def reject(
        self,
        proposal_id: str,
        provenance: SignalProvenance,
        *,
        code: RejectionCode,
        note: str | None = None,
    ) -> StoredReviewEvent:
        """Record a human rejection (``proposed|approved -> rejected``).

        Atomically emits a ``thumbs_down`` correction signal back into the NE-064
        store (the rejection-feedback loop) in the same transaction.
        """
        return self._transition(ReviewAction.REJECT, proposal_id, provenance, code=code, note=note)

    def record_apply(
        self,
        proposal_id: str,
        provenance: SignalProvenance,
        *,
        applied_ref: str,
        note: str | None = None,
        extra_writes: Callable[[sa.Connection, str], None] | None = None,
    ) -> StoredReviewEvent:
        """RECORD a human-performed apply (``approved -> applied``).

        The name says it: this only RECORDS that a human applied the change (WHO
        / WHEN / ``applied_ref`` = the PR URL / commit / OPA-bundle version). It
        NEVER performs the apply — v1 is tracked manual apply only. Raises
        :class:`ReviewError` before any write if ``applied_ref`` is empty.

        ``extra_writes`` (R-20): an optional same-conn hook invoked as
        ``extra_writes(conn, review_id)`` INSIDE the one transition transaction
        (after the review-row insert, before the transition's own audit
        dual-append). It lets an in-platform apply seam (the NE-SKILL-1b
        ``apply-skill`` seam) commit its registry writes + its own audit event
        ATOMICALLY with the ``record_apply`` fold — any exception it raises
        propagates and rolls the WHOLE transition back. NO nested
        ``engine.begin()`` is opened; the hook rides this transaction's ``conn``
        (the curation.py:272-277 one-conn discipline). The caller MUST run its
        registry on the SAME engine as this review service (asserted at the
        seam).
        """
        if not applied_ref.strip():
            raise ReviewError("applied_ref must be a non-empty reference (a human-recorded apply)")
        return self._transition(
            ReviewAction.APPLY,
            proposal_id,
            provenance,
            note=note,
            applied_ref=applied_ref,
            extra_writes=extra_writes,
        )

    # -- public reads -------------------------------------------------------

    def status_of(self, proposal_id: str) -> ProposalStatus:
        """Return the current folded status; ``UnknownProposalError`` if absent.

        A proposal with no review rows is :attr:`ProposalStatus.PROPOSED`.
        """
        with self._engine.connect() as conn:
            if not self._proposal_exists(conn, proposal_id):
                raise UnknownProposalError(
                    f"proposal_id {proposal_id!r} is not in ne_fix_proposals"
                )
            return self._fold_status(conn, proposal_id)

    def read_reviews(
        self,
        *,
        proposal_id: str | None = None,
        after_id: int = 0,
        limit: int = 100,
    ) -> list[StoredReviewEvent]:
        """Cursor-ordered read of the review log: ``id > after_id``, id ASC, capped."""
        stmt = sa.select(ne_proposal_reviews).where(ne_proposal_reviews.c.id > after_id)
        if proposal_id is not None:
            stmt = stmt.where(ne_proposal_reviews.c.proposal_id == proposal_id)
        stmt = stmt.order_by(ne_proposal_reviews.c.id.asc()).limit(limit)
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return [_row_to_stored(row) for row in rows]

    # -- internals ----------------------------------------------------------

    def _proposal_exists(self, conn: sa.Connection, proposal_id: str) -> bool:
        row = conn.execute(
            sa.select(ne_fix_proposals.c.lane).where(ne_fix_proposals.c.proposal_id == proposal_id)
        ).first()
        return row is not None

    def _fold_status(self, conn: sa.Connection, proposal_id: str) -> ProposalStatus:
        last = conn.execute(
            sa.select(ne_proposal_reviews.c.to_status)
            .where(ne_proposal_reviews.c.proposal_id == proposal_id)
            .order_by(ne_proposal_reviews.c.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        return ProposalStatus(last) if last is not None else ProposalStatus.PROPOSED

    def _transition(
        self,
        action: ReviewAction,
        proposal_id: str,
        provenance: SignalProvenance,
        *,
        code: RejectionCode | None = None,
        note: str | None = None,
        applied_ref: str | None = None,
        extra_writes: Callable[[sa.Connection, str], None] | None = None,
    ) -> StoredReviewEvent:
        # Resolve config_hash + clock BEFORE any write (ConfigHashNotFrozenError
        # must precede the transaction).
        config_hash = get_config_hash()
        now = _as_utc(self._clock())

        scrubbed_note = (
            scrub_text(note, max_chars=self._max_text_chars) if note is not None else None
        )
        scrubbed_ref = (
            scrub_text(applied_ref, max_chars=self._max_text_chars)
            if applied_ref is not None
            else None
        )
        review_id = str(uuid.uuid4())

        with self._engine.begin() as conn:
            # P2-13: take the row lock FIRST, before the status fold, so a
            # concurrent transition on Postgres blocks here and the fold below
            # always sees the winner's COMMITTED latest status.
            row = conn.execute(_proposal_lock_select(proposal_id)).first()
            lane = str(row.lane) if row is not None else None
            if lane is None:
                raise UnknownProposalError(
                    f"proposal_id {proposal_id!r} is not in ne_fix_proposals"
                )

            current = self._fold_status(conn, proposal_id)
            target = _ALLOWED[action].get(current)
            if target is None:
                raise InvalidTransitionError(
                    f"cannot {action.value} proposal {proposal_id!r} from status "
                    f"{current.value!r} (illegal transition)"
                )

            self._insert_review(
                conn,
                review_id=review_id,
                proposal_id=proposal_id,
                action=action,
                from_status=current,
                to_status=target,
                provenance=provenance,
                code=code,
                note=scrubbed_note,
                applied_ref=scrubbed_ref,
                config_hash=config_hash,
                now=now,
            )

            if action is ReviewAction.REJECT:
                self._emit_rejection_signal(
                    conn,
                    proposal_id=proposal_id,
                    lane=lane,
                    provenance=provenance,
                    code=code,
                    note=scrubbed_note,
                    review_id=review_id,
                )

            # R-20: run the caller's same-conn writes (e.g. the apply-skill
            # registry writes + its own skill.apply audit event) INSIDE this one
            # transaction, after the review row and before the transition's audit
            # dual-append. Any exception propagates and rolls EVERYTHING back —
            # the review row, the extra writes, and (never reached) the audit
            # event. No nested begin() is opened.
            if extra_writes is not None:
                extra_writes(conn, review_id)

            event = self._build_audit_event(
                action=action,
                proposal_id=proposal_id,
                provenance=provenance,
                review_id=review_id,
                from_status=current,
                to_status=target,
                code=code,
                applied_ref=scrubbed_ref,
            )
            outbox_append(conn, event)
            self._chain_append(conn, event)

            row_id = self._last_review_id(conn, review_id)

        return StoredReviewEvent(
            id=row_id,
            review_id=review_id,
            proposal_id=proposal_id,
            action=action.value,
            from_status=current.value,
            to_status=target.value,
            actor=provenance.principal.id,
            surface=provenance.surface.value,
            session_id=provenance.session_id,
            rejection_code=code.value if code is not None else None,
            note=scrubbed_note,
            applied_ref=scrubbed_ref,
            config_hash=config_hash,
            created_at=now,
        )

    def _insert_review(
        self,
        conn: sa.Connection,
        *,
        review_id: str,
        proposal_id: str,
        action: ReviewAction,
        from_status: ProposalStatus,
        to_status: ProposalStatus,
        provenance: SignalProvenance,
        code: RejectionCode | None,
        note: str | None,
        applied_ref: str | None,
        config_hash: str,
        now: datetime,
    ) -> None:
        stmt = ne_proposal_reviews.insert().values(
            review_id=review_id,
            proposal_id=proposal_id,
            action=action.value,
            from_status=from_status.value,
            to_status=to_status.value,
            actor=provenance.principal.id,
            surface=provenance.surface.value,
            session_id=provenance.session_id,
            rejection_code=code.value if code is not None else None,
            note=note,
            applied_ref=applied_ref,
            config_hash=config_hash,
            created_at=now,
        )
        try:
            conn.execute(stmt)
        except IntegrityError as exc:
            # The unique (proposal_id, action) index rejected a concurrent
            # duplicate. Do NOT idempotently swallow — a real double transition
            # is a conflict. This index is the SECOND line of defense behind the
            # `_proposal_lock_select` row lock (P2-13): the lock serializes racing
            # transitions on Postgres, and the index still guards same-action
            # duplicates (belt-and-braces).
            raise InvalidTransitionError(
                f"a {action.value} row already exists for proposal {proposal_id!r} "
                "(concurrent duplicate transition)"
            ) from exc

    def _last_review_id(self, conn: sa.Connection, review_id: str) -> int:
        return int(
            conn.execute(
                sa.select(ne_proposal_reviews.c.id).where(
                    ne_proposal_reviews.c.review_id == review_id
                )
            ).scalar_one()
        )

    def _emit_rejection_signal(
        self,
        conn: sa.Connection,
        *,
        proposal_id: str,
        lane: str,
        provenance: SignalProvenance,
        code: RejectionCode | None,
        note: str | None,
        review_id: str,
    ) -> None:
        """Append the ``thumbs_down`` rejection-feedback signal (store + outbox).

        Mirrors ``capture.emit``'s dual-write body inline (capture.emit itself is
        not reusable here because it opens its OWN transaction — this must ride
        the caller's ``conn`` so the signal, the review row, and the audit event
        commit atomically).
        """
        prov: dict[str, str] = {
            "surface": provenance.surface.value,
            "principal": provenance.principal.id,
            "session_id": provenance.session_id,
            "source": "ne-flywheel",
            "component": "review",
            "rejected_proposal_id": proposal_id,
            "proposal_lane": lane,
            "rejection_code": code.value if code is not None else RejectionCode.OTHER.value,
            "review_id": review_id,
        }
        if provenance.turn_id is not None:
            prov["turn_id"] = provenance.turn_id
        if provenance.message_id is not None:
            prov["message_id"] = provenance.message_id

        signal = new_correction_signal(
            signal_kind=CorrectionKind.thumbs_down,
            subject_ref=f"proposal:{proposal_id}",
            actor=provenance.principal.id,
            comment=note,
            provenance=prov,
        )
        self._signal_store.append(
            conn,
            signal,
            surface=provenance.surface.value,
            principal_id=provenance.principal.id,
            session_id=provenance.session_id,
            turn_id=provenance.turn_id,
            message_id=provenance.message_id,
        )
        outbox_append(conn, signal)

    def _build_audit_event(
        self,
        *,
        action: ReviewAction,
        proposal_id: str,
        provenance: SignalProvenance,
        review_id: str,
        from_status: ProposalStatus,
        to_status: ProposalStatus,
        code: RejectionCode | None,
        applied_ref: str | None,
    ) -> AuditEvent:
        """Build the ``proposal.<action>`` audit event — ids/codes/refs ONLY (C12).

        The detail carries ids, the status transition, the rejection code, and
        the applied_ref (a ref) — NEVER the note free text.
        """
        return new_audit_event(
            action=f"proposal.{action.value}",
            resource=f"flywheel:proposal:{proposal_id}",
            outcome=AuditOutcome.success,
            actor=provenance.principal.id,
            detail=json.dumps(
                {
                    "review_id": review_id,
                    "proposal_id": proposal_id,
                    "from_status": from_status.value,
                    "to_status": to_status.value,
                    "rejection_code": code.value if code is not None else None,
                    "applied_ref": applied_ref,
                }
            ),
            provenance={
                **_PROVENANCE_SOURCE,
                "surface": provenance.surface.value,
                "session_id": provenance.session_id,
            },
        )

    def _chain_append(self, conn: sa.Connection, event: AuditEvent) -> None:
        """Append ``event`` to the hash chain; fail closed on any failure (C6).

        A chain-append failure raises :class:`ReviewAuditError` so the WHOLE
        review transaction (review row + any rejection signal + outbox event)
        rolls back — nothing persists without its tamper-evident audit record.
        Copied from ``curation._chain_append``.
        """
        try:
            append_record(conn, event)
        except Exception as exc:
            raise ReviewAuditError(
                f"audit-chain append failed for proposal.{event.action}; the whole "
                "review transition rolls back (C6 fail closed)"
            ) from exc


__all__ = [
    "ProposalReviewService",
    "ProposalStatus",
    "RejectionCode",
    "ReviewAction",
    "StoredReviewEvent",
    "ensure_review_schema",
    "ne_proposal_reviews",
    "review_metadata",
]
