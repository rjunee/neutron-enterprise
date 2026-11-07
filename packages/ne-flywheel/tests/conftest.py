# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures for the ne-flywheel tests.

Runs against a real in-memory SQLite engine (the SAME SQLAlchemy statements,
including SAVEPOINT) — nothing is mocked past the seam (plan §1.4).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator

import pytest
import sqlalchemy as sa

from ne.audit import ensure_transactional_sqlite
from ne.contracts import Principal, freeze_config_hash, reset_config_hash_for_testing
from ne.contracts.config import NeSettings
from ne.events import ensure_outbox_schema
from ne.flywheel.capture import CorrectionSignalCapture
from ne.flywheel.curation import CurationAgent, ensure_curation_schema
from ne.flywheel.proposals import SqlAlchemyProposalStore, StoredFixProposal
from ne.flywheel.review import ensure_review_schema
from ne.flywheel.signals import (
    ApprovalRejectionSignal,
    DraftEditSignal,
    ReQuerySignal,
    SignalPayload,
    SignalProvenance,
    SignalSurface,
    ThumbsSignal,
    WrongSourceSignal,
)
from ne.flywheel.store import ensure_flywheel_schema


class _DemoSettings(NeSettings):
    demo: int = 7


@pytest.fixture
def engine() -> Iterator[sa.Engine]:
    eng = sa.create_engine("sqlite://")
    ensure_flywheel_schema(eng)
    ensure_outbox_schema(eng)
    try:
        yield eng
    finally:
        eng.dispose()


def make_curation_engine(url: str = "sqlite://") -> sa.Engine:
    """A properly-transactional in-memory sqlite engine + the full curation schema.

    pysqlite's autocommit-emulation does not open a real transaction before DML,
    which lets a released SAVEPOINT survive an OUTER rollback — so a fail-closed
    curation run (audit-append failure) would leave its proposals behind on
    sqlite even though Postgres discards them. Delegates to the SHARED NE-CLEAN2
    recipe :func:`ne.audit.ensure_transactional_sqlite` (the NE-069 reference
    recipe hoisted) so the in-memory sqlite test faithfully reproduces the
    Postgres atomicity the engine guarantees. Delegating (rather than installing
    a local begin-listener) is MANDATORY: the flywheel constructors now apply the
    same shared helper, and the WeakSet guard makes double-application a no-op —
    a second local begin-listener would stack and double-emit ``BEGIN``.
    """
    eng = sa.create_engine(url)
    ensure_transactional_sqlite(eng)
    ensure_curation_schema(eng)
    return eng


@pytest.fixture
def curation_engine() -> Iterator[sa.Engine]:
    """A fresh sqlite engine with the FULL curation schema closure (NE-065).

    Proposals + cursor + correction-signal + outbox + audit-chain tables all
    ride ONE engine, so a curation run's proposals and its dual-appended audit
    event commit atomically (same-engine requirement).
    """
    eng = make_curation_engine()
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def seed_signal(
    curation_engine: sa.Engine,
    make_provenance: Callable[..., SignalProvenance],
) -> Callable[..., str]:
    """Drive the real NE-064 capture path to seed ONE signal; return its event_id.

    Uses ``asyncio.run`` per the repo convention (no pytest-asyncio) — the SAME
    ``CorrectionSignalCapture.capture`` surfaces call, so the seeded rows are
    byte-identical to production capture.
    """
    capture = CorrectionSignalCapture(curation_engine)

    def _seed(payload: SignalPayload, **provenance_kwargs: object) -> str:
        provenance = make_provenance(**provenance_kwargs)
        return asyncio.run(capture.capture(provenance, payload))

    return _seed


@pytest.fixture
def review_engine() -> Iterator[sa.Engine]:
    """A fresh sqlite engine with the FULL review schema closure (NE-066).

    Reuses the pysqlite BEGIN-recipe helper (``make_curation_engine``) so the
    NE-066 fail-closed atomicity tests are faithful to Postgres, exactly like
    NE-065's tests. Reviews + proposals + signals + outbox + audit tables all
    ride ONE engine so a transition's review row, any rejection signal, and its
    dual-appended audit event commit atomically.
    """
    eng = make_curation_engine()
    ensure_review_schema(eng)
    try:
        yield eng
    finally:
        eng.dispose()


def _seed_all_kinds_capture(
    capture: CorrectionSignalCapture,
    make_provenance: Callable[..., SignalProvenance],
) -> dict[str, str]:
    """Seed one signal of every kind through the REAL capture path (ids 1..6)."""
    payloads: dict[str, SignalPayload] = {
        "thumbs_up": ThumbsSignal(up=True, target_ref="doc-tu"),
        "thumbs_down": ThumbsSignal(up=False, target_ref="doc-td"),
        "edit_distance": DraftEditSignal(draft_ref="doc-ed", edit_ratio=0.5),
        "wrong_source": WrongSourceSignal(answer_ref="q-shared", citation_ref="cite-1"),
        "re_query": ReQuerySignal(original_ref="q-shared", requery_ref="q-2"),
        "approval_rejection": ApprovalRejectionSignal(approval_id="appr-1", tier=2, reason="risk"),
    }
    return {
        label: asyncio.run(capture.capture(make_provenance(), payload))
        for label, payload in payloads.items()
    }


@pytest.fixture
def seeded_proposals(
    review_engine: sa.Engine,
    frozen_config: str,
    make_provenance: Callable[..., SignalProvenance],
) -> list[StoredFixProposal]:
    """Genuine NE-065-produced proposals of every lane on the real sqlite seam.

    Seeds one signal of each of the six kinds through the REAL
    ``CorrectionSignalCapture``, runs one real ``CurationAgent`` pass, and
    returns the appended proposals — fixture-first: nothing is mocked past the
    sqlite seam, so the NE-066 review/diff tests operate on real proposals.
    """
    capture = CorrectionSignalCapture(review_engine)
    _seed_all_kinds_capture(capture, make_provenance)
    asyncio.run(CurationAgent(review_engine).run_once())
    return SqlAlchemyProposalStore(review_engine).read_proposals()


@pytest.fixture
def frozen_config() -> Iterator[str]:
    """Freeze a deterministic config_hash for the duration of a test."""
    reset_config_hash_for_testing()
    digest = freeze_config_hash(_DemoSettings())
    try:
        yield digest
    finally:
        reset_config_hash_for_testing()


def _make_provenance(
    *,
    surface: SignalSurface = SignalSurface.CONSOLE,
    principal_id: str = "user:ada@acme.com",
    session_id: str = "sess-1",
    turn_id: str | None = "turn-3",
    message_id: str | None = "msg-9",
) -> SignalProvenance:
    return SignalProvenance(
        principal=Principal(id=principal_id),
        surface=surface,
        session_id=session_id,
        turn_id=turn_id,
        message_id=message_id,
    )


@pytest.fixture
def make_provenance() -> Callable[..., SignalProvenance]:
    return _make_provenance
