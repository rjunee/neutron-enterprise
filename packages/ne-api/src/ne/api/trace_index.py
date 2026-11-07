# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The trace index (B11, observability plan §6 U1) — the run index over the chain.

:func:`query_trace_index` answers "what ran, what did it cost, what was denied"
without a trace id: one keyset-paged GROUP BY ``trace_id`` over ``ne_audit_log``
(the durable analytics substrate — plan §0.3; the outbox prunes acked rows and
is therefore NOT a store), then a page-bounded row fetch that derives every
rendered value from the hash-covered ``payload``.

TWO-STEP SHAPE, AND WHY. Step 1 (SQL) does everything that must happen BEFORE
``LIMIT``: trace-level filtering (HAVING conditional counts), the
``started_at``-desc sort, and the keyset cursor. It touches only the
denormalized query columns plus SQL JSON extraction over ``payload.provenance``
— exactly what those columns exist for (``store.py``: "indexed WHERE
filtering"). Step 2 (SQL + Python, bounded by the page) re-reads the page's
rows and derives every RENDERED field from the payload with the SAME defensive
readers the /v1/trace assembler uses (:func:`ne.api.trace_assembly._prov_str`
et al. — one derivation, two consumers, never a transliterated twin). Rendered
values are therefore payload-authoritative per the store's read discipline;
the columns only ever select and order.

VERIFICATION IS FULL-CHAIN, ALWAYS — deliberately NOT the audit page's
windowed verdict, and the divergence is the point, not an oversight. The
GROUP BY reads every row of the log regardless of the requested ``from``/``to``
window (a trace is selected by its min ``occurred_at``, but grouping and its
aggregates span ALL rows), so a verdict windowed to ``[from, to]`` could come
back ``valid=True`` while a tampered row OUTSIDE the window had silently moved
a trace into or out of this page, or reshaped its aggregates. Same trade, same
reason as ``trace_assembly.py``'s "VERIFICATION IS FULL-CHAIN, NOT WINDOWED":
a false "valid" on a governed ops surface is the worse failure than a
whole-log walk per request. (Cost note: this is the identical per-request cost
``GET /v1/trace/{id}`` already pays.)

COST IS LANE-KEYED AND PROVENANCE-DERIVED (plan §0.5, §5.4) — never a
model-name heuristic. A call is PRICED iff its provenance ``lane`` is exactly
``"frontier-api"`` (:data:`PRICED_LANE`); every other lane — including an
absent lane and any future, unrecognized lane — is UNPRICED (fail-honest: a
lane must earn dollars rendering, never the reverse). ``cost_usd`` sums
priced calls' costs ONLY, and is ``None`` when no priced call carried a cost —
so a private-vLLM run whose litellm cost is a real ``0.0`` serves
``cost_usd: null`` (self-hosted lanes have no market price), while a frontier
run that genuinely cost ``0.0`` serves ``cost_usd: 0.0``. "No cost" and
"zero cost" are different facts and are never conflated here.

FIELD DERIVATIONS (the contract, plan §6 U1). Per trace, over its seq-ordered
chain rows: ``started_at``/``ended_at`` = min/max payload ``occurred_at``;
``duration_ms`` their delta; ``root_action`` = the lowest-seq row's ``action``;
``task`` = first non-null provenance ``task`` in seq order; ``actor`` = the
single distinct provenance ``principal`` across gateway rows (B12 stamps it
going forward; pre-B12 rows have none) — ``None`` when zero or several;
``models`` = first-seen-ordered distinct union of ``response_model`` and
``resource`` over gateway rows; ``calls`` = count of gateway rows
(:data:`GATEWAY_ACTIONS`, ANY outcome — a mid-stream failure still consumed
tokens, NE-163a stamps every terminating call); token sums are ``None`` iff no
gateway row carried that key (absent is not zero); ``denials``/``errors`` =
rows with that payload ``outcome``; ``approvals`` = rows with
``event_type == "approval"``.

C12 / DATA MINIMIZATION (plan §3 row 8): rows carry identifiers, enums,
counts, timestamps and money — NEVER ``detail`` text, prompt/response bodies,
or unenumerated provenance. The only payload keys this module ever reads are
the enumerated scalars above; ``TraceIndexRowView`` has no free-text field, so
body-shaped content structurally cannot cross (pinned by the sentinel + exact
row-key-set tests in ``test_trace_index_api.py``).

PERFORMANCE (plan §5.1): step 1 is a full-table GROUP BY and the verdict is a
full-chain walk — both LINEAR in chain length, per request. The ``trace_id``
and ``occurred_at`` indexes exist (``store.py``) and serve step 2's page fetch
and the audit page's windowed reads, but they cannot make step 1 sublinear:
selecting the newest traces requires every trace's min ``occurred_at``, i.e. a
full scan, by construction. Measured numbers live in the plan's §5.1 (updated
at U1): the verify walk dominates the response, and because BOTH steps are
linear the §5.1 revisit trigger's p95 clause — not its row-count clause — is
the binding one, expected to fire around tens of thousands of chain rows.

