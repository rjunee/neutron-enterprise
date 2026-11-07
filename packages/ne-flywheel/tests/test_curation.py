# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Acceptance spine for the NE-065 curation engine (real sqlite seam).

Seeds correction signals through the real NE-064 capture path, runs the curation
agent, and asserts: deterministic clusters, correct 4-lane routing, typed
proposals with exact evidence, a golden-set candidate for the retrieval cluster,
that the proposal store is the ONLY business write, the dual-appended audit
event, idempotent re-runs, fail-closed atomicity, the LLM seam, and batch
limits. asyncio.run convention (no pytest-asyncio).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest
import sqlalchemy as sa

from ne.audit import verify_chain
from ne.audit.store import ne_audit_log
from ne.events.outbox import ne_outbox_events
from ne.flywheel.curation import CurationAgent, TemplateSummaryComposer, ne_curation_cursor
from ne.flywheel.errors import CurationAuditError
from ne.flywheel.lanes import FixLane
from ne.flywheel.proposals import ProposalKind, SqlAlchemyProposalStore, ne_fix_proposals
from ne.flywheel.signals import (
    ApprovalRejectionSignal,
    DraftEditSignal,
    ReQuerySignal,
    SignalPayload,
    ThumbsSignal,
    WrongSourceSignal,
)
from ne.flywheel.store import SqlAlchemyCorrectionSignalStore, ne_correction_signals


def _count(engine: sa.Engine, table: sa.Table) -> int:
    with engine.connect() as conn:
        return int(conn.execute(sa.select(sa.func.count()).select_from(table)).scalar_one())


def _audit_outbox_count(engine: sa.Engine) -> int:
    """Number of ``audit``-typed outbox events (= number of curation runs).

    The NE-064 capture path writes ``correction_signal`` events to the SAME
    outbox, so total outbox rows include the seeded signals; the curation run's
    ``curation.run`` event is the only ``audit``-typed row.
    """
    with engine.connect() as conn:
        return int(
            conn.execute(
                sa.select(sa.func.count())
                .select_from(ne_outbox_events)
                .where(ne_outbox_events.c.event_type == "audit")
            ).scalar_one()
        )


def _seed_all_kinds(seed_signal: Callable[..., str]) -> dict[str, str]:
    """Seed one signal of every kind; wrong_source + re_query share a ref.

    Returns a ``{label: event_id}`` map. Seed order fixes store ids 1..6.
    """
    return {
        "thumbs_up": seed_signal(ThumbsSignal(up=True, target_ref="doc-tu")),
        "thumbs_down": seed_signal(ThumbsSignal(up=False, target_ref="doc-td")),
        "edit_distance": seed_signal(DraftEditSignal(draft_ref="doc-ed", edit_ratio=0.5)),
        "wrong_source": seed_signal(
            WrongSourceSignal(answer_ref="q-shared", citation_ref="cite-1")
        ),
        "re_query": seed_signal(ReQuerySignal(original_ref="q-shared", requery_ref="q-2")),
        "approval_rejection": seed_signal(
            ApprovalRejectionSignal(approval_id="appr-1", tier=2, reason="risk")
        ),
    }


def test_curation_run_end_to_end(
    curation_engine: sa.Engine,
    frozen_config: str,
    seed_signal: Callable[..., str],
) -> None:
    events = _seed_all_kinds(seed_signal)
    agent = CurationAgent(curation_engine)

    outcome = asyncio.run(agent.run_once())

    assert outcome.signals_read == 6
    assert outcome.clusters == 4
    assert outcome.golden_candidates == 1
    assert outcome.lane_counts == {"retrieval": 1, "policy": 1, "content": 2}
    assert len(outcome.proposal_ids) == 5

    store = SqlAlchemyProposalStore(curation_engine)
    proposals = store.read_proposals()
    assert len(proposals) == 5
    assert {p.config_hash for p in proposals} == {frozen_config}

    by_lane_kind = {(p.lane, p.kind): p for p in proposals}

    policy = by_lane_kind[(FixLane.POLICY.value, ProposalKind.FIX.value)]
    assert policy.evidence_signal_ids == (6,)
    assert policy.evidence_event_ids == (events["approval_rejection"],)

    retrieval = by_lane_kind[(FixLane.RETRIEVAL.value, ProposalKind.FIX.value)]
    assert retrieval.evidence_signal_ids == (4, 5)
    assert retrieval.evidence_event_ids == (
        events["wrong_source"],
        events["re_query"],
    )

    golden = by_lane_kind[(FixLane.RETRIEVAL.value, ProposalKind.GOLDEN_SET_CANDIDATE.value)]
    assert golden.payload["question_ref"] == "q-shared"
    assert golden.payload["wrong_source_refs"] == ["cite-1"]
    assert golden.payload["expected_source_refs"] == []
    assert golden.payload["evidence_event_ids"] == [
        events["wrong_source"],
        events["re_query"],
    ]

    # Two content proposals (thumbs_down id=2, edit_distance id=3), separate clusters.
    content = [
        p for p in proposals if p.lane == FixLane.CONTENT.value and p.kind == ProposalKind.FIX.value
    ]
    assert len(content) == 2
    assert {p.evidence_signal_ids for p in content} == {(2,), (3,)}


