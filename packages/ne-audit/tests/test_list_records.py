# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""``ne.audit.list_records`` — the additive, payload-authoritative filtered read.

Proves: newest-first (seq DESC), actor/action/time-window filters, the 1..=1000
limit clamp, and — the invariant that matters — that a tampered denormalized
COLUMN can HIDE a row from a column-filtered read but can NEVER surface forged
content (every returned value derives from the hash-covered payload).

Also proves ``list_records_anchored`` — the ``?seq=`` deep link's read: the
window CONTAINING an anchor seq (not the newest page), clamped at both ends of
the filtered set, with a server-side reachability verdict (found /
filtered_out / not_found + the chain head) so a miss is a stated fact, never a
silent empty highlight.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy import event

from ne.audit import (
    MAX_PAGE_ROWS,
    append_record,
    count_records,
    ensure_audit_schema,
    find_approval_decision,
    list_records,
    list_records_anchored,
    records_by_event_ids,
)
from ne.audit.store import ne_audit_log
from ne.schema import AuditEvent


def _seed(engine: sa.Engine, audit: Callable[..., AuditEvent]) -> None:
    with engine.begin() as conn:
        append_record(
            conn,
            audit(
                event_id="e1",
                actor="user:ada",
                action="api.approval.submitted",
                occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
            ),
        )
        append_record(
            conn,
            audit(
                event_id="e2",
                actor="user:bob",
                action="api.approval.unauthorized",
                occurred_at=datetime(2026, 2, 1, tzinfo=UTC),
            ),
        )
        append_record(
            conn,
            audit(
                event_id="e3",
                actor="user:ada",
                action="api.audit.unauthorized",
                occurred_at=datetime(2026, 3, 1, tzinfo=UTC),
            ),
        )


def test_newest_first_seq_desc(engine: sa.Engine, audit: Callable[..., AuditEvent]) -> None:
    _seed(engine, audit)
    result = list_records(engine)
    seqs = [r.seq for r in result]
    assert seqs == sorted(seqs, reverse=True)
    assert [r.event_id for r in result] == ["e3", "e2", "e1"]


def test_actor_filter(engine: sa.Engine, audit: Callable[..., AuditEvent]) -> None:
    _seed(engine, audit)
    result = list_records(engine, actor="user:ada")
    assert {r.event_id for r in result} == {"e1", "e3"}
    assert all(r.actor == "user:ada" for r in result)


def test_action_filter(engine: sa.Engine, audit: Callable[..., AuditEvent]) -> None:
    _seed(engine, audit)
    result = list_records(engine, action="api.approval.unauthorized")
    assert [r.event_id for r in result] == ["e2"]


