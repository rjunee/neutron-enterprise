# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""State-machine suite for the propose-then-approve gate (NE-066, real sqlite seam).

Runs against ``seeded_proposals`` — genuine NE-065-produced proposals seeded
through the REAL capture -> curation path. Every transition is asserted to
persist an append-only review row and dual-append a config_hash-stamped audit
event; a rejection is asserted to emit a ``thumbs_down`` correction signal back
into the NE-064 store atomically. asyncio-free (the service is sync).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
import sqlalchemy as sa

from ne.audit import verify_chain
from ne.audit.store import ne_audit_log
from ne.events.outbox import ne_outbox_events
from ne.flywheel.errors import (
    InvalidTransitionError,
    ReviewAuditError,
    ReviewError,
    UnknownProposalError,
)
from ne.flywheel.proposals import StoredFixProposal, ne_fix_proposals
from ne.flywheel.review import (
    ProposalReviewService,
    ProposalStatus,
    RejectionCode,
    ne_proposal_reviews,
)
from ne.flywheel.signals import SignalProvenance
from ne.flywheel.store import ne_correction_signals


def _count(engine: sa.Engine, table: sa.Table) -> int:
    with engine.connect() as conn:
        return int(conn.execute(sa.select(sa.func.count()).select_from(table)).scalar_one())


def _pick(proposals: list[StoredFixProposal], lane: str, kind: str = "fix") -> StoredFixProposal:
    return next(p for p in proposals if p.lane == lane and p.kind == kind)


def _audit_outbox_rows(engine: sa.Engine, action: str) -> list[sa.Row[Any]]:
    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(ne_outbox_events).where(ne_outbox_events.c.event_type == "audit")
        ).all()
    return [r for r in rows if r.payload["action"] == action]


def _audit_log_rows(engine: sa.Engine, action: str) -> list[sa.Row[Any]]:
    with engine.connect() as conn:
        rows = conn.execute(sa.select(ne_audit_log).where(ne_audit_log.c.action == action)).all()
    return list(rows)


def test_approve_persists_and_dual_appends_audit(
    review_engine: sa.Engine,
    seeded_proposals: list[StoredFixProposal],
    frozen_config: str,
    make_provenance: Callable[..., SignalProvenance],
) -> None:
    proposal = _pick(seeded_proposals, "content")
    svc = ProposalReviewService(review_engine)
    prov = make_provenance()

    event = svc.approve(proposal.proposal_id, prov, note="ship it")

    assert svc.status_of(proposal.proposal_id) is ProposalStatus.APPROVED
    assert event.from_status == "proposed"
    assert event.to_status == "approved"
    assert event.actor == prov.principal.id
    assert event.surface == prov.surface.value
    assert event.session_id == prov.session_id
    assert event.config_hash == frozen_config

    # Exactly ONE proposal.approve event in BOTH the outbox and the hash chain,
    # with the same event_id.
    outbox = _audit_outbox_rows(review_engine, "proposal.approve")
    chain = _audit_log_rows(review_engine, "proposal.approve")
    assert len(outbox) == 1
    assert len(chain) == 1
    assert chain[0].event_id == outbox[0].event_id
    assert chain[0].config_hash == frozen_config

    assert verify_chain(review_engine).valid


def test_reject_emits_correction_signal_into_ne064_store(
    review_engine: sa.Engine,
    seeded_proposals: list[StoredFixProposal],
    frozen_config: str,
    make_provenance: Callable[..., SignalProvenance],
) -> None:
    proposal = _pick(seeded_proposals, "content")
    svc = ProposalReviewService(review_engine)
    prov = make_provenance()
    signals_before = _count(review_engine, ne_correction_signals)

    svc.reject(proposal.proposal_id, prov, code=RejectionCode.INCORRECT, note="wrong doc")

    assert svc.status_of(proposal.proposal_id) is ProposalStatus.REJECTED
    assert _count(review_engine, ne_correction_signals) == signals_before + 1

    with review_engine.connect() as conn:
        rows = conn.execute(
            sa.select(ne_correction_signals).where(
                ne_correction_signals.c.subject_ref == f"proposal:{proposal.proposal_id}"
            )
        ).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.signal_kind == "thumbs_down"
    prov_map = row.payload["provenance"]
    assert prov_map["rejected_proposal_id"] == proposal.proposal_id
    assert prov_map["proposal_lane"] == "content"
    assert prov_map["rejection_code"] == RejectionCode.INCORRECT.value
    assert prov_map["principal"] == prov.principal.id
    assert prov_map["review_id"]

    # The SAME signal event is mirrored to the outbox (correction_signal type).
    with review_engine.connect() as conn:
        ob = conn.execute(
            sa.select(ne_outbox_events).where(ne_outbox_events.c.event_id == row.event_id)
        ).all()
    assert len(ob) == 1

    # proposal.reject audit dual-appended; chain valid — the loop is closed.
    assert len(_audit_outbox_rows(review_engine, "proposal.reject")) == 1
    assert len(_audit_log_rows(review_engine, "proposal.reject")) == 1
    assert verify_chain(review_engine).valid


