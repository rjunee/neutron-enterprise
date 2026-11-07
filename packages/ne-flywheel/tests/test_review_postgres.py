# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""LIVE-PARKABLE Postgres acceptance for the propose-then-approve gate (NE-066).

Env-gated on ``NE_TEST_POSTGRES_URL``, mirroring ``test_capture_postgres.py``.
The build host has no Docker daemon, so this module auto-skips
(pending-live-verification ledger). When a real Postgres endpoint is configured
it re-runs the approve->apply roundtrip and the reject->signal atomic dual-write
under REAL Postgres transactions (Identity PKs, JSONB payload round-trip, real
unique-index + rollback behavior).
"""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Callable, Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import event

from ne.audit import verify_chain
from ne.audit.store import metadata as audit_metadata
from ne.contracts import Principal, freeze_config_hash, reset_config_hash_for_testing
from ne.contracts.config import NeSettings
from ne.events.outbox import metadata as outbox_metadata
from ne.flywheel.capture import CorrectionSignalCapture
from ne.flywheel.curation import CurationAgent, curation_metadata, ensure_curation_schema
from ne.flywheel.errors import InvalidTransitionError
from ne.flywheel.proposals import SqlAlchemyProposalStore, proposal_metadata
from ne.flywheel.review import (
    ProposalReviewService,
    ProposalStatus,
    RejectionCode,
    ensure_review_schema,
    ne_proposal_reviews,
    review_metadata,
)
from ne.flywheel.signals import (
    ApprovalRejectionSignal,
    SignalProvenance,
    SignalSurface,
    ThumbsSignal,
)
from ne.flywheel.store import SqlAlchemyCorrectionSignalStore, ne_correction_signals
from ne.flywheel.store import flywheel_metadata as signal_metadata

_URL = os.environ.get("NE_TEST_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    not _URL,
    reason="live Postgres not configured (set NE_TEST_POSTGRES_URL); "
    "pending-live-verification ledger",
)


class _DemoSettings(NeSettings):
    demo: int = 7


def _drop_all(engine: sa.Engine) -> None:
    review_metadata.drop_all(engine)
    curation_metadata.drop_all(engine)
    proposal_metadata.drop_all(engine)
    outbox_metadata.drop_all(engine)
    signal_metadata.drop_all(engine)
    audit_metadata.drop_all(engine)


@pytest.fixture
def pg_engine() -> Iterator[sa.Engine]:
    assert _URL is not None
    engine = sa.create_engine(_URL)
    _drop_all(engine)
    ensure_curation_schema(engine)
    ensure_review_schema(engine)
    try:
        yield engine
    finally:
        _drop_all(engine)
        engine.dispose()


@pytest.fixture
def frozen_config() -> Iterator[str]:
    reset_config_hash_for_testing()
    digest = freeze_config_hash(_DemoSettings())
    try:
        yield digest
    finally:
        reset_config_hash_for_testing()


def _prov() -> SignalProvenance:
    return SignalProvenance(
        principal=Principal(id="user:pg@acme.com"),
        surface=SignalSurface.API,
        session_id="sess-pg",
    )


def _seed_and_curate(engine: sa.Engine) -> list[str]:
    capture = CorrectionSignalCapture(engine)
    asyncio.run(capture.capture(_prov(), ThumbsSignal(up=False, target_ref="doc-td")))
    asyncio.run(
        capture.capture(_prov(), ApprovalRejectionSignal(approval_id="a1", tier=2, reason="risk"))
    )
    asyncio.run(CurationAgent(engine).run_once())
    return [p.proposal_id for p in SqlAlchemyProposalStore(engine).read_proposals()]


def test_review_lifecycle_under_real_postgres(pg_engine: sa.Engine, frozen_config: str) -> None:
    proposal_ids = _seed_and_curate(pg_engine)
    svc = ProposalReviewService(pg_engine)
    pid = proposal_ids[0]

    svc.approve(pid, _prov())
    svc.record_apply(pid, _prov(), applied_ref="https://git.example/pr/42")

    assert svc.status_of(pid) is ProposalStatus.APPLIED
    assert verify_chain(pg_engine).valid


def test_reject_signal_atomic_under_real_postgres(pg_engine: sa.Engine, frozen_config: str) -> None:
    proposal_ids = _seed_and_curate(pg_engine)
    svc = ProposalReviewService(pg_engine)
    pid = proposal_ids[0]
    signal_store = SqlAlchemyCorrectionSignalStore(pg_engine)
    before = len(signal_store.read_signals(limit=1000))

    svc.reject(pid, _prov(), code=RejectionCode.INCORRECT, note="wrong doc")

    assert svc.status_of(pid) is ProposalStatus.REJECTED
    after = signal_store.read_signals(limit=1000)
    assert len(after) == before + 1
    with pg_engine.connect() as conn:
        rows = conn.execute(
            sa.select(ne_correction_signals).where(
                ne_correction_signals.c.subject_ref == f"proposal:{pid}"
            )
        ).all()
    assert len(rows) == 1
    assert rows[0].signal_kind == "thumbs_down"
    assert verify_chain(pg_engine).valid


def _review_rows(engine: sa.Engine, proposal_id: str) -> list[sa.Row[Any]]:
    with engine.connect() as conn:
        return list(
            conn.execute(
                sa.select(ne_proposal_reviews)
                .where(ne_proposal_reviews.c.proposal_id == proposal_id)
                .order_by(ne_proposal_reviews.c.id.asc())
            ).all()
        )


def _run_interleave(
    engine: sa.Engine,
    holder: tuple[str, Callable[[], object]],
    rival: tuple[str, Callable[[], object]],
) -> dict[str, BaseException]:
    """Deterministic two-connection interleave of two transitions on one proposal.

    The HOLDER thread starts its transition and pauses (via a one-shot
    ``before_cursor_execute`` listener) at its ``ne_proposal_reviews`` INSERT —
    at which point it already holds the ``FOR UPDATE`` row lock taken before the
    fold. The RIVAL thread then starts: post-fix it BLOCKS on the row lock (so it
    is still alive at ``join(timeout=1.5)``); the holder is then released, commits
    and drops the lock, and the rival folds the holder's COMMITTED state. Every
    wait/join is hard-bounded and the listener is removed in ``finally`` — no hang
    is possible regardless of thread scheduling.
    """
    holder_in_txn = threading.Event()
    release_holder = threading.Event()
    errors: dict[str, BaseException] = {}

    def _pause(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        if (
            not holder_in_txn.is_set()
            and statement.lstrip().upper().startswith("INSERT")
            and "ne_proposal_reviews" in statement
        ):
            holder_in_txn.set()
            release_holder.wait(timeout=5.0)

    def _runner(name: str, fn: Callable[[], object]) -> Callable[[], None]:
        def _run() -> None:
            try:
                fn()
            except BaseException as exc:
                errors[name] = exc

        return _run

    event.listen(engine, "before_cursor_execute", _pause)
    thread_a = threading.Thread(target=_runner(holder[0], holder[1]))
    thread_b = threading.Thread(target=_runner(rival[0], rival[1]))
    try:
        thread_a.start()
        assert holder_in_txn.wait(timeout=5.0), "holder never entered its transaction"
        thread_b.start()
        # Pre-fix the rival completes here (the P2-13 resurrect bug); post-fix it
        # blocks on the FOR UPDATE row lock and is still alive.
        thread_b.join(timeout=1.5)
        release_holder.set()
        thread_a.join(timeout=10.0)
        thread_b.join(timeout=10.0)
        assert not thread_a.is_alive()
        assert not thread_b.is_alive()
    finally:
        release_holder.set()
        event.remove(engine, "before_cursor_execute", _pause)
    return errors


def test_interleaved_reject_approve_exactly_one_wins_under_real_postgres(
    pg_engine: sa.Engine, frozen_config: str
) -> None:
    proposal_ids = _seed_and_curate(pg_engine)
    svc = ProposalReviewService(pg_engine)
    pid = proposal_ids[0]

    errors = _run_interleave(
        pg_engine,
        holder=("reject", lambda: svc.reject(pid, _prov(), code=RejectionCode.INCORRECT)),
        rival=("approve", lambda: svc.approve(pid, _prov())),
    )

    # Reject wins; the racing approve folds the committed REJECTED state and
    # raises (rejected is terminal) — exactly one winner.
    assert set(errors) == {"approve"}
    assert isinstance(errors["approve"], InvalidTransitionError)
    assert svc.status_of(pid) is ProposalStatus.REJECTED
    with pytest.raises(InvalidTransitionError):
        svc.record_apply(pid, _prov(), applied_ref="https://git.example/pr/1")

    rows = _review_rows(pg_engine, pid)
    proposed = [r for r in rows if r.from_status == ProposalStatus.PROPOSED.value]
    assert len(proposed) == 1
    assert proposed[0].action == "reject"
    assert verify_chain(pg_engine).valid


def test_interleaved_approve_reject_loser_folds_locked_state_under_real_postgres(
    pg_engine: sa.Engine, frozen_config: str
) -> None:
    proposal_ids = _seed_and_curate(pg_engine)
    svc = ProposalReviewService(pg_engine)
    pid = proposal_ids[0]

    errors = _run_interleave(
        pg_engine,
        holder=("approve", lambda: svc.approve(pid, _prov())),
        rival=("reject", lambda: svc.reject(pid, _prov(), code=RejectionCode.INCORRECT)),
    )

    # Approve wins; the racing reject folds the committed APPROVED state and lands
    # HONESTLY as approved -> rejected (never a stale 'proposed'). No exceptions.
    assert errors == {}
    rows = _review_rows(pg_engine, pid)
    by_action = {r.action: r for r in rows}
    assert by_action["approve"].from_status == ProposalStatus.PROPOSED.value
    assert by_action["reject"].from_status == ProposalStatus.APPROVED.value
    proposed = [r for r in rows if r.from_status == ProposalStatus.PROPOSED.value]
    assert len(proposed) == 1
    assert svc.status_of(pid) is ProposalStatus.REJECTED
    with pytest.raises(InvalidTransitionError):
        svc.record_apply(pid, _prov(), applied_ref="https://git.example/pr/2")
    assert verify_chain(pg_engine).valid
