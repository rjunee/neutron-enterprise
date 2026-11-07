# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Deterministic SQL-math suite for the ne-api-computed digest/latency aggregations.

Direct INSERTs into ``ne_fix_proposals``/``ne_proposal_reviews`` with FIXED
timestamps over a raw tmp-path SQLite engine + ``ensure_review_schema``. Direct
seeding is legitimate here — these functions are READ-ONLY aggregation; the
governed write path is exercised through HTTP in test_steward_api.py.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import sqlalchemy as sa

from ne.api.steward_metrics import (
    compute_digest,
    compute_latency,
    folded_status_map,
    iso_week_start,
)
from ne.flywheel.proposals import ne_fix_proposals
from ne.flywheel.review import ensure_review_schema, ne_proposal_reviews


def _engine(tmp_path: Path) -> sa.Engine:
    engine = sa.create_engine(f"sqlite:///{tmp_path}/metrics.db")
    ensure_review_schema(engine)
    return engine


def _insert_proposal(
    engine: sa.Engine, proposal_id: str, *, lane: str, created_at: datetime
) -> None:
    with engine.begin() as conn:
        conn.execute(
            ne_fix_proposals.insert().values(
                proposal_id=proposal_id,
                kind="fix",
                lane=lane,
                cluster_id="cl",
                status="proposed",
                config_hash="h",
                summary="s",
                confidence=0.5,
                priority="medium",
                evidence={"event_ids": [], "signal_ids": []},
                payload={},
                created_at=created_at,
            )
        )


def _insert_review(
    engine: sa.Engine,
    *,
    review_id: str,
    proposal_id: str,
    action: str,
    to_status: str,
    created_at: datetime,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            ne_proposal_reviews.insert().values(
                review_id=review_id,
                proposal_id=proposal_id,
                action=action,
                from_status="proposed",
                to_status=to_status,
                actor="user:s",
                surface="console",
                session_id="sid",
                rejection_code=None,
                note=None,
                applied_ref=None,
                config_hash="h",
                created_at=created_at,
            )
        )


def test_iso_week_start() -> None:
    # 2026-07-08 is a Wednesday → its Monday is 2026-07-06.
    wed = datetime(2026, 7, 8, 13, 30, tzinfo=UTC)
    assert iso_week_start(wed) == date(2026, 7, 6)
    # A Monday 00:00 UTC maps to itself.
    mon = datetime(2026, 7, 6, 0, 0, tzinfo=UTC)
    assert iso_week_start(mon) == date(2026, 7, 6)