def test_full_lifecycle_proposed_approved_applied(
    review_engine: sa.Engine,
    seeded_proposals: list[StoredFixProposal],
    frozen_config: str,
    make_provenance: Callable[..., SignalProvenance],
) -> None:
    proposal = _pick(seeded_proposals, "content")
    svc = ProposalReviewService(review_engine)
    prov = make_provenance()

    svc.approve(proposal.proposal_id, prov)
    applied = svc.record_apply(proposal.proposal_id, prov, applied_ref="https://git.example/pr/42")

    assert svc.status_of(proposal.proposal_id) is ProposalStatus.APPLIED
    assert applied.applied_ref == "https://git.example/pr/42"
    assert len(_audit_log_rows(review_engine, "proposal.approve")) == 1
    assert len(_audit_log_rows(review_engine, "proposal.apply")) == 1
    assert verify_chain(review_engine).valid

    # NE-065's proposal store was NEVER updated — status column still 'proposed'.
    with review_engine.connect() as conn:
        status = conn.execute(
            sa.select(ne_fix_proposals.c.status).where(
                ne_fix_proposals.c.proposal_id == proposal.proposal_id
            )
        ).scalar_one()
    assert status == "proposed"


def test_apply_without_approval_is_rejected(
    review_engine: sa.Engine,
    seeded_proposals: list[StoredFixProposal],
    frozen_config: str,
    make_provenance: Callable[..., SignalProvenance],
) -> None:
    proposal = _pick(seeded_proposals, "content")
    svc = ProposalReviewService(review_engine)
    reviews_before = _count(review_engine, ne_proposal_reviews)

    with pytest.raises(InvalidTransitionError):
        svc.record_apply(proposal.proposal_id, make_provenance(), applied_ref="https://git/pr/1")

    assert _count(review_engine, ne_proposal_reviews) == reviews_before
    assert svc.status_of(proposal.proposal_id) is ProposalStatus.PROPOSED


def test_terminal_states_are_terminal(
    review_engine: sa.Engine,
    seeded_proposals: list[StoredFixProposal],
    frozen_config: str,
    make_provenance: Callable[..., SignalProvenance],
) -> None:
    svc = ProposalReviewService(review_engine)
    prov = make_provenance()

    # After reject: nothing more is legal.
    rejected = _pick(seeded_proposals, "content")
    svc.reject(rejected.proposal_id, prov, code=RejectionCode.STALE)
    for op in (
        lambda: svc.approve(rejected.proposal_id, prov),
        lambda: svc.reject(rejected.proposal_id, prov, code=RejectionCode.OTHER),
        lambda: svc.record_apply(rejected.proposal_id, prov, applied_ref="ref"),
    ):
        with pytest.raises(InvalidTransitionError):
            op()

    # After apply: likewise terminal.
    applied = _pick(seeded_proposals, "policy")
    svc.approve(applied.proposal_id, prov)
    svc.record_apply(applied.proposal_id, prov, applied_ref="https://git/pr/9")
    for op in (
        lambda: svc.approve(applied.proposal_id, prov),
        lambda: svc.reject(applied.proposal_id, prov, code=RejectionCode.OTHER),
        lambda: svc.record_apply(applied.proposal_id, prov, applied_ref="ref2"),
    ):
        with pytest.raises(InvalidTransitionError):
            op()