PURE / SYNC: no FastAPI imports; the route runs this in one
``anyio.to_thread`` hop (the ``routers/audit.py`` discipline).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa

from ne.api.trace_assembly import _opt_str, _prov_str, _to_float, _to_int
from ne.api.wire import TraceIndexRowView, TraceIndexView, chain_verification_view
from ne.audit import verify_chain
from ne.audit.store import ne_audit_log

#: The per-call usage rows (NE-163a provenance riders). BOTH terminating
#: actions — enumerating only ``complete`` would silently drop every streamed
#: call from calls/tokens/cost (pinned by the exact-value fixture test).
GATEWAY_ACTIONS: tuple[str, ...] = ("ne.gateway.complete", "ne.gateway.stream")

#: The ONLY lane with a market price (``ne.contracts.types.Lane.FRONTIER_API``).
#: Everything else — including absent and future lanes — is unpriced (§0.5).
PRICED_LANE = "frontier-api"

#: Legal ``outcome`` filter values (plan §6 U1: traces containing ≥1 such row).
OUTCOME_FILTERS: tuple[str, ...] = ("denied", "error")

#: Page-size ceiling (plan §6 U1: ``limit`` ≤ 200, default 50).
MAX_TRACE_PAGE = 200

_C = ne_audit_log.c


# ---------------------------------------------------------------------------
# Step-1 SQL fragments
# ---------------------------------------------------------------------------


def _prov_sql(key: str) -> sa.ColumnElement[str | None]:
    """SQL twin of :func:`ne.api.trace_assembly._prov_str`, both shapes.

    Provenance values are typed ``str | ProvenanceEntry`` and round-trip
    through JSON as either a plain string or ``{"key":…, "value":…}``. The
    entry path is COALESCEd first (NULL for a plain string on both dialects),
    then the plain path; a malformed shape yields a non-matching string, never
    an error — the same degrade-to-no-match posture as ``_prov_str``.
    """
    entry = _C.payload[("provenance", key, "value")].as_string()
    plain = _C.payload[("provenance", key)].as_string()
    return sa.func.coalesce(entry, plain)


def _count_where(cond: sa.ColumnElement[bool]) -> sa.ColumnElement[int]:
    return sa.func.sum(sa.case((cond, 1), else_=0))


_IS_GATEWAY = _C.action.in_(GATEWAY_ACTIONS)


def _page_statement(
    *,
    start: datetime | None,
    end: datetime | None,
    actor: str | None,
    task: str | None,
    model: str | None,
    lane: str | None,
    config_hash: str | None,
    outcome: str | None,
    limit: int,
    before: tuple[datetime, str] | None,
) -> sa.Select[tuple[str]]:
    """The page-selection statement: GROUP BY ``trace_id``, trace-level HAVING
    filters, ``started_at`` DESC + ``trace_id`` DESC sort, keyset cursor,
    ``limit + 1`` rows (the +1 detects ``truncated`` without a COUNT).

    Filters are HAVING conditional counts, NOT WHERE clauses, deliberately: a
    WHERE would drop rows from the very GROUP BY that computes each trace's
    ``min(occurred_at)``, so a trace straddling ``from`` would be selected on a
    truncated start time. HAVING keeps grouping over ALL rows and filters
    whole traces.
    """
    min_occ = sa.func.min(_C.occurred_at)
    having: list[sa.ColumnElement[bool]] = []

    if start is not None:
        having.append(min_occ >= start)
    if end is not None:
        having.append(min_occ <= end)
    if outcome is not None:
        if outcome not in OUTCOME_FILTERS:
            raise ValueError(f"outcome must be one of {OUTCOME_FILTERS}")
        having.append(_count_where(_C.outcome == outcome) >= 1)
    if task is not None:
        having.append(_count_where(_prov_sql("task") == task) >= 1)
    if lane is not None:
        having.append(_count_where(sa.and_(_IS_GATEWAY, _prov_sql("lane") == lane)) >= 1)
    if model is not None:
        having.append(
            _count_where(
                sa.or_(
                    _prov_sql("response_model") == model,
                    sa.and_(_IS_GATEWAY, _C.resource == model),
                )
            )
            >= 1
        )
    if config_hash is not None:
        having.append(_count_where(_C.config_hash == config_hash) >= 1)
    if actor is not None:
        principal = _prov_sql("principal")
        attributed = _count_where(sa.and_(_IS_GATEWAY, principal.is_not(None)))
        if actor == "unattributed":
            # The service bucket: traces where NO gateway call carries a
            # recorded principal (plan §0.4 — recorded attribution only).
            having.append(attributed == 0)
        else:
            having.append(_count_where(sa.and_(_IS_GATEWAY, principal == actor)) >= 1)
    if before is not None:
        b_start, b_tid = before
        having.append(sa.or_(min_occ < b_start, sa.and_(min_occ == b_start, _C.trace_id < b_tid)))

    stmt = (
        sa.select(_C.trace_id)
        .where(_C.trace_id.is_not(None))
        .group_by(_C.trace_id)
        .order_by(min_occ.desc(), _C.trace_id.desc())
        .limit(limit + 1)
    )
    for cond in having:
        stmt = stmt.having(cond)
    return stmt


