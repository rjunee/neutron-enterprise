# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Trace legibility spans (NE-TRACE-LEGIBILITY) — typed lifts of the redacted details.

The trace screen must state what a run DID in plain language, derived from
served fields — so the assembler lifts the emitters' redacted details into
typed spans: the verified-answer executor's ``tool.execute`` detail
(template_id / args_shape NAMES / result_count), the T3 read core's
``knowledge.verified_read`` counts line, the grounding gate's
``grounding.assist`` enums, and ``session.run``'s conversation POINTER.

Proves, each direction mutation-relevant per docs/testing/vacuous-guards.md:

* a served ``result_count`` crosses as an int and a served ZERO stays 0 —
  distinct from ABSENT (the emitter's ``-1`` "could not parse my own header"
  sentinel maps to ``None``, never a fabricated count — and so does a JSON
  ``true``: ``bool`` subclasses ``int`` and is not a count);
* a ``tool.execute`` detail with NO verified-executor fields (a write tool)
  serves the same span with every lift absent — no invented template;
* ``knowledge.verified_read`` parses rows/row_limit/truncated, and an
  unparseable detail serves all-``None`` (kind still typed);
* ``grounding.assist`` / ``session.run`` lift exactly their enum/id fields;
* C12 choke: a body-shaped sentinel planted in an UN-ENUMERATED payload
  position never crosses the assembler, with an in-response presence CONTROL
  (a second sentinel in a genuinely-served position) proving the sweep reads
  the surface it claims to sweep.