def test_reject_after_approve_allowed_and_emits_signal(
    review_engine: sa.Engine,
    seeded_proposals: list[StoredFixProposal],
    frozen_config: str,
    make_provenance: Callable[..., SignalProvenance],
) -> None:
    proposal = _pick(seeded_proposals, "content")
    svc = ProposalReviewService(review_engine)
    prov = make_provenance()
    signals_before = _count(review_engine, ne_correction_signals)

    svc.approve(proposal.proposal_id, prov)
    event = svc.reject(proposal.proposal_id, prov, code=RejectionCode.UNSAFE)

    assert event.from_status == "approved"
    assert event.to_status == "rejected"
    assert svc.status_of(proposal.proposal_id) is ProposalStatus.REJECTED
    # approved -> rejected still emits the feedback signal.
    assert _count(review_engine, ne_correction_signals) == signals_before + 1


def test_double_approve_blocked(
    review_engine: sa.Engine,
    seeded_proposals: list[StoredFixProposal],
    frozen_config: str,
    make_provenance: Callable[..., SignalProvenance],
) -> None:
    proposal = _pick(seeded_proposals, "content")
    svc = ProposalReviewService(review_engine)
    prov = make_provenance()

    svc.approve(proposal.proposal_id, prov)
    with pytest.raises(InvalidTransitionError):
        svc.approve(proposal.proposal_id, prov)

    # The unique (proposal_id, action) index exists.
    indexes = {ix["name"] for ix in sa.inspect(review_engine).get_indexes("ne_proposal_reviews")}
    assert "ux_ne_proposal_reviews_proposal_action" in indexes


def test_unknown_proposal_raises(
    review_engine: sa.Engine,
    seeded_proposals: list[StoredFixProposal],
    frozen_config: str,
    make_provenance: Callable[..., SignalProvenance],
) -> None:
    svc = ProposalReviewService(review_engine)
    reviews_before = _count(review_engine, ne_proposal_reviews)

    with pytest.raises(UnknownProposalError):
        svc.approve("deadbeef" * 8, make_provenance())

    assert _count(review_engine, ne_proposal_reviews) == reviews_before


