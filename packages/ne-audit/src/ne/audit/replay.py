# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The replay query API — "why did the system say X on date D?" (SPEC §4).

Given a decision/answer id, a trace, a resource, and/or a time window, return the
``config_hash`` in force, the retrieved context IDs, and the producing events in
chain order. The ``config_hash`` is the SPEC §4 answer to "why did the system say
X on March 3" (it pins the exact model/retrieval/policy/prompt config); the
context IDs plus the event list reconstruct the decision's provenance.

PAYLOAD-AUTHORITATIVE READ: the denormalized columns are used ONLY inside WHERE
clauses for indexed selection; every value RETURNED (``config_hash``, ``actor``,
``outcome``, ``occurred_at``, …) derives from the hash-covered ``payload`` (via
:func:`ne.audit.store._record_from_row`). So a tampered filter column can at worst
hide or mis-select a row — which :func:`ne.audit.verify.verify_chain` now detects
and names — but can never cause replay to return forged content. The ``event_id``
pivot follows the same rule: the trace it pivots on is read from the anchor's
hash-covered ``payload`` (not the tamperable ``trace_id`` COLUMN), so a swapped
trace_id column cannot redirect the pivot to a forged trace.

DECIDED CONVENTION (NE-022/NE-033 will stamp it): retrieval/decision events carry
their retrieved chunk/context IDs in ``provenance['context_ids']`` as a
comma-separated string. Because ``ne.schema`` provenance values are typed
``str | ProvenanceEntry`` and a ProvenanceEntry round-trips through JSON as
``{"key": ..., "value": ...}``, this module accepts BOTH a plain string and that
dict shape.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

import sqlalchemy as sa

from ne.audit.store import AuditRecord, _record_from_row, ne_audit_head, ne_audit_log

CONTEXT_IDS_KEY = "context_ids"

# The gate's terminal human-decision vocabulary. This is intentionally narrower
# than every event that can carry the same approval_id (payload reads and the
# later tool.execute join both do): a receipt links to the decision, never to a
# subsequent use of that decision.
_APPROVAL_DECISION_ACTIONS = ("approval.granted", "approval.rejected")


@dataclass(frozen=True)
class ReplayResult:
    """The provenance of a decision: its config, retrieved context, and events."""

    events: tuple[AuditRecord, ...]
    config_hashes: tuple[str, ...]
    context_ids: tuple[str, ...]


def _provenance_context_ids(payload: dict[str, Any]) -> list[str]:
    provenance = payload.get("provenance")
    if not isinstance(provenance, dict):
        return []
    raw = provenance.get(CONTEXT_IDS_KEY)
    if isinstance(raw, dict):
        # ProvenanceEntry-shaped {"key": ..., "value": ...} after JSON round-trip.
        raw = raw.get("value")
    if not isinstance(raw, str) or not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _rows_to_result(rows: Sequence[sa.Row[Any]]) -> ReplayResult:
    records = tuple(_record_from_row(row) for row in rows)
    config_hashes = tuple(sorted({r.config_hash for r in records}))
    seen: dict[str, None] = {}
    for r in records:
        for cid in _provenance_context_ids(r.payload):
            if cid not in seen:
                seen[cid] = None
    return ReplayResult(events=records, config_hashes=config_hashes, context_ids=tuple(seen))


def find_approval_decision(engine: sa.Engine, request_id: str) -> AuditRecord | None:
    """Return the terminal audit record for one exact approval request id.

    Both predicates read the hash-covered JSON payload itself: the action must
    be the gate's terminal ``approval.granted``/``approval.rejected`` action AND
    ``provenance.approval_id`` must equal ``request_id``. A substring, an API
    echo, a payload read, or a later ``tool.execute`` therefore cannot match.
    The query is independent of the newest-page limit and returns at most one
    row, newest-first only as a defensive tie-breaker for malformed history.

    ``ProvenanceEntry``-shaped values are accepted alongside the normal plain
    string, matching replay's existing provenance compatibility discipline.
    An empty request id resolves to nothing rather than matching malformed data.
    """
    if request_id == "":
        return None

    payload = ne_audit_log.c.payload
    payload_action = payload["action"].as_string()
    plain_id = payload[("provenance", "approval_id")].as_string()
    entry_id = payload[("provenance", "approval_id", "value")].as_string()
    stmt = (
        ne_audit_log.select()
        .where(
            sa.and_(
                payload_action.in_(_APPROVAL_DECISION_ACTIONS),
                sa.or_(plain_id == request_id, entry_id == request_id),
            )
        )
        .order_by(ne_audit_log.c.seq.desc())
        .limit(1)
    )
    with engine.connect() as conn:
        row = conn.execute(stmt).first()
    return _record_from_row(row) if row is not None else None