"""

from __future__ import annotations

import json
import uuid

from ne.events import new_audit_event
from ne.schema import AuditOutcome

from .conftest import AppBuilder, get_state, run

#: A conversation-body-shaped string that must NEVER cross the assembler.
_BODY_SENTINEL = "BODY-SENTINEL-acme-sla-owes-9c4e"

#: A sentinel in a genuinely-served position (raw event detail) — the presence
#: control proving the no-leak sweep observes the serialized response.
_SERVED_SENTINEL = "SERVED-SENTINEL-raw-detail-51b0"


def _seed(h, tid: str) -> None:
    """One grounded chat turn's chain, in the REAL emitters' detail shapes."""
    state = get_state(h.app)

    async def _append() -> None:
        # (1) verified read that matched — ne.brain_ontology.verified.answers shape.
        await state.sink.append(
            new_audit_event(
                action="knowledge.verified_read",
                resource="knowledge://verified",
                outcome=AuditOutcome.success,
                actor="user:ada",
                detail="rows=120 row_limit=500 truncated=false truncation_reason=none",
                trace_id=tid,
            )
        )
        # (2) the tool execution behind it — the verified executor's redacted
        # detail (_make_audit_detail), plus a planted body sentinel in an
        # UN-ENUMERATED provenance key that must never cross (C12 choke).
        await state.sink.append(
            new_audit_event(
                action="tool.execute",
                resource="tool:read.kg.accounts_directory",
                outcome=AuditOutcome.success,
                actor="user:ada",
                detail=json.dumps(
                    {
                        "template_id": "accounts-directory",
                        "template_version": 1,
                        "args_shape": [],
                        "result_count": 120,
                        "ontology_version": "0cc94a72",
                        "shapes_hash": "8b9dd6b2",
                        "registry_hash": "7fd9c1af",
                    }
                ),
                trace_id=tid,
                provenance={
                    "source": "ne-runtime",
                    "component": "middleware",
                    "smuggled_body": _BODY_SENTINEL,
                },
            )
        )
        # (3) verified read that matched NOTHING — rows=0 is a fact, not absence.
        await state.sink.append(
            new_audit_event(
                action="knowledge.verified_read",
                resource="knowledge://verified",
                outcome=AuditOutcome.success,
                actor="user:ada",
                detail="rows=0 row_limit=500 truncated=false truncation_reason=none",
                trace_id=tid,
            )
        )
        # (4) the zero-row execution, with the single arg NAME the emitter records.
        await state.sink.append(
            new_audit_event(
                action="tool.execute",
                resource="tool:read.kg.tickets_by_status",
                outcome=AuditOutcome.success,
                actor="user:ada",
                detail=json.dumps(
                    {
                        "template_id": "tickets-by-status",
                        "template_version": 1,
                        "args_shape": ["status"],
                        "result_count": 0,
                    }
                ),
                trace_id=tid,
            )
        )
        # (5) the emitter's own parse-failure sentinel: result_count=-1 means
        # "unknown", and serving it as a count would fabricate a measurement.
        await state.sink.append(
            new_audit_event(
                action="tool.execute",
                resource="tool:read.kg.tickets_by_status",
                outcome=AuditOutcome.success,
                actor="user:ada",
                detail=json.dumps(
                    {
                        "template_id": "tickets-by-status",
                        "template_version": 1,
                        "args_shape": ["status"],
                        "result_count": -1,
                    }
                ),
                trace_id=tid,
            )
        )
        # (6) a write-tool execution — non-verified detail shape: no lift.
        await state.sink.append(
            new_audit_event(
                action="tool.execute",
                resource="tool:write.erp",
                outcome=AuditOutcome.success,
                actor="user:ada",
                detail='{"what_action": "write.erp"}',
                trace_id=tid,
            )
        )
        # (7) an OLD verified_read whose detail predates the counts line.
        await state.sink.append(
            new_audit_event(
                action="knowledge.verified_read",
                resource="knowledge://verified",
                outcome=AuditOutcome.success,
                actor="user:ada",
                detail="legacy free-text detail",
                trace_id=tid,
            )
        )
        # (8) the grounding gate's assist — counts/enums only (grounding.py).
        await state.sink.append(
            new_audit_event(
                action="grounding.assist",
                resource="grounding.gate",
                outcome=AuditOutcome.success,
                actor="ne-runtime",
                detail=json.dumps({"phase": "fail-redraft", "adopted": True, "error_class": None}),
                trace_id=tid,
            )
        )
        # (9) the terminal session.run — the conversation POINTER (agent.py).
        await state.sink.append(
            new_audit_event(
                action="session.run",
                resource="session:sess-1",
                outcome=AuditOutcome.success,
                actor="user:ada",
                detail=json.dumps(
                    {
                        "agent": "grounded-analyst",
                        "department": "engineering",
                        "session_id": "sess-1",
                        "succeeded": True,
                    }
                ),
                trace_id=tid,
            )
        )
        # (10) the presence CONTROL: a sentinel in a position the assembler
        # GENUINELY serves (raw-event detail, §0 law 5) — if the no-leak sweep
        # below cannot see this one, it proves nothing about the other.
        await state.sink.append(
            new_audit_event(
                action="custom.thing",
                resource="thing:1",
                outcome=AuditOutcome.success,
                actor="user:ada",
                detail=_SERVED_SENTINEL,
                trace_id=tid,
            )
        )
        # (11) a JSON-boolean result_count. ``bool`` subclasses ``int``, so a
        # bare isinstance-int guard passes ``True`` and serves it as 1 — a
        # "1 row" count nobody measured. It must serve as ABSENT.
        await state.sink.append(
            new_audit_event(
                action="tool.execute",
                resource="tool:read.kg.tickets_by_status",
                outcome=AuditOutcome.success,
                actor="user:ada",
                detail=json.dumps(
                    {
                        "template_id": "tickets-by-status",
                        "template_version": 1,
                        "args_shape": ["status"],
                        "result_count": True,
                    }
                ),
                trace_id=tid,
            )
        )

    run(_append())


def _get_body(make_app: AppBuilder) -> dict:
    h = make_app(ops_roles=("ops",))
    tid = uuid.uuid4().hex
    _seed(h, tid)

    async def _t() -> dict:
        async with h.client() as client:
            headers = h.bearer("user:ops", groups=["ops"])
            r = await client.get(f"/api/v1/trace/{tid}", headers=headers)
            assert r.status_code == 200
            return r.json()

    return run(_t())


def test_tool_span_lifts_the_verified_detail_and_keeps_zero_distinct_from_absent(
    make_app: AppBuilder,
) -> None:
    body = _get_body(make_app)
    nodes = body["nodes"]
    assert [n["kind"] for n in nodes] == [
        "verified_read",
        "tool",
        "verified_read",
        "tool",
        "tool",
        "tool",
        "verified_read",
        "grounding",
        "session",
        "event",
        "tool",
    ]

    full = nodes[1]["tool"]
    assert full["template_id"] == "accounts-directory"
    assert full["template_version"] == 1
    assert full["args"] == []
    assert full["result_count"] == 120

    # The zero-row execution: 0 crosses AS 0 — the fact the screen must show.
    empty = nodes[3]["tool"]
    assert empty["template_id"] == "tickets-by-status"
    assert empty["args"] == ["status"]
    assert empty["result_count"] == 0

    # The emitter's -1 parse-failure sentinel maps to ABSENT, never a count —
    # and never survives as -1 (a negative row count nobody measured).
    unknown = nodes[4]["tool"]
    assert unknown["template_id"] == "tickets-by-status"
    assert unknown["result_count"] is None

    # A write-tool detail lifts NOTHING: no invented template, args, or count.
    bare = nodes[5]["tool"]
    assert bare["template_id"] is None
    assert bare["template_version"] is None
    assert bare["args"] == []
    assert bare["result_count"] is None
    # The raw redacted detail still rides the node (the pane renders it).
    assert nodes[5]["detail"] == '{"what_action": "write.erp"}'

    # A JSON `true` result_count is a bool, and bool subclasses int — served
    # as a count it would render "1 row", a measurement nobody made. It maps
    # to ABSENT; the sibling lifts still cross (the record was typed, not
    # dropped — the control for this assertion).
    flagged = nodes[10]["tool"]
    assert flagged["template_id"] == "tickets-by-status"
    assert flagged["args"] == ["status"]
    assert flagged["result_count"] is None


def test_verified_read_span_parses_counts_and_degrades_to_absent(
    make_app: AppBuilder,
) -> None:
    body = _get_body(make_app)
    nodes = body["nodes"]

    assert nodes[0]["verified_read"] == {"rows": 120, "row_limit": 500, "truncated": False}
    # rows=0 is a served fact, distinct from the unparseable legacy detail.
    assert nodes[2]["verified_read"] == {"rows": 0, "row_limit": 500, "truncated": False}
    legacy = nodes[6]["verified_read"]
    assert legacy == {"rows": None, "row_limit": None, "truncated": None}
    # The unparsed legacy detail text still crosses for the raw fallback render.
    assert nodes[6]["detail"] == "legacy free-text detail"


def test_grounding_and_session_spans_lift_their_enum_and_pointer_fields(
    make_app: AppBuilder,
) -> None:
    body = _get_body(make_app)
    nodes = body["nodes"]

    assert nodes[7]["grounding"] == {
        "phase": "fail-redraft",
        "adopted": True,
        "error_class": None,
    }
    assert nodes[8]["session"] == {
        "agent": "grounded-analyst",
        "department": "engineering",
        "session_id": "sess-1",
        "succeeded": True,
    }


def test_no_body_shaped_payload_crosses_the_assembler(make_app: AppBuilder) -> None:
    """C12 choke: un-enumerated payload keys never cross; the control proves
    the sweep observes the serialized response it claims to sweep."""
    body = _get_body(make_app)
    serialized = json.dumps(body)

    # Presence CONTROL first: a sentinel in a genuinely-served position IS in
    # the response — a sweep that cannot see this one proves nothing.
    assert _SERVED_SENTINEL in serialized

    # The planted body sentinel rode an un-enumerated provenance key on a
    # tool.execute record; the assembler must not pass it through.
    assert _BODY_SENTINEL not in serialized