def test_digest_window_boundaries_inclusive_exclusive(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    week_start = date(2026, 7, 6)
    start = datetime(2026, 7, 6, 0, 0, tzinfo=UTC)
    _insert_proposal(engine, "p_start", lane="content", created_at=start)  # counted (>=)
    _insert_proposal(
        engine,
        "p_end_minus",
        lane="content",
        created_at=start + timedelta(days=7) - timedelta(microseconds=1),
    )  # counted (< end)
    _insert_proposal(
        engine, "p_end", lane="content", created_at=start + timedelta(days=7)
    )  # excluded (== end)
    _insert_proposal(
        engine, "p_before", lane="content", created_at=start - timedelta(microseconds=1)
    )  # excluded (< start)

    now = datetime(2026, 7, 20, tzinfo=UTC)
    digest = compute_digest(engine, week_start=week_start, now=now)
    assert digest.proposed == 2
    assert digest.by_lane[0].lane == "content"
    assert digest.by_lane[0].proposed == 2
    assert digest.week_start == week_start
    assert digest.week_end == week_start + timedelta(days=7)


def test_digest_lane_grouping_and_action_mapping(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    week_start = date(2026, 7, 6)
    start = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)

    _insert_proposal(engine, "c1", lane="content", created_at=start)
    _insert_proposal(engine, "c2", lane="content", created_at=start)
    _insert_proposal(engine, "r1", lane="retrieval", created_at=start)
    _insert_proposal(engine, "s1", lane="schema", created_at=start)
    # policy proposal created OUTSIDE the window (its reject IS inside) → only rejected counts.
    _insert_proposal(engine, "pol1", lane="policy", created_at=start - timedelta(days=30))

    _insert_review(
        engine,
        review_id="rv1",
        proposal_id="c1",
        action="approve",
        to_status="approved",
        created_at=start,
    )
    _insert_review(
        engine,
        review_id="rv2",
        proposal_id="c1",
        action="apply",
        to_status="applied",
        created_at=start,
    )
    _insert_review(
        engine,
        review_id="rv3",
        proposal_id="pol1",
        action="reject",
        to_status="rejected",
        created_at=start,
    )

    now = datetime(2026, 7, 20, tzinfo=UTC)
    digest = compute_digest(engine, week_start=week_start, now=now)

    assert digest.proposed == 4  # c1, c2, r1, s1 (pol1 is out of window)
    assert digest.approved == 1
    assert digest.applied == 1
    assert digest.rejected == 1

    lanes = {ld.lane: ld for ld in digest.by_lane}
    # FixLane order: content, retrieval, schema, policy. Only lanes with activity.
    assert [ld.lane for ld in digest.by_lane] == ["content", "retrieval", "schema", "policy"]
    assert lanes["content"].proposed == 2
    assert lanes["content"].approved == 1
    assert lanes["content"].applied == 1
    assert lanes["retrieval"].proposed == 1
    assert lanes["schema"].proposed == 1
    assert lanes["policy"].proposed == 0
    assert lanes["policy"].rejected == 1


def test_digest_empty_week_is_zero_and_no_lanes(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    now = datetime(2026, 7, 20, tzinfo=UTC)
    digest = compute_digest(engine, week_start=date(2020, 1, 6), now=now)
    assert digest.proposed == digest.approved == digest.rejected == digest.applied == 0
    assert digest.by_lane == ()


def test_latency_median_p90_nearest_rank_exact(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    base = datetime(2026, 7, 6, 0, 0, tzinfo=UTC)
    # 10 proposals with waits 10, 20, ..., 100 seconds (first-review only).
    for i in range(1, 11):
        pid = f"p{i}"
        _insert_proposal(engine, pid, lane="content", created_at=base)
        # A later "apply" review has a LATER created_at; only the earliest (approve) counts.
        _insert_review(
            engine,
            review_id=f"a{i}",
            proposal_id=pid,
            action="approve",
            to_status="approved",
            created_at=base + timedelta(seconds=10 * i),
        )
        _insert_review(
            engine,
            review_id=f"b{i}",
            proposal_id=pid,
            action="apply",
            to_status="applied",
            created_at=base + timedelta(seconds=10 * i + 999),
        )

    now = datetime(2026, 7, 20, tzinfo=UTC)
    summary = compute_latency(engine, now=now)
    assert summary.decided_count == 10
    assert summary.median_wait_s == 55.0
    # nearest-rank p90: ceil(0.9 * 10) - 1 = index 8 → 90.0.
    assert summary.p90_wait_s == 90.0
    assert summary.pending_count == 0
    assert summary.oldest_pending_age_s is None


def test_latency_single_decided(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    base = datetime(2026, 7, 6, 0, 0, tzinfo=UTC)
    _insert_proposal(engine, "only", lane="content", created_at=base)
    _insert_review(
        engine,
        review_id="rv",
        proposal_id="only",
        action="approve",
        to_status="approved",
        created_at=base + timedelta(seconds=42),
    )
    summary = compute_latency(engine, now=datetime(2026, 7, 20, tzinfo=UTC))
    assert summary.decided_count == 1
    assert summary.median_wait_s == 42.0
    assert summary.p90_wait_s == 42.0


def test_latency_pending_and_oldest(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    base = datetime(2026, 7, 6, 0, 0, tzinfo=UTC)
    # 2 pending proposals (no review) + 1 decided.
    _insert_proposal(engine, "pend_old", lane="content", created_at=base)
    _insert_proposal(engine, "pend_new", lane="content", created_at=base + timedelta(hours=1))
    _insert_proposal(engine, "done", lane="content", created_at=base)
    _insert_review(
        engine,
        review_id="rv",
        proposal_id="done",
        action="approve",
        to_status="approved",
        created_at=base + timedelta(seconds=5),
    )

    now = base + timedelta(hours=2)
    summary = compute_latency(engine, now=now)
    assert summary.pending_count == 2
    # oldest pending is pend_old at base → age is 2 hours = 7200s.
    assert summary.oldest_pending_age_s == 7200.0
    assert summary.decided_count == 1


def test_latency_empty_store_all_null(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    summary = compute_latency(engine, now=datetime(2026, 7, 20, tzinfo=UTC))
    assert summary.median_wait_s is None
    assert summary.p90_wait_s is None
    assert summary.decided_count == 0
    assert summary.pending_count == 0
    assert summary.oldest_pending_age_s is None


def test_folded_status_map_single_query_semantics(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    base = datetime(2026, 7, 6, 0, 0, tzinfo=UTC)
    _insert_proposal(engine, "none", lane="content", created_at=base)
    _insert_proposal(engine, "appr", lane="content", created_at=base)
    _insert_proposal(engine, "rej", lane="content", created_at=base)

    _insert_review(
        engine,
        review_id="a",
        proposal_id="appr",
        action="approve",
        to_status="approved",
        created_at=base,
    )
    # approve then reject on the same proposal → last row (max id) wins → rejected.
    _insert_review(
        engine,
        review_id="r1",
        proposal_id="rej",
        action="approve",
        to_status="approved",
        created_at=base,
    )
    _insert_review(
        engine,
        review_id="r2",
        proposal_id="rej",
        action="reject",
        to_status="rejected",
        created_at=base + timedelta(seconds=1),
    )

    fold = folded_status_map(engine)
    assert "none" not in fold  # no review row → absent (folds to 'proposed' at the caller)
    assert fold["appr"] == "approved"
    assert fold["rej"] == "rejected"