def replay(
    engine: sa.Engine,
    *,
    trace_id: str | None = None,
    event_id: str | None = None,
    resource: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> ReplayResult:
    """Answer "why did the system say X on date D?".

    Selection (at least one selector required, else :class:`ValueError`):

    * ``event_id`` — that record, plus (if it carries a ``trace_id``) every record
      sharing that trace — pivoting from a single decision to its whole trace;
    * ``trace_id`` — every record on that trace;
    * ``resource`` and/or a ``start``/``end`` window — matching records.

    An empty selection returns an empty :class:`ReplayResult` (not an error).
    """
    if event_id is None and trace_id is None and resource is None and start is None and end is None:
        raise ValueError(
            "replay() requires at least one selector: event_id, trace_id, resource, "
            "or a start/end time window"
        )

    with engine.connect() as conn:
        if event_id is not None:
            anchor = conn.execute(
                ne_audit_log.select().where(ne_audit_log.c.event_id == event_id)
            ).first()
            if anchor is None:
                return ReplayResult(events=(), config_hashes=(), context_ids=())
            # Pivot on the hash-covered payload trace_id, not the tamperable column.
            payload_trace = dict(anchor.payload).get("trace_id")
            pivot_trace = (
                payload_trace if isinstance(payload_trace, str) and payload_trace else None
            )
            if pivot_trace:
                rows = conn.execute(
                    ne_audit_log.select()
                    .where(ne_audit_log.c.trace_id == pivot_trace)
                    .order_by(ne_audit_log.c.seq)
                ).all()
            else:
                rows = [anchor]
            return _rows_to_result(rows)

        clauses = []
        if trace_id is not None:
            clauses.append(ne_audit_log.c.trace_id == trace_id)
        if resource is not None:
            clauses.append(ne_audit_log.c.resource == resource)
        if start is not None:
            clauses.append(ne_audit_log.c.occurred_at >= start)
        if end is not None:
            clauses.append(ne_audit_log.c.occurred_at <= end)

        rows = conn.execute(
            ne_audit_log.select().where(sa.and_(*clauses)).order_by(ne_audit_log.c.seq)
        ).all()
        return _rows_to_result(rows)


#: The hard per-call page ceiling ``list_records`` clamps ``limit`` to.
#:
#: EXPORTED because a streaming caller must size its batches against the SAME
#: bound: a batch larger than this silently comes back short, and a caller that
#: reads "fewer rows than I asked for" as "end of results" then stops early. That
#: is precisely how NE-AUDIT-EXPORT shipped a CSV 100 rows short while its own
#: preamble reported completeness. Derive the ceiling from here; never restate it.
MAX_PAGE_ROWS = 1000


def _filter_clauses(
    actor: str | None,
    action: str | None,
    start: datetime | None,
    end: datetime | None,
) -> list[Any]:
    """The actor/action/time-window WHERE clauses shared by :func:`list_records`
    and :func:`count_records` — kept as ONE builder so the two reads can never
    silently drift apart (an export's "how many total" and "which rows" must
    agree on exactly the same filter semantics)."""
    clauses: list[Any] = []
    if actor is not None:
        clauses.append(ne_audit_log.c.actor == actor)
    if action is not None:
        clauses.append(ne_audit_log.c.action == action)
    if start is not None:
        clauses.append(ne_audit_log.c.occurred_at >= start)
    if end is not None:
        clauses.append(ne_audit_log.c.occurred_at <= end)
    return clauses


def list_records(
    engine: sa.Engine,
    *,
    actor: str | None = None,
    action: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 100,
    before_seq: int | None = None,
) -> tuple[AuditRecord, ...]:
    """List audit records filtered by actor/action/time window, newest-first.

    The read seam behind ``ne-api``'s ``GET /v1/audit/events`` (ne-api spec R1).
    ``replay()`` answers provenance-by-trace; this answers the ops-console's
    "show me events by actor/action in a window" filter, which ``replay()`` does
    not accept.

    PAYLOAD-AUTHORITATIVE READ (identical discipline to :func:`replay`): the
    denormalized columns (``actor``, ``action``, ``occurred_at``) are used ONLY
    inside the WHERE clause for indexed selection; EVERY value RETURNED derives
    from the hash-covered ``payload`` via :func:`ne.audit.store._record_from_row`.
    So a tampered filter column can at worst hide or mis-select a row — which
    :func:`ne.audit.verify.verify_chain` separately detects and names — but can
    never cause this read to surface forged content.

    Ordered ``seq`` DESC (newest-first, the ops-console default). ``limit`` is
    clamped to ``1..=MAX_PAGE_ROWS`` so a caller can never request an unbounded
    page. This
    is a READ-ONLY helper: it never touches the write path, the head, or the chain.

    ``before_seq`` — an OPTIONAL keyset cursor for streaming exports (additive):
    when set, restricts the read to ``seq < before_seq`` (STRICT less-than), so a
    caller can page strictly-older rows across repeated calls with no overlap and
    no gap (``before_seq=page[-1].seq`` on the next call). Default ``None``
    preserves every existing caller's behavior byte-for-byte.
    """
    clamped = max(1, min(int(limit), MAX_PAGE_ROWS))
    clauses = _filter_clauses(actor, action, start, end)
    if before_seq is not None:
        clauses.append(ne_audit_log.c.seq < before_seq)

    stmt = ne_audit_log.select()
    if clauses:
        stmt = stmt.where(sa.and_(*clauses))
    stmt = stmt.order_by(ne_audit_log.c.seq.desc()).limit(clamped)
    with engine.connect() as conn:
        rows = conn.execute(stmt).all()
    return tuple(_record_from_row(row) for row in rows)


def records_by_event_ids(engine: sa.Engine, event_ids: Sequence[str]) -> tuple[AuditRecord, ...]:
    """Return the exact existing records named by ``event_ids``.

    This bounded lookup supports reference-bound secondary reads such as human
    actor attribution.  The caller supplies server-issued event identifiers,
    never actors.  As with every replay read, returned actors derive from the
    hash-covered payload through :func:`_record_from_row`; denormalized columns
    are selection-only. Unknown identifiers are absent and duplicates collapse.
    """
    ids = list(dict.fromkeys(event_ids))
    if not ids:
        return ()
    with engine.connect() as conn:
        rows = conn.execute(
            ne_audit_log.select()
            .where(ne_audit_log.c.event_id.in_(ids))
            .order_by(ne_audit_log.c.seq)
        ).all()
    return tuple(_record_from_row(row) for row in rows)


#: The server's verdict on an anchored read — see :class:`AnchorResolution`.
AnchorStatus = Literal["found", "filtered_out", "not_found"]


@dataclass(frozen=True)
class AnchorResolution:
    """The SERVER's determination of whether an anchored entry was reachable.

    ``status`` is the reachability verdict a console renders VERBATIM (C7 —
    never inferred client-side from row counts):

    * ``found`` — a row with ``seq == seq`` exists in the FILTERED set and is in
      the returned window;
    * ``filtered_out`` — the row exists in the chain but the caller's
      actor/action/time filters exclude it;
    * ``not_found`` — no row with that seq exists in the chain at all (past the
      head, or absent from a chain whose seqs are dense — the latter is itself
      a fact worth stating loudly, since a dense chain only loses an in-range
      seq to tampering or destruction, both of which ``verify_chain`` names).

    ``head_seq`` is the chain head observed at the START of the read (see
    CONCURRENCY in :func:`list_records_anchored`), so a miss can be stated
    precisely ("the log ends at entry N") instead of generically.
    """

    seq: int
    status: AnchorStatus
    head_seq: int


def list_records_anchored(
    engine: sa.Engine,
    *,
    anchor_seq: int,
    actor: str | None = None,
    action: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 100,
) -> tuple[tuple[AuditRecord, ...], AnchorResolution]:
    """The window of the filtered set CONTAINING ``anchor_seq`` — not the newest page.

    The read behind ``GET /v1/audit/events?anchor_seq=`` (the ``?seq=`` deep
    link's fetch). :func:`list_records` can only ever return the most recent
    page, so a deep link to an entry older than one page was unreachable by any
    request the API accepted — and the console's highlight missed silently.

    WINDOW SHAPE: the same filtered, ``seq`` DESC sequence :func:`list_records`
    pages, positioned so the anchor sits mid-window (editor scroll-to-line
    semantics): up to ``(limit-1)//2`` rows newer than the anchor, the rest the
    anchor and older — clamped at either end of the filtered set, always
    contiguous IN THAT SET, length ≤ ``limit`` (clamped to ``MAX_PAGE_ROWS``
    exactly like :func:`list_records` — anchoring never widens the page bound).
    When the anchor is not in the filtered set the window is the filtered rows
    NEAREST the anchor position (for a seq past the head: the newest page), so
    the screen stays useful while the resolution states the miss.

    PAGING FROM HERE keeps :func:`list_records`'s keyset discipline: the
    window's own boundary seqs are the cursors (``before_seq=window[-1].seq``
    ages; a newer-side cursor is the symmetric ``seq >`` read this function
    already performs). No new paging primitive exists to drift.

    AUTHORIZATION: this function adds NO authorization semantics. It reads the
    same table through the same payload-authoritative seam
    (:func:`ne.audit.store._record_from_row`) as :func:`list_records`, is
    READ-ONLY (never touches the write path or the head pointer beyond reading
    it), and its caller gates it behind the identical ops-role chokepoint as
    every other chain read. The audit surface has no per-row authorization to
    bypass: any principal entitled to call this can already reach any row by
    time-windowed filtering, or by export, so the anchor reveals existence of
    nothing the caller could not otherwise reach.

    CONCURRENCY: the reads are separate statements on one connection, so under
    Postgres READ COMMITTED each sees its own snapshot. The head is therefore
    read FIRST, and the beyond-head verdict is decided from that head alone —
    never from an existence probe, whose later snapshot could contain a row
    appended mid-read and turn a bare miss into a false ``filtered_out`` on a
    request that applied no filters. Every row statement runs after the head
    read, and appends are the only concurrent writes (C10: append-only), so a
    row with ``seq <= head`` is guaranteed visible to the window and existence
    statements: ``filtered_out`` is only ever stated for a row that provably
    predates the filter read. A row appended mid-read CAN land in the window
    with ``seq > head_seq`` — ``found`` is then still a witnessed truth (the
    row is in the returned events), and the stale ``head_seq`` costs one
    refresh, never a false verdict. The statement ORDER is the invariant —
    pinned by ``test_list_records.py::
    test_append_racing_the_anchored_read_is_not_found_never_filtered_out``, so
    a refactor of the read sequence cannot silently reintroduce the race.
    """
    clamped = max(1, min(int(limit), MAX_PAGE_ROWS))
    clauses = _filter_clauses(actor, action, start, end)

    with engine.connect() as conn:
        # HEAD FIRST — this ordering is load-bearing (CONCURRENCY, above): the
        # beyond-head verdict is decided from a head observed BEFORE any row
        # statement, so a row appended mid-read can never turn a bare miss into
        # a false ``filtered_out``.
        head = conn.execute(
            sa.select(ne_audit_head.c.seq).where(ne_audit_head.c.id == 1)
        ).scalar_one()
        # The anchor-and-older half: DESC from the anchor, at most a full page.
        older = conn.execute(
            ne_audit_log.select()
            .where(sa.and_(ne_audit_log.c.seq <= anchor_seq, *clauses))
            .order_by(ne_audit_log.c.seq.desc())
            .limit(clamped)
        ).all()
        # The newer half: ASC away from the anchor — nearest-newer first. At most
        # clamped-1 can ever be used (the anchor side contributes ≥1 when it can).
        newer = conn.execute(
            ne_audit_log.select()
            .where(sa.and_(ne_audit_log.c.seq > anchor_seq, *clauses))
            .order_by(ne_audit_log.c.seq.asc())
            .limit(clamped)
        ).all()

        # Centered, clamped at both edges: aim (clamped-1)//2 newer rows, give
        # unused budget from a short side to the other so the window is always
        # as full as the filtered set allows.
        n_newer = min(len(newer), (clamped - 1) // 2)
        n_older = min(len(older), clamped - n_newer)
        n_newer = min(len(newer), clamped - n_older)
        window = list(reversed(newer[:n_newer])) + list(older[:n_older])

        found = bool(older) and int(older[0].seq) == int(anchor_seq)
        if found:
            status: AnchorStatus = "found"
        elif int(anchor_seq) > int(head):
            # Beyond the head observed at the start of the read: ``not_found``
            # by construction — never probed, so never subject to a probe
            # seeing a later snapshot than the window reads did.
            status = "not_found"
        else:
            exists = (
                conn.execute(
                    sa.select(ne_audit_log.c.seq).where(ne_audit_log.c.seq == anchor_seq)
                ).first()
                is not None
            )
            status = "filtered_out" if exists else "not_found"

    return (
        tuple(_record_from_row(row) for row in window),
        AnchorResolution(seq=int(anchor_seq), status=status, head_seq=int(head)),
    )


def count_records(
    engine: sa.Engine,
    *,
    actor: str | None = None,
    action: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> int:
    """Count audit records matching the SAME actor/action/time-window filters as
    :func:`list_records` (built from the identical :func:`_filter_clauses`).

    PAYLOAD-AUTHORITATIVE-READ discipline note: like ``list_records``, the count
    is computed over the denormalized COLUMNS for selection only — a tampered
    column can at most hide or mis-select a row from the count (never fabricate
    one), and :func:`ne.audit.verify.verify_chain` separately detects and names
    such tampering. READ-ONLY: never touches the write path, the head, or the
    chain.
    """
    clauses = _filter_clauses(actor, action, start, end)
    stmt = sa.select(sa.func.count()).select_from(ne_audit_log)
    if clauses:
        stmt = stmt.where(sa.and_(*clauses))
    with engine.connect() as conn:
        return int(conn.execute(stmt).scalar_one())
