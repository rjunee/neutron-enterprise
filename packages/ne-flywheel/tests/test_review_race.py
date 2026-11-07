# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""P2-13 regression coverage — lock-before-fold serialization of transitions.

Sqlite-runnable proofs that :func:`_proposal_lock_select` renders a real
``FOR UPDATE`` row lock on the production (Postgres) dialect and is a silent
no-op on sqlite, that :meth:`ProposalReviewService._transition` takes that lock
BEFORE the status fold (structural wiring), and that the race-equivalent
sequential semantics hold: a steward-rejected proposal can NEVER reach APPLIED,
in either ordering. The live two-connection interleave proof lives in
``test_review_postgres.py`` (env-gated on ``NE_TEST_POSTGRES_URL``).
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, sqlite

from ne.flywheel.errors import InvalidTransitionError
from ne.flywheel.proposals import StoredFixProposal
from ne.flywheel.review import (
    ProposalReviewService,
    ProposalStatus,
    RejectionCode,
    _proposal_lock_select,  # pyright: ignore[reportPrivateUsage]
    ne_proposal_reviews,
)
from ne.flywheel.signals import SignalProvenance


def _pick(proposals: list[StoredFixProposal], lane: str) -> StoredFixProposal:
    return next(p for p in proposals if p.lane == lane and p.kind == "fix")


def _proposed_review_rows(engine: sa.Engine, proposal_id: str) -> list[sa.Row[Any]]:
    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(ne_proposal_reviews).where(
                ne_proposal_reviews.c.proposal_id == proposal_id,
                ne_proposal_reviews.c.from_status == ProposalStatus.PROPOSED.value,
            )
        ).all()
    return list(rows)


def test_lock_select_renders_for_update_on_postgres_dialect() -> None:
    compiled = str(_proposal_lock_select("p-1").compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" in compiled
    assert "ne_fix_proposals.lane" in compiled
    assert "proposal_id" in compiled


def test_lock_select_is_silently_omitted_on_sqlite_dialect() -> None:
    compiled = str(_proposal_lock_select("p-1").compile(dialect=sqlite.dialect()))
    assert "FOR UPDATE" not in compiled


def test_transition_locks_proposal_row_before_folding() -> None:
    source = inspect.getsource(ProposalReviewService._transition)
    assert "_proposal_lock_select(" in source
    assert "_fold_status(" in source
    assert source.index("_proposal_lock_select(") < source.index("_fold_status(")


def test_approve_after_committed_reject_raises_and_never_applies(
    review_engine: sa.Engine,
    seeded_proposals: list[StoredFixProposal],
    frozen_config: str,
    make_provenance: Callable[..., SignalProvenance],
) -> None:
    proposal = _pick(seeded_proposals, "content")
    svc = ProposalReviewService(review_engine)
    prov = make_provenance()

    # Reject wins (commits first); a racing approve then folds the committed
    # REJECTED state and must raise (rejected is terminal).
    svc.reject(proposal.proposal_id, prov, code=RejectionCode.INCORRECT)
    with pytest.raises(InvalidTransitionError):
        svc.approve(proposal.proposal_id, prov)

    # A rejected proposal can NEVER reach APPLIED.
    with pytest.raises(InvalidTransitionError):
        svc.record_apply(proposal.proposal_id, prov, applied_ref="https://git/pr/1")

    assert svc.status_of(proposal.proposal_id) is ProposalStatus.REJECTED
    # Exactly one review row folded from the original 'proposed' state.
    assert len(_proposed_review_rows(review_engine, proposal.proposal_id)) == 1


def test_reject_after_committed_approve_records_honest_state_and_never_applies(
    review_engine: sa.Engine,
    seeded_proposals: list[StoredFixProposal],
    frozen_config: str,
    make_provenance: Callable[..., SignalProvenance],
) -> None:
    proposal = _pick(seeded_proposals, "content")
    svc = ProposalReviewService(review_engine)
    prov = make_provenance()

    # Approve wins; a racing reject then folds the committed APPROVED state and
    # lands HONESTLY as approved -> rejected (never a stale 'proposed').
    svc.approve(proposal.proposal_id, prov)
    event = svc.reject(proposal.proposal_id, prov, code=RejectionCode.UNSAFE)
    assert event.from_status == ProposalStatus.APPROVED.value
    assert event.to_status == ProposalStatus.REJECTED.value

    # After the reject, the proposal is terminal — record_apply must raise.
    with pytest.raises(InvalidTransitionError):
        svc.record_apply(proposal.proposal_id, prov, applied_ref="https://git/pr/2")

    assert svc.status_of(proposal.proposal_id) is ProposalStatus.REJECTED
    # Exactly one review row folded from the original 'proposed' state (approve).
    assert len(_proposed_review_rows(review_engine, proposal.proposal_id)) == 1