def test_proposal_store_is_the_only_write(
    curation_engine: sa.Engine,
    frozen_config: str,
    seed_signal: Callable[..., str],
) -> None:
    _seed_all_kinds(seed_signal)
    signal_store = SqlAlchemyCorrectionSignalStore(curation_engine)
    before = [s.model_dump() for s in signal_store.read_signals(limit=1000)]

    agent = CurationAgent(curation_engine)
    asyncio.run(agent.run_once())

    after = [s.model_dump() for s in signal_store.read_signals(limit=1000)]
    # Signal rows are byte-identical — the curation run touched no signal.
    assert before == after

    # Signals unchanged; the run's ONLY new rows are proposals, cursor, one
    # curation.run outbox event, and one audit-chain record.
    assert _count(curation_engine, ne_correction_signals) == 6  # unchanged
    assert _count(curation_engine, ne_fix_proposals) == 5
    assert _count(curation_engine, ne_curation_cursor) == 1
    assert _count(curation_engine, ne_audit_log) == 1
    # 6 seeded correction_signal events + exactly 1 curation.run audit event.
    assert _count(curation_engine, ne_outbox_events) == 7
    assert _audit_outbox_count(curation_engine) == 1

    with curation_engine.connect() as conn:
        row = conn.execute(
            sa.select(ne_outbox_events).where(ne_outbox_events.c.event_type == "audit")
        ).one()
    assert row.payload["action"] == "curation.run"


def test_curation_run_emits_dual_appended_audit_event(
    curation_engine: sa.Engine,
    frozen_config: str,
    seed_signal: Callable[..., str],
) -> None:
    _seed_all_kinds(seed_signal)
    agent = CurationAgent(curation_engine)
    asyncio.run(agent.run_once())

    # In the outbox (the one audit-typed event among the seeded signal events).
    with curation_engine.connect() as conn:
        outbox_rows = conn.execute(
            sa.select(ne_outbox_events).where(ne_outbox_events.c.event_type == "audit")
        ).all()
        chain_rows = conn.execute(sa.select(ne_audit_log)).all()
    assert len(outbox_rows) == 1
    outbox_event_id = outbox_rows[0].event_id

    # In the tamper-evident chain, SAME event_id, action + config_hash stamped.
    assert len(chain_rows) == 1
    assert chain_rows[0].event_id == outbox_event_id
    assert chain_rows[0].action == "curation.run"
    assert chain_rows[0].config_hash == frozen_config

    verification = verify_chain(curation_engine)
    assert verification.valid
    assert verification.checked == 1


def test_second_run_is_idempotent_and_cursor_advances(
    curation_engine: sa.Engine,
    frozen_config: str,
    seed_signal: Callable[..., str],
) -> None:
    _seed_all_kinds(seed_signal)
    agent = CurationAgent(curation_engine)

    first = asyncio.run(agent.run_once())
    store = SqlAlchemyProposalStore(curation_engine)
    proposals_after_first = len(store.read_proposals())

    second = asyncio.run(agent.run_once())
    assert second.signals_read == 0
    assert second.clusters == 0
    assert second.proposal_ids == ()
    # No new proposals; deterministic ids + SAVEPOINT idempotency.
    assert len(store.read_proposals()) == proposals_after_first
    # Cursor unchanged in value across the empty run.
    assert second.cursor_start == first.cursor_end == second.cursor_end

    # Both runs still audited: two curation.run audit events.
    assert _audit_outbox_count(curation_engine) == 2