# ---------------------------------------------------------------------------
# Step-2 payload-authoritative row derivation
# ---------------------------------------------------------------------------


def _occurred(payload: dict[str, Any]) -> datetime | None:
    """Defensive payload ``occurred_at`` read (ISO string or datetime ⇒ aware).

    ``append_record`` validates ``occurred_at`` at insert time, so on an
    untampered chain this never returns ``None``; the defensive posture exists
    so a corrupted payload degrades (row excluded from time bounds, verdict
    already ``valid=False``) instead of 500-ing the whole index.
    """
    raw = payload.get("occurred_at")
    if isinstance(raw, datetime):
        value = raw
    elif isinstance(raw, str):
        try:
            value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _row_view(trace_id: str, payloads: list[dict[str, Any]]) -> TraceIndexRowView | None:
    """Derive one index row from a trace's seq-ordered payloads (docstring
    "FIELD DERIVATIONS" is the contract). Never raises on malformed fields."""
    times = [t for t in (_occurred(p) for p in payloads) if t is not None]
    if not times:
        return None
    started_at = min(times)
    ended_at = max(times)

    task: str | None = None
    models: list[str] = []
    principals: set[str] = set()
    calls = 0
    priced_calls = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    denials = 0
    errors = 0
    approvals = 0

    for payload in payloads:
        outcome = payload.get("outcome")
        if outcome == "denied":
            denials += 1
        elif outcome == "error":
            errors += 1
        if payload.get("event_type") == "approval":
            approvals += 1
        if task is None:
            task = _prov_str(payload, "task")
        if payload.get("action") not in GATEWAY_ACTIONS:
            continue

        calls += 1
        priced = _prov_str(payload, "lane") == PRICED_LANE
        if priced:
            priced_calls += 1
        for name in (_prov_str(payload, "response_model"), _opt_str(payload.get("resource"))):
            if name is not None and name not in models:
                models.append(name)
        in_tok = _to_int(_prov_str(payload, "input_tokens"))
        if in_tok is not None:
            input_tokens = in_tok if input_tokens is None else input_tokens + in_tok
        out_tok = _to_int(_prov_str(payload, "output_tokens"))
        if out_tok is not None:
            output_tokens = out_tok if output_tokens is None else output_tokens + out_tok
        if priced:
            # PRICED calls only — an unpriced lane's litellm cost is a real
            # 0.0 byte on the chain, and summing it would render a dollar
            # figure for a lane with no market price (§0.5). None stays None:
            # a priced call whose cost never landed is "unmeasured", not $0.
            cost = _to_float(_prov_str(payload, "cost_usd"))
            if cost is not None:
                cost_usd = cost if cost_usd is None else cost_usd + cost
        principal = _prov_str(payload, "principal")
        if principal is not None:
            principals.add(principal)

    return TraceIndexRowView(
        trace_id=trace_id,
        started_at=started_at,
        ended_at=ended_at,
        duration_ms=(ended_at - started_at).total_seconds() * 1000.0,
        task=task,
        root_action=_opt_str(payloads[0].get("action")),
        actor=next(iter(principals)) if len(principals) == 1 else None,
        models=models,
        calls=calls,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
        priced_calls=priced_calls,
        unpriced_calls=calls - priced_calls,
        denials=denials,
        errors=errors,
        approvals=approvals,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def query_trace_index(
    engine: sa.Engine,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    actor: str | None = None,
    task: str | None = None,
    model: str | None = None,
    lane: str | None = None,
    config_hash: str | None = None,
    outcome: str | None = None,
    limit: int = 50,
    before: tuple[datetime, str] | None = None,
) -> TraceIndexView:
    """Run the index query + page row derivation + FULL chain verify, one
    connection. ``limit`` is clamped to ``1..=MAX_TRACE_PAGE`` here as well as
    at the route (defense in depth, the ``list_records`` precedent)."""
    limit = max(1, min(int(limit), MAX_TRACE_PAGE))
    stmt = _page_statement(
        start=start,
        end=end,
        actor=actor,
        task=task,
        model=model,
        lane=lane,
        config_hash=config_hash,
        outcome=outcome,
        limit=limit,
        before=before,
    )

    with engine.connect() as conn:
        page_ids: list[str] = [row.trace_id for row in conn.execute(stmt)]
        truncated = len(page_ids) > limit
        page_ids = page_ids[:limit]

        grouped: dict[str, list[dict[str, Any]]] = {tid: [] for tid in page_ids}
        if page_ids:
            rows = conn.execute(
                sa.select(_C.trace_id, _C.seq, _C.payload)
                .where(_C.trace_id.in_(page_ids))
                .order_by(_C.seq.asc())
            )
            for row in rows:
                grouped[row.trace_id].append(dict(row.payload))

        cv = verify_chain(conn)

    traces = [view for tid in page_ids if (view := _row_view(tid, grouped[tid])) is not None]
    return TraceIndexView(
        traces=traces,
        verification=chain_verification_view(cv),
        truncated=truncated,
    )