def test_transition_is_atomic_fail_closed(
    review_engine: sa.Engine,
    seeded_proposals: list[StoredFixProposal],
    frozen_config: str,
    make_provenance: Callable[..., SignalProvenance],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proposal = _pick(seeded_proposals, "content")
    svc = ProposalReviewService(review_engine)

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("audit chain unavailable")

    monkeypatch.setattr("ne.flywheel.review.append_record", _boom)

    reviews_before = _count(review_engine, ne_proposal_reviews)
    signals_before = _count(review_engine, ne_correction_signals)
    outbox_before = _count(review_engine, ne_outbox_events)

    with pytest.raises(ReviewAuditError):
        svc.reject(proposal.proposal_id, make_provenance(), code=RejectionCode.INCORRECT)

    # Whole-transaction rollback: ZERO new review rows, correction signals, outbox rows.
    assert _count(review_engine, ne_proposal_reviews) == reviews_before
    assert _count(review_engine, ne_correction_signals) == signals_before
    assert _count(review_engine, ne_outbox_events) == outbox_before


def test_note_free_text_is_scrubbed_and_capped(
    review_engine: sa.Engine,
    seeded_proposals: list[StoredFixProposal],
    frozen_config: str,
    make_provenance: Callable[..., SignalProvenance],
) -> None:
    proposal = _pick(seeded_proposals, "content")
    svc = ProposalReviewService(review_engine)

    event = svc.reject(
        proposal.proposal_id,
        make_provenance(),
        code=RejectionCode.INCORRECT,
        note="password=hunter2",
    )

    # The review row stores the redacted note.
    assert event.note == "[REDACTED]"
    with review_engine.connect() as conn:
        review_note = conn.execute(
            sa.select(ne_proposal_reviews.c.note).where(
                ne_proposal_reviews.c.review_id == event.review_id
            )
        ).scalar_one()
        signal_comment = conn.execute(
            sa.select(ne_correction_signals.c.comment).where(
                ne_correction_signals.c.subject_ref == f"proposal:{proposal.proposal_id}"
            )
        ).scalar_one()
        audit_payload = conn.execute(
            sa.select(ne_audit_log.c.payload).where(ne_audit_log.c.action == "proposal.reject")
        ).scalar_one()
    assert review_note == "[REDACTED]"
    assert signal_comment == "[REDACTED]"
    # The audit event (incl. its detail) carries ids/codes only — NEVER the note
    # free text (C12). The whole serialized record must not leak the note.
    audit_text = str(audit_payload)
    assert "hunter2" not in audit_text
    assert "password" not in audit_text


def test_applied_ref_required_non_empty(
    review_engine: sa.Engine,
    seeded_proposals: list[StoredFixProposal],
    frozen_config: str,
    make_provenance: Callable[..., SignalProvenance],
) -> None:
    proposal = _pick(seeded_proposals, "content")
    svc = ProposalReviewService(review_engine)
    prov = make_provenance()
    svc.approve(proposal.proposal_id, prov)
    reviews_before = _count(review_engine, ne_proposal_reviews)

    with pytest.raises(ReviewError):
        svc.record_apply(proposal.proposal_id, prov, applied_ref="  ")

    assert _count(review_engine, ne_proposal_reviews) == reviews_before


def test_status_of_unreviewed_proposal_is_proposed(
    review_engine: sa.Engine,
    seeded_proposals: list[StoredFixProposal],
    frozen_config: str,
) -> None:
    svc = ProposalReviewService(review_engine)
    proposal = _pick(seeded_proposals, "content")
    assert svc.status_of(proposal.proposal_id) is ProposalStatus.PROPOSED


# -- R-20 extra_writes same-conn hook (NE-SKILL-1b apply-skill seam) -----------

_scratch = sa.Table(
    "ne_test_extra_writes_scratch",
    sa.MetaData(),
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("review_id", sa.String, nullable=False),
)


def _ensure_scratch(engine: sa.Engine) -> None:
    _scratch.metadata.create_all(engine)


def test_extra_writes_commits_atomically_with_the_review_row(
    review_engine: sa.Engine,
    seeded_proposals: list[StoredFixProposal],
    frozen_config: str,
    make_provenance: Callable[..., SignalProvenance],
) -> None:
    _ensure_scratch(review_engine)
    proposal = _pick(seeded_proposals, "content")
    svc = ProposalReviewService(review_engine)
    prov = make_provenance()
    svc.approve(proposal.proposal_id, prov)

    seen: dict[str, str] = {}

    def _extra(conn: sa.Connection, review_id: str) -> None:
        seen["review_id"] = review_id
        conn.execute(_scratch.insert().values(review_id=review_id))

    applied = svc.record_apply(
        proposal.proposal_id, prov, applied_ref="skill:x@v1", extra_writes=_extra
    )

    assert svc.status_of(proposal.proposal_id) is ProposalStatus.APPLIED
    # (c) the hook received the SAME review_id the returned event carries.
    assert seen["review_id"] == applied.review_id
    with review_engine.connect() as conn:
        rows = conn.execute(sa.select(_scratch.c.review_id)).all()
    assert [r.review_id for r in rows] == [applied.review_id]
    assert verify_chain(review_engine).valid


def test_extra_writes_raising_rolls_back_the_whole_transition(
    review_engine: sa.Engine,
    seeded_proposals: list[StoredFixProposal],
    frozen_config: str,
    make_provenance: Callable[..., SignalProvenance],
) -> None:
    _ensure_scratch(review_engine)
    proposal = _pick(seeded_proposals, "content")
    svc = ProposalReviewService(review_engine)
    prov = make_provenance()
    svc.approve(proposal.proposal_id, prov)
    reviews_before = _count(review_engine, ne_proposal_reviews)
    apply_audit_before = len(_audit_log_rows(review_engine, "proposal.apply"))

    class _Boom(RuntimeError):
        pass

    def _extra(conn: sa.Connection, review_id: str) -> None:
        conn.execute(_scratch.insert().values(review_id=review_id))
        raise _Boom("registry write failed")

    with pytest.raises(_Boom):
        svc.record_apply(proposal.proposal_id, prov, applied_ref="skill:x@v1", extra_writes=_extra)

    # (b) NO review row, fold stays approved, no apply audit event, scratch empty.
    assert svc.status_of(proposal.proposal_id) is ProposalStatus.APPROVED
    assert _count(review_engine, ne_proposal_reviews) == reviews_before
    assert len(_audit_log_rows(review_engine, "proposal.apply")) == apply_audit_before
    with review_engine.connect() as conn:
        scratch_count = int(
            conn.execute(sa.select(sa.func.count()).select_from(_scratch)).scalar_one()
        )
    assert scratch_count == 0
    assert verify_chain(review_engine).valid