def test_records_by_event_ids_exact_deduped_and_payload_authoritative(
    engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    _seed(engine, audit)
    result = records_by_event_ids(engine, ["missing", "e3", "e1", "e3"])
    assert [(row.event_id, row.actor) for row in result] == [
        ("e1", "user:ada"),
        ("e3", "user:ada"),
    ]


def test_time_window_filter(engine: sa.Engine, audit: Callable[..., AuditEvent]) -> None:
    _seed(engine, audit)
    result = list_records(
        engine,
        start=datetime(2026, 1, 15, tzinfo=UTC),
        end=datetime(2026, 2, 15, tzinfo=UTC),
    )
    assert [r.event_id for r in result] == ["e2"]


def test_limit_clamp_low(engine: sa.Engine, audit: Callable[..., AuditEvent]) -> None:
    _seed(engine, audit)
    # limit=0 clamps UP to 1 (never an unbounded/empty request).
    result = list_records(engine, limit=0)
    assert len(result) == 1
    assert result[0].event_id == "e3"  # still newest-first


def test_limit_clamp_high(engine: sa.Engine, audit: Callable[..., AuditEvent]) -> None:
    _seed(engine, audit)
    # limit=5000 clamps DOWN to 1000; with only 3 rows seeded, all 3 return, no error.
    result = list_records(engine, limit=5000)
    assert len(result) == 3


def test_before_seq_keyset_pages_are_disjoint_and_exhaustive(
    engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    _seed(engine, audit)
    # Newest-first, page 1: the two newest rows (e3, e2).
    page1 = list_records(engine, limit=2)
    assert [r.event_id for r in page1] == ["e3", "e2"]
    # Page 2, cursored strictly before page 1's tail: exactly the remaining row.
    page2 = list_records(engine, limit=2, before_seq=page1[-1].seq)
    assert [r.event_id for r in page2] == ["e1"]

    ids1 = {r.event_id for r in page1}
    ids2 = {r.event_id for r in page2}
    assert ids1.isdisjoint(ids2)
    assert ids1 | ids2 == {"e1", "e2", "e3"}


def test_count_records_matches_filters(engine: sa.Engine, audit: Callable[..., AuditEvent]) -> None:
    _seed(engine, audit)

    assert count_records(engine) == 3
    assert count_records(engine) == len(list_records(engine, limit=1000))

    assert count_records(engine, actor="user:ada") == 2
    assert count_records(engine, actor="user:ada") == len(
        list_records(engine, actor="user:ada", limit=1000)
    )

    window_start = datetime(2026, 1, 15, tzinfo=UTC)
    window_end = datetime(2026, 2, 15, tzinfo=UTC)
    assert count_records(engine, start=window_start, end=window_end) == 1
    assert count_records(engine, start=window_start, end=window_end) == len(
        list_records(engine, limit=1000, start=window_start, end=window_end)
    )


def test_payload_authoritative_tampered_column(
    engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    _seed(engine, audit)
    # Drop the append-only UPDATE trigger (test_chain_verify.py:23 precedent), then
    # forge the denormalized ``actor`` COLUMN of seq 1 (event e1, payload actor
    # 'user:ada').
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TRIGGER IF EXISTS ne_audit_log_no_update")
        conn.execute(
            sa.text("UPDATE ne_audit_log SET actor = :a WHERE seq = 1"),
            {"a": "forged"},
        )

    # The forged COLUMN value can HIDE the row from an actor= filter on the payload
    # actor ...
    ada = list_records(engine, actor="user:ada")
    assert "e1" not in {r.event_id for r in ada}

    # ... but the returned record.actor is still the PAYLOAD value, never 'forged'.
    forged = list_records(engine, actor="forged")
    assert [r.event_id for r in forged] == ["e1"]
    assert forged[0].actor == "user:ada"
    # And the raw hash-covered payload confirms it.
    assert forged[0].payload["actor"] == "user:ada"
    # Sanity: the forged string appears nowhere in any returned record's fields.
    for r in list_records(engine):
        assert r.actor != "forged"
        assert "forged" not in json.dumps(r.payload)


def test_approval_decision_lookup_is_exact_terminal_and_not_page_bounded(
    engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    with engine.begin() as conn:
        append_record(
            conn,
            audit(
                event_id="decision",
                action="approval.granted",
                resource="tool:crm.create_note",
                provenance={"approval_id": "req-1", "approver": "user:ada"},
            ),
        )
        # Exact-id collision with a terminal action must not match req-1.
        append_record(
            conn,
            audit(
                event_id="substring",
                action="approval.rejected",
                resource="tool:crm.create_note",
                provenance={"approval_id": "prefix-req-1-suffix"},
            ),
        )
        # More than the console poll's newest 25 records, including the same
        # exact approval id on actions that are NOT the terminal decision.
        for index in range(30):
            append_record(
                conn,
                audit(
                    event_id=f"later-{index}",
                    action="tool.execute" if index % 2 == 0 else "approval.payload_read",
                    resource="tool:crm.create_note",
                    provenance={"approval_id": "req-1"},
                ),
            )

    found = find_approval_decision(engine, "req-1")
    assert found is not None
    assert found.event_id == "decision"
    assert found.action == "approval.granted"
    assert found.payload["provenance"]["approval_id"] == "req-1"


def test_approval_decision_lookup_reports_exact_misses(
    engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    with engine.begin() as conn:
        append_record(
            conn,
            audit(
                event_id="near-match",
                action="approval.granted",
                provenance={"approval_id": "req-10"},
            ),
        )
    assert find_approval_decision(engine, "req-1") is None
    assert find_approval_decision(engine, "") is None


# ---------------------------------------------------------------------------
# list_records_anchored — the ?seq= deep link's read
# ---------------------------------------------------------------------------
def _seed_chain(engine: sa.Engine, audit: Callable[..., AuditEvent], n: int) -> None:
    """Append ``n`` rows: seq i carries event_id ``a{i}``; odd seqs are ada,
    even seqs are bob — so an actor filter thins the chain deterministically."""
    with engine.begin() as conn:
        for i in range(1, n + 1):
            append_record(
                conn,
                audit(
                    event_id=f"a{i}",
                    actor="user:ada" if i % 2 == 1 else "user:bob",
                    occurred_at=datetime(2026, 1, 1, i % 24, i % 60, tzinfo=UTC),
                ),
            )


def test_anchored_window_contains_a_seq_far_outside_the_newest_page(
    engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    # THE defect this read exists to fix: seq 10 is 20 rows older than the head,
    # so the newest page (limit 5) can never contain it...
    _seed_chain(engine, audit, 30)
    newest = list_records(engine, limit=5)
    assert 10 not in [r.seq for r in newest]

    # ...and the anchored read returns the page it sits in, mid-window.
    window, resolution = list_records_anchored(engine, anchor_seq=10, limit=5)
    assert [r.seq for r in window] == [12, 11, 10, 9, 8]
    assert resolution.seq == 10
    assert resolution.status == "found"
    assert resolution.head_seq == 30


def test_anchored_window_clamps_at_the_head(
    engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    _seed_chain(engine, audit, 30)
    window, resolution = list_records_anchored(engine, anchor_seq=30, limit=5)
    # No newer rows exist; the unused newer budget goes to the older side.
    assert [r.seq for r in window] == [30, 29, 28, 27, 26]
    assert resolution.status == "found"


def test_anchored_window_clamps_at_the_genesis_end(
    engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    _seed_chain(engine, audit, 30)
    window, resolution = list_records_anchored(engine, anchor_seq=1, limit=5)
    # Only the anchor exists on the older side; the rest of the window is the
    # nearest newer rows.
    assert [r.seq for r in window] == [5, 4, 3, 2, 1]
    assert resolution.status == "found"


def test_anchor_past_the_head_is_not_found_and_names_the_head(
    engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    _seed_chain(engine, audit, 30)
    window, resolution = list_records_anchored(engine, anchor_seq=999, limit=5)
    assert resolution.status == "not_found"
    assert resolution.seq == 999
    assert resolution.head_seq == 30
    # The screen stays useful: the window is the filtered rows nearest the
    # anchor position — here, the newest page.
    assert [r.seq for r in window] == [30, 29, 28, 27, 26]


def test_anchor_excluded_by_filters_is_filtered_out_not_not_found(
    engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    _seed_chain(engine, audit, 30)
    # Seq 10 is bob; the ada filter excludes it. The verdict must distinguish
    # "exists but your filters hide it" from "does not exist" — conflating them
    # states a falsehood about the chain.
    window, resolution = list_records_anchored(engine, anchor_seq=10, actor="user:ada", limit=5)
    assert resolution.status == "filtered_out"
    assert resolution.head_seq == 30
    # The window is still the ada rows around the anchor position (odd seqs),
    # contiguous in the FILTERED set.
    assert [r.seq for r in window] == [13, 11, 9, 7, 5]
    assert all(r.actor == "user:ada" for r in window)


def test_anchoring_never_widens_the_page_bound(
    engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    _seed_chain(engine, audit, 30)
    # The same 1..=MAX_PAGE_ROWS clamp as list_records: limit=0 clamps UP to 1,
    # and the one row is the anchor itself — never an unbounded window.
    window, resolution = list_records_anchored(engine, anchor_seq=10, limit=0)
    assert [r.seq for r in window] == [10]
    assert resolution.status == "found"


def test_anchored_limit_clamps_down_to_max_page_rows(
    engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    # The UPPER end of the same clamp, proven against a set that actually
    # exceeds it: MAX_PAGE_ROWS+5 rows exist, limit=5000 clamps DOWN, and the
    # window is exactly MAX_PAGE_ROWS contiguous rows positioned by the anchor
    # (the 5 rows past the window's newer edge are excluded — the clamp is
    # real, not just error-free).
    _seed_chain(engine, audit, MAX_PAGE_ROWS + 5)
    window, resolution = list_records_anchored(engine, anchor_seq=500, limit=5000)
    assert resolution.status == "found"
    assert resolution.head_seq == MAX_PAGE_ROWS + 5
    assert [r.seq for r in window] == list(range(MAX_PAGE_ROWS, 0, -1))


def test_anchored_window_of_a_single_filtered_row(
    engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    # Filters thin the set to EXACTLY ONE row — the anchor itself. The window
    # is that row alone (limit budget with nothing to spend it on), found.
    _seed_chain(engine, audit, 30)
    with engine.begin() as conn:
        append_record(
            conn,
            audit(
                event_id="c1",
                actor="user:carol",
                occurred_at=datetime(2026, 1, 2, tzinfo=UTC),
            ),
        )
    window, resolution = list_records_anchored(engine, anchor_seq=31, actor="user:carol", limit=5)
    assert [(r.seq, r.event_id) for r in window] == [(31, "c1")]
    assert resolution.status == "found"
    assert resolution.head_seq == 31


def _is_existence_probe(statement: str) -> bool:
    """The anchored read's existence probe: a ``ne_audit_log`` read with a seq
    EQUALITY and no ORDER BY (the window reads order+limit; the head read names
    its own table)."""
    return "ne_audit_log" in statement and "seq = " in statement and "ORDER BY" not in statement


def test_append_racing_the_anchored_read_is_not_found_never_filtered_out(
    tmp_path: Path, audit: Callable[..., AuditEvent]
) -> None:
    """THE cross-snapshot race, replayed deterministically: head is 30, the
    request asks for anchor 31 with NO filters, and a real append commits
    mid-read.

    The vulnerable statement order ran the window reads first, the existence
    probe after them, and the head read LAST — so under per-statement snapshots
    (Postgres READ COMMITTED; plain SQLite here) the probe saw the
    just-appended row 31 and the verdict came back ``filtered_out`` ("the
    applied filters exclude it") on a request that applied no filters. The fix
    is statement ORDER — head first, beyond-head decided from that head, probe
    never consulted for it — so this fixture fires the append the moment an
    existence probe is about to execute: any refactor of the read sequence
    that probes existence for a beyond-head anchor goes RED here with the
    false ``filtered_out``.
    """
    eng = sa.create_engine(f"sqlite:///{tmp_path / 'race.db'}")
    ensure_audit_schema(eng)
    _seed_chain(eng, audit, 30)

    # Harness-liveness control: the probe matcher MUST match the probe's real
    # compiled shape, or this fixture could go quietly inert under SQL drift.
    probe_sql = str(
        sa.select(ne_audit_log.c.seq)
        .where(ne_audit_log.c.seq == 31)
        .compile(eng, compile_kwargs={"literal_binds": True})
    )
    assert _is_existence_probe(probe_sql)
    window_sql = str(
        ne_audit_log.select()
        .where(ne_audit_log.c.seq <= 31)
        .order_by(ne_audit_log.c.seq.desc())
        .limit(5)
        .compile(eng, compile_kwargs={"literal_binds": True})
    )
    assert not _is_existence_probe(window_sql)

    fired: list[str] = []

    def _append_row_31() -> None:
        with eng.begin() as conn:
            append_record(conn, audit(event_id="a31", actor="user:ada"))

    @event.listens_for(eng, "before_cursor_execute")
    def _interleave(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool
    ) -> None:
        if not fired and _is_existence_probe(statement):
            fired.append(statement)  # once, and never re-entered by the append's own statements
            _append_row_31()

    try:
        window, resolution = list_records_anchored(eng, anchor_seq=31, limit=5)
    finally:
        event.remove(eng, "before_cursor_execute", _interleave)
        eng.dispose()

    # No filters were applied, so ``filtered_out`` would state a falsehood
    # about the chain in the exact field this read exists to make trustworthy.
    assert resolution.status == "not_found"
    # The head named is the one observed BEFORE any row statement — the old
    # order read it last and would name 31 here.
    assert resolution.head_seq == 30
    # The screen stays useful: the newest page as of that head.
    assert [r.seq for r in window] == [30, 29, 28, 27, 26]