def test_crash_atomicity_fail_closed(
    curation_engine: sa.Engine,
    frozen_config: str,
    seed_signal: Callable[..., str],
) -> None:
    _seed_all_kinds(seed_signal)
    # Break the audit chain so append_record raises inside the run's transaction.
    with curation_engine.begin() as conn:
        conn.execute(sa.text("DROP TABLE ne_audit_head"))

    outbox_before = _count(curation_engine, ne_outbox_events)  # the 6 seeded signals
    agent = CurationAgent(curation_engine)
    with pytest.raises(CurationAuditError):
        asyncio.run(agent.run_once())

    # Whole-transaction rollback: no proposals, no cursor advance, and the run
    # added NO outbox event (only the pre-existing seeded signal events remain).
    assert _count(curation_engine, ne_fix_proposals) == 0
    assert _count(curation_engine, ne_curation_cursor) == 0
    assert _count(curation_engine, ne_outbox_events) == outbox_before
    assert _audit_outbox_count(curation_engine) == 0


class _ScriptedComposer:
    def __init__(self, sentinel: str) -> None:
        self._sentinel = sentinel

    def compose(self, cluster: object, lane: object) -> str:
        return self._sentinel


def test_scripted_summary_composer_seam(
    curation_engine: sa.Engine,
    frozen_config: str,
    seed_signal: Callable[..., str],
) -> None:
    _seed_all_kinds(seed_signal)
    sentinel = "SENTINEL-COMPOSER-OUTPUT"
    agent = CurationAgent(curation_engine, composer=_ScriptedComposer(sentinel))
    asyncio.run(agent.run_once())

    store = SqlAlchemyProposalStore(curation_engine)
    fix_summaries = {p.summary for p in store.read_proposals() if p.kind == ProposalKind.FIX.value}
    assert fix_summaries == {sentinel}


def test_default_template_composer_is_deterministic(
    curation_engine: sa.Engine,
    frozen_config: str,
    seed_signal: Callable[..., str],
    make_provenance: Callable[..., object],
) -> None:
    # Two identical runs on FRESH engines yield identical (ref-based) SUMMARY text.
    # (proposal_ids are content-addressed over per-capture event uuids and so
    # differ across independent captures — that is correct; the TEMPLATE output
    # is what must be deterministic.)
    from ne.flywheel.capture import CorrectionSignalCapture
    from ne.flywheel.curation import ensure_curation_schema

    def _run(engine: sa.Engine) -> list[str]:
        capture = CorrectionSignalCapture(engine)
        payloads: list[SignalPayload] = [
            ThumbsSignal(up=False, target_ref="doc-td"),
            WrongSourceSignal(answer_ref="q-shared", citation_ref="cite-1"),
            ReQuerySignal(original_ref="q-shared", requery_ref="q-2"),
        ]
        for payload in payloads:
            asyncio.run(capture.capture(make_provenance(), payload))  # type: ignore[arg-type]
        agent = CurationAgent(engine, composer=TemplateSummaryComposer())
        asyncio.run(agent.run_once())
        store = SqlAlchemyProposalStore(engine)
        return sorted(p.summary for p in store.read_proposals())

    first = _run(curation_engine)

    second_engine = sa.create_engine("sqlite://")
    ensure_curation_schema(second_engine)
    try:
        second = _run(second_engine)
    finally:
        second_engine.dispose()

    assert first == second


def test_batch_limit_respected(
    curation_engine: sa.Engine,
    frozen_config: str,
    seed_signal: Callable[..., str],
) -> None:
    for i in range(8):
        seed_signal(ThumbsSignal(up=False, target_ref=f"doc-{i}"))

    agent = CurationAgent(curation_engine, batch_limit=5)
    first = asyncio.run(agent.run_once())
    assert first.signals_read == 5

    second = asyncio.run(agent.run_once())
    assert second.signals_read == 3
    assert second.cursor_start == first.cursor_end
