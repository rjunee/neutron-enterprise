# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""``GET /v1/traces`` (B11, observability plan §6 U1) — the run index.

The five load-bearing guards from the plan, each bidirectional per
``docs/testing/vacuous-guards.md`` (the RED direction is mutation-proven
against ``ne.api.trace_index`` — mutation transcript in the unit report; the
GREEN control is an in-test assertion a naive over-broad rule would flag):

1. EXACT-VALUE rows over a three-trace mixed-lane fixture with a two-leg
   approval flow — calls / token sums / ``cost_usd`` to exact float values /
   ``denials == 1`` on the deny-leg trace, ``denials == 0`` controls.
   Control: an outcome-``error`` gateway row still COUNTS in ``calls``
   (a success-only enumeration is the natural wrong rule).
2. UNPRICED is lane-keyed, bidirectional: a private-vllm-only trace (whose
   chain rows carry a REAL litellm ``cost_usd: "0.0"``) serves
   ``cost_usd: null``; the control frontier trace with a genuine ``0.0``
   serves ``cost_usd: 0.0``. A lane-blind sum fails the first; a
   "0-means-unpriced" conflation fails the second. An unrecognized lane
   defaults to unpriced (fail-honest) with its dollars EXCLUDED.
3. ``outcome=denied`` returns exactly the traces with ≥1 denial (positive +
   negative row). Control: the denied trace still serves in the UNFILTERED
   listing (over-broad "hide denied runs" would flag it).
4. Keyset paging: pages disjoint and gapless including a started_at TIE
   (trace_id tiebreak), stable under an insert between requests. Control:
   the inserted trace IS visible to a fresh unanchored query.
5. A tampered chain flips ``verification.valid`` to false while rows still
   serve — including a tamper OUTSIDE a requested ``from`` window (the page
   verdict is FULL-chain, never windowed) and a denormalized trace_id
   COLUMN tamper. Control: rows still serve (an over-broad "refuse on
   tamper" rule would flag serving them).

Plus: C12 body sentinels (prompt/note-shaped text planted in ``detail`` and
un-enumerated provenance never crosses; every row's key set is EXACTLY the
17 contract fields), ops-gate parity with ``/v1/audit/*``, filter semantics
(task/model/lane/actor/config_hash/window), and param validation (limit
bounds, malformed ``before``, unknown ``outcome``, bad ISO).

No pytest-asyncio (repo policy): async driven via :func:`conftest.run`.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa

from ne.events import new_approval_event, new_audit_event
from ne.schema import ApprovalDecision, AuditOutcome

from .conftest import AppBuilder, get_state, run

_T0 = datetime(2026, 7, 20, 12, 0, 0, tzinfo=UTC)

#: The exact wire fields of one index row — pinned as a SET so an added
#: free-text/body-shaped field is a loud failure here, not a quiet widening
#: (C12; the plan's row 8 stance that bodies are never served).
ROW_FIELDS = {
    "trace_id",
    "started_at",
    "ended_at",
    "duration_ms",
    "task",
    "root_action",
    "actor",
    "models",
    "calls",
    "input_tokens",
    "output_tokens",
    "cost_usd",
    "priced_calls",
    "unpriced_calls",
    "denials",
    "errors",
    "approvals",
}

#: Body-shaped sentinels planted in the fixture (detail text + an
#: un-enumerated provenance key). If either string ever appears in the
#: response, body content found a path to the wire.
PROMPT_SENTINEL = "PROMPT-BODY-SENTINEL-cc3b1e"
NOTE_SENTINEL = "NOTE-BODY-SENTINEL-9d4a72"


def _at(minutes: float) -> datetime:
    return _T0 + timedelta(minutes=minutes)


def _gw(
    tid: str,
    minutes: float,
    *,
    lane: str | None,
    resource: str,
    model_name: str = "m",
    task: str = "chat",
    cost: str | None = None,
    in_tok: int | None = None,
    out_tok: int | None = None,
    response_model: str | None = None,
    principal: str | None = None,
    outcome: AuditOutcome = AuditOutcome.success,
    action: str = "ne.gateway.complete",
    extra_prov: dict[str, str] | None = None,
) -> Any:
    """A gateway per-call usage row mirroring the REAL emitter's provenance
    shape (gateway.py ``ne.gateway.complete``/``.stream`` + NE-163a keys)."""
    prov: dict[str, str] = {"task": task, "model_name": model_name}
    if lane is not None:
        prov["lane"] = lane
    if cost is not None:
        prov["cost_usd"] = cost
    if in_tok is not None:
        prov["input_tokens"] = str(in_tok)
    if out_tok is not None:
        prov["output_tokens"] = str(out_tok)
    if response_model is not None:
        prov["response_model"] = response_model
    if principal is not None:
        prov["principal"] = principal
    if extra_prov:
        prov.update(extra_prov)
    return new_audit_event(
        action=action,
        resource=resource,
        outcome=outcome,
        actor="ne-gateway",
        detail=f"task={task} lane={lane} model={model_name}",
        trace_id=tid,
        provenance=prov,
        occurred_at=_at(minutes),
    )


def _seed(h: Any, events: list[Any]) -> None:
    state = get_state(h.app)

    async def _append() -> None:
        for ev in events:
            await state.sink.append(ev)

    run(_append())


def _seed_three_traces(h: Any) -> tuple[str, str, str]:
    """The plan's guard-1 fixture: three traces, mixed lanes, a two-leg
    approval flow (granted leg on A executes; deny leg on B is refused),
    plus C12 body sentinels riding detail text and an un-enumerated
    provenance key."""
    a, b, c = "tr-frontier", "tr-denied", "tr-private"
    events = [
        # --- trace A: granted leg — pdp allow → 2 frontier + 1 private call
        # → approval approved → tool.execute success.
        new_audit_event(
            action="pdp.decide",
            resource="tool:write.crm.update",
            outcome=AuditOutcome.success,
            actor="user:ada",
            detail=json.dumps(
                {"effect": "allow", "reason": "ok", "tier": 2, "policy_version": "v1"}
            ),
            trace_id=a,
            occurred_at=_at(0),
        ),
        _gw(
            a,
            0.5,
            lane="frontier-api",
            resource="openai/gpt-x",
            model_name="gpt-x",
            cost="0.5",
            in_tok=100,
            out_tok=20,
            response_model="gpt-x-0714",
            extra_prov={"prompt_body": PROMPT_SENTINEL},
        ),
        _gw(
            a,
            1.0,
            lane="frontier-api",
            resource="openai/gpt-x",
            model_name="gpt-x",
            cost="0.25",
            in_tok=50,
            out_tok=10,
            response_model="gpt-x-0714",
            action="ne.gateway.stream",
        ),
        _gw(
            a,
            1.5,
            lane="private-vllm",
            resource="private/qwen",
            model_name="qwen",
            cost="0.0",
            in_tok=30,
            out_tok=5,
            response_model="qwen-2.5",
        ),
        new_approval_event(
            approval_id="ap-grant",
            tier=2,
            decision=ApprovalDecision.approved,
            subject_ref="tool:write.crm.update",
            actor="user:bob",
            trace_id=a,
            occurred_at=_at(2),
        ),
        new_audit_event(
            action="tool.execute",
            resource="tool:write.crm.update",
            outcome=AuditOutcome.success,
            actor="user:ada",
            detail=f"executed after ap-grant {NOTE_SENTINEL}",
            trace_id=a,
            occurred_at=_at(2.5),
        ),
        # --- trace B: deny leg — 1 frontier call → pdp deny → approval
        # rejected → the real all-candidates-failed error emit shape.
        _gw(
            b,
            10,
            lane="frontier-api",
            resource="openai/gpt-x",
            model_name="gpt-x",
            task="review",
            cost="0.125",
            in_tok=40,
            out_tok=8,
            response_model="gpt-x-0714",
        ),
        new_audit_event(
            action="pdp.decide",
            resource="tool:write.crm.delete",
            outcome=AuditOutcome.denied,
            actor="user:ada",
            detail=json.dumps({"effect": "deny", "reason": "tier 2 requires approval", "tier": 2}),
            trace_id=b,
            occurred_at=_at(10.5),
        ),
        new_approval_event(
            approval_id="ap-deny",
            tier=2,
            decision=ApprovalDecision.rejected,
            subject_ref="tool:write.crm.delete",
            actor="user:bob",
            trace_id=b,
            occurred_at=_at(11),
        ),
        # The REAL gateway all-failed emit: resource is the TASK, provenance
        # carries task only (no lane/usage) — counts as a call, unpriced.
        new_audit_event(
            action="ne.gateway.complete",
            resource="review",
            outcome=AuditOutcome.error,
            actor="ne-gateway",
            detail="all model candidates failed: redacted",
            trace_id=b,
            provenance={"task": "review"},
            occurred_at=_at(11.5),
        ),
        # --- trace C: private-lane-only run — litellm's REAL 0.0 costs.
        _gw(
            c,
            20,
            lane="private-vllm",
            resource="private/qwen",
            model_name="qwen",
            task="summarize",
            cost="0.0",
            in_tok=60,
            out_tok=12,
            response_model="qwen-2.5",
        ),
        _gw(
            c,
            20.5,
            lane="private-vllm",
            resource="private/phi",
            model_name="phi",
            task="summarize",
            cost="0.0",
            in_tok=25,
            out_tok=6,
            action="ne.gateway.stream",
        ),
    ]
    _seed(h, events)
    return a, b, c


async def _get(h: Any, query: str = "") -> Any:
    async with h.client() as client:
        headers = h.bearer("user:ops", groups=["ops"])
        return await client.get(f"/api/v1/traces{query}", headers=headers)


def _body(h: Any, query: str = "") -> dict[str, Any]:
    async def _t() -> dict[str, Any]:
        r = await _get(h, query)
        assert r.status_code == 200, r.text
        return r.json()

    return run(_t())


def _ids(body: dict[str, Any]) -> list[str]:
    return [row["trace_id"] for row in body["traces"]]


# ---------------------------------------------------------------------------
# Ops gate — same fail-closed parity as /v1/trace and /v1/audit/*
# ---------------------------------------------------------------------------


def test_traces_requires_ops_role(make_app: AppBuilder) -> None:
    h = make_app(ops_roles=("ops",))
    _seed_three_traces(h)

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get("/api/v1/traces", headers=h.bearer("u", groups=["not-ops"]))
            assert r.status_code == 403

    run(_t())


def test_traces_empty_ops_roles_fails_closed(make_app: AppBuilder) -> None:
    h = make_app()  # ops_roles default ()
    _seed_three_traces(h)

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get(
                "/api/v1/traces", headers=h.bearer("rich", groups=["ops", "admins"])
            )
            assert r.status_code == 403

    run(_t())

    engine = sa.create_engine(h.audit_url)
    try:
        from ne.audit import list_records

        rows = list_records(engine, action="api.audit.unauthorized")
    finally:
        engine.dispose()
    assert len(rows) >= 1


# ---------------------------------------------------------------------------
# Guard 1 — exact-value rows over the three-trace fixture
# ---------------------------------------------------------------------------


def test_index_rows_exact_values(make_app: AppBuilder) -> None:
    h = make_app(ops_roles=("ops",))
    a, b, c = _seed_three_traces(h)

    body = _body(h)
    assert set(body) == {"traces", "verification", "truncated"}
    assert body["truncated"] is False
    assert body["verification"]["valid"] is True
    # Sort: started_at DESC — C (12:20) then B (12:10) then A (12:00).
    assert _ids(body) == [c, b, a]

    row_c, row_b, row_a = body["traces"]

    assert row_a["trace_id"] == a
    assert row_a["started_at"] == "2026-07-20T12:00:00Z"
    assert row_a["ended_at"] == "2026-07-20T12:02:30Z"
    assert row_a["duration_ms"] == 150000.0
    assert row_a["task"] == "chat"
    assert row_a["root_action"] == "pdp.decide"
    assert row_a["actor"] is None
    assert row_a["models"] == ["gpt-x-0714", "openai/gpt-x", "qwen-2.5", "private/qwen"]
    assert row_a["calls"] == 3
    assert row_a["input_tokens"] == 180
    assert row_a["output_tokens"] == 35
    # 0.5 + 0.25 — priced (frontier) calls ONLY; the private call's real
    # 0.0 chain byte is not in this sum (it is UNPRICED, not free).
    assert row_a["cost_usd"] == 0.75
    assert row_a["priced_calls"] == 2
    assert row_a["unpriced_calls"] == 1
    assert row_a["denials"] == 0  # control: the granted leg has no denial
    assert row_a["errors"] == 0
    assert row_a["approvals"] == 1

    assert row_b["trace_id"] == b
    assert row_b["task"] == "review"
    assert row_b["root_action"] == "ne.gateway.complete"
    # The error emit COUNTS as a call (control: a success-only enumeration
    # is the natural wrong rule — NE-163a stamps every TERMINATING call,
    # including failures); its resource (the task string) joins models[]
    # exactly as the real emitter writes it.
    assert row_b["calls"] == 2
    assert row_b["models"] == ["gpt-x-0714", "openai/gpt-x", "review"]
    assert row_b["input_tokens"] == 40
    assert row_b["output_tokens"] == 8
    assert row_b["cost_usd"] == 0.125
    assert row_b["priced_calls"] == 1
    assert row_b["unpriced_calls"] == 1
    assert row_b["denials"] == 1  # exactly the deny leg's pdp row
    assert row_b["errors"] == 1
    assert row_b["approvals"] == 1

    assert row_c["trace_id"] == c
    assert row_c["task"] == "summarize"
    assert row_c["calls"] == 2
    assert row_c["input_tokens"] == 85
    assert row_c["output_tokens"] == 18
    assert row_c["denials"] == 0
    assert row_c["approvals"] == 0
    assert row_c["models"] == ["qwen-2.5", "private/qwen", "private/phi"]


# ---------------------------------------------------------------------------
# Guard 2 — unpriced is lane-keyed, both directions
# ---------------------------------------------------------------------------


def test_unpriced_is_lane_keyed_bidirectional(make_app: AppBuilder) -> None:
    h = make_app(ops_roles=("ops",))
    _, _, c = _seed_three_traces(h)
    _seed(
        h,
        [
            # Control: a frontier call that GENUINELY cost 0.0 — priced.
            _gw(
                "tr-free-frontier",
                30,
                lane="frontier-api",
                resource="openai/gpt-x",
                model_name="gpt-x",
                cost="0.0",
                in_tok=10,
                out_tok=2,
                response_model="gpt-x-0714",
            ),
            # Fail-honest: an UNRECOGNIZED lane never earns dollars rendering —
            # its 0.9 is excluded from cost_usd and counted unpriced.
            _gw(
                "tr-newlane",
                31,
                lane="frontier-api",
                resource="openai/gpt-x",
                model_name="gpt-x",
                cost="0.0",
                response_model="gpt-x-0714",
            ),
            _gw(
                "tr-newlane",
                31.5,
                lane="gpu-experimental",
                resource="exp/new",
                model_name="new",
                cost="0.9",
                action="ne.gateway.stream",
            ),
        ],
    )

    rows = {row["trace_id"]: row for row in _body(h)["traces"]}

    # A private-lane run has NO market price: null, never $0.00 — even though
    # its chain rows carry a literal cost_usd "0.0" (litellm's real output).
    private = rows[c]
    assert private["cost_usd"] is None
    assert private["priced_calls"] == 0
    assert private["unpriced_calls"] == 2

    # CONTROL (kills the 0-means-unpriced conflation): a frontier call whose
    # cost is genuinely 0.0 serves the NUMBER 0.0, not null.
    free = rows["tr-free-frontier"]
    assert free["cost_usd"] == 0.0
    assert free["cost_usd"] is not None
    assert free["priced_calls"] == 1
    assert free["unpriced_calls"] == 0

    # Fail-honest on a lane nobody has heard of: dollars excluded, unpriced.
    newlane = rows["tr-newlane"]
    assert newlane["cost_usd"] == 0.0  # the frontier 0.0, NOT 0.9
    assert newlane["priced_calls"] == 1
    assert newlane["unpriced_calls"] == 1


# ---------------------------------------------------------------------------
# Guard 3 — outcome filter returns exactly the matching traces
# ---------------------------------------------------------------------------


def test_outcome_filter_exact(make_app: AppBuilder) -> None:
    h = make_app(ops_roles=("ops",))
    a, b, c = _seed_three_traces(h)

    assert _ids(_body(h, "?outcome=denied")) == [b]
    assert _ids(_body(h, "?outcome=error")) == [b]
    # Control: the denied trace still serves UNFILTERED (an over-broad
    # "suppress denied runs" rule would flag this), and the clean traces'
    # absence above is a real exclusion, not an empty page.
    unfiltered = _ids(_body(h))
    assert b in unfiltered
    assert a in unfiltered and c in unfiltered

    async def _bad() -> None:
        r = await _get(h, "?outcome=bogus")
        assert r.status_code == 422

    run(_bad())


# ---------------------------------------------------------------------------
# Guard 4 — keyset paging: disjoint, gapless, tie-broken, insert-stable
# ---------------------------------------------------------------------------


def test_keyset_paging_stable_under_insert(make_app: AppBuilder) -> None:
    h = make_app(ops_roles=("ops",))
    # Five single-call traces; c and d TIE on started_at (tiebreak trace_id
    # DESC), so the cursor's second component is load-bearing, not decor.
    for tid, minutes in [
        ("tr-pg-a", 0),
        ("tr-pg-b", 1),
        ("tr-pg-c", 2),
        ("tr-pg-d", 2),
        ("tr-pg-e", 3),
    ]:
        _seed(h, [_gw(tid, minutes, lane="private-vllm", resource="private/qwen")])

    page1 = _body(h, "?limit=2")
    assert _ids(page1) == ["tr-pg-e", "tr-pg-d"]
    assert page1["truncated"] is True
    last = page1["traces"][-1]
    cursor = f"{last['started_at']}~{last['trace_id']}"

    # An insert BETWEEN requests — newest-so-far — must not shift the
    # anchored continuation.
    _seed(h, [_gw("tr-pg-z", 9, lane="private-vllm", resource="private/qwen")])

    page2 = _body(h, f"?limit=2&before={cursor}")
    assert _ids(page2) == ["tr-pg-c", "tr-pg-b"]
    assert page2["truncated"] is True
    last2 = page2["traces"][-1]
    page3 = _body(h, f"?limit=2&before={last2['started_at']}~{last2['trace_id']}")
    assert _ids(page3) == ["tr-pg-a"]
    assert page3["truncated"] is False

    # No overlap, no gap: the three anchored pages tile the original five.
    walked = _ids(page1) + _ids(page2) + _ids(page3)
    assert walked == ["tr-pg-e", "tr-pg-d", "tr-pg-c", "tr-pg-b", "tr-pg-a"]
    assert len(set(walked)) == len(walked)

    # Control: the inserted trace IS live — a fresh unanchored query leads
    # with it (paging stability came from the cursor, not from hiding data).
    assert _ids(_body(h, "?limit=2"))[0] == "tr-pg-z"


def test_before_cursor_malformed_422(make_app: AppBuilder) -> None:
    h = make_app(ops_roles=("ops",))

    async def _t() -> None:
        for bad in ("garbage", "2026-07-20T12:00:00Z", "not-a-date~tr-x", "~", "iso~"):
            r = await _get(h, f"?before={bad}")
            assert r.status_code == 422, bad

    run(_t())


def test_limit_bounds(make_app: AppBuilder) -> None:
    h = make_app(ops_roles=("ops",))

    async def _t() -> None:
        assert (await _get(h, "?limit=0")).status_code == 422
        assert (await _get(h, "?limit=201")).status_code == 422
        assert (await _get(h, "?limit=200")).status_code == 200
        assert (await _get(h, "?from=not-a-date")).status_code == 422

    run(_t())


# ---------------------------------------------------------------------------
# Guard 5 — tamper flips the page verdict; rows still serve; never windowed
# ---------------------------------------------------------------------------


def _tamper_payload(h: Any, tid: str) -> None:
    """Forge one payload field on the trace's FIRST row (hash now mismatched)."""
    engine = sa.create_engine(h.audit_url)
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("DROP TRIGGER IF EXISTS ne_audit_log_no_update")
            row = conn.execute(
                sa.text(
                    "SELECT seq, payload FROM ne_audit_log "
                    "WHERE trace_id = :tid ORDER BY seq LIMIT 1"
                ),
                {"tid": tid},
            ).first()
            assert row is not None
            seq, raw = row
            payload = json.loads(raw) if isinstance(raw, str) else dict(raw)
            payload["actor"] = "forged"
            conn.execute(
                sa.text("UPDATE ne_audit_log SET payload = :p WHERE seq = :seq"),
                {"p": json.dumps(payload), "seq": seq},
            )
    finally:
        engine.dispose()


def test_tampered_chain_flips_verdict_rows_still_serve(make_app: AppBuilder) -> None:
    h = make_app(ops_roles=("ops",))
    a, _, _ = _seed_three_traces(h)
    _tamper_payload(h, a)

    body = _body(h)
    assert body["verification"]["valid"] is False
    # Control: rows STILL serve beside the red verdict (an over-broad
    # refuse-on-tamper rule would flag this) — the operator sees both facts.
    assert len(body["traces"]) == 3


def test_tamper_outside_window_still_flips_verdict(make_app: AppBuilder) -> None:
    """The page verdict is FULL-chain, never windowed to ``from``/``to``.

    The tampered row lives at 12:00 (trace A); the request windows from
    12:05, so A is not even on the page — but the GROUP BY read the whole
    table to build it, so a verdict windowed to [12:05, ∞) would come back
    valid over data whose substrate is forged. Full-chain verify says false.
    """
    h = make_app(ops_roles=("ops",))
    a, b, c = _seed_three_traces(h)
    _tamper_payload(h, a)

    body = _body(h, "?from=2026-07-20T12:05:00%2B00:00")
    assert _ids(body) == [c, b]  # A correctly outside the window
    assert body["verification"]["valid"] is False


def test_trace_id_column_tamper_flips_verdict(make_app: AppBuilder) -> None:
    """A denormalized trace_id COLUMN tamper (hash columns untouched) regroups
    rows silently — the exact attack class trace_assembly's full-chain note
    documents. The page verdict must go false."""
    h = make_app(ops_roles=("ops",))
    a, _, _ = _seed_three_traces(h)

    engine = sa.create_engine(h.audit_url)
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("DROP TRIGGER IF EXISTS ne_audit_log_no_update")
            row = conn.execute(
                sa.text("SELECT seq FROM ne_audit_log WHERE trace_id = :tid ORDER BY seq LIMIT 1"),
                {"tid": a},
            ).first()
            assert row is not None
            conn.execute(
                sa.text("UPDATE ne_audit_log SET trace_id = :bogus WHERE seq = :seq"),
                {"bogus": "evil-" + uuid.uuid4().hex, "seq": row[0]},
            )
    finally:
        engine.dispose()

    body = _body(h)
    assert body["verification"]["valid"] is False
    assert len(body["traces"]) >= 3  # rows still serve


# ---------------------------------------------------------------------------
# C12 — body content structurally cannot reach the index
# ---------------------------------------------------------------------------


def test_no_body_content_ever_crosses(make_app: AppBuilder) -> None:
    h = make_app(ops_roles=("ops",))
    _seed_three_traces(h)

    async def _t() -> None:
        r = await _get(h)
        assert r.status_code == 200
        # The planted prompt-body provenance key and note-body detail text
        # never cross (C12 / plan §3 row 8: bodies are not stored fields of
        # this view and no free-text field exists to carry them).
        assert PROMPT_SENTINEL not in r.text
        assert NOTE_SENTINEL not in r.text
        # And no detail-shaped field exists at all: every row's key set is
        # EXACTLY the 17 contract fields.
        for row in r.json()["traces"]:
            assert set(row) == ROW_FIELDS

    run(_t())


# ---------------------------------------------------------------------------
# Filters — task / model / lane / actor / config_hash / window
# ---------------------------------------------------------------------------


def test_task_model_lane_filters(make_app: AppBuilder) -> None:
    h = make_app(ops_roles=("ops",))
    a, b, c = _seed_three_traces(h)

    assert _ids(_body(h, "?task=review")) == [b]
    assert _ids(_body(h, "?task=chat")) == [a]
    # model matches EITHER arm: response_model (both b and a serve
    # gpt-x-0714) or the gateway row's resource (private/phi is resource-only
    # on trace c — proves the resource arm is live, not decor).
    assert _ids(_body(h, "?model=gpt-x-0714")) == [b, a]
    assert _ids(_body(h, "?model=private/phi")) == [c]
    assert _ids(_body(h, "?lane=frontier-api")) == [b, a]
    assert _ids(_body(h, "?lane=private-vllm")) == [c, a]
    assert _ids(_body(h, "?lane=no-such-lane")) == []


def test_actor_filter_and_attribution(make_app: AppBuilder) -> None:
    h = make_app(ops_roles=("ops",))
    _seed(
        h,
        [
            # B12-forward: gateway rows stamped with a recorded principal.
            _gw(
                "tr-principal",
                0,
                lane="frontier-api",
                resource="openai/gpt-x",
                cost="0.5",
                principal="user:ada",
            ),
            # Pre-B12 / service rows: no principal anywhere.
            _gw("tr-plain", 1, lane="private-vllm", resource="private/qwen"),
            # Two DISTINCT principals on one trace: actor renders null
            # (single-distinct rule) but the containment filter still matches.
            _gw(
                "tr-two",
                2,
                lane="frontier-api",
                resource="openai/gpt-x",
                cost="0.1",
                principal="user:ada",
            ),
            _gw(
                "tr-two",
                2.5,
                lane="frontier-api",
                resource="openai/gpt-x",
                cost="0.1",
                principal="user:bob",
                action="ne.gateway.stream",
            ),
        ],
    )

    rows = {row["trace_id"]: row for row in _body(h)["traces"]}
    assert rows["tr-principal"]["actor"] == "user:ada"
    assert rows["tr-plain"]["actor"] is None
    assert rows["tr-two"]["actor"] is None  # 2 distinct ⇒ never a guess

    assert _ids(_body(h, "?actor=user:ada")) == ["tr-two", "tr-principal"]
    assert _ids(_body(h, "?actor=user:bob")) == ["tr-two"]
    # `unattributed` selects the service bucket — traces with NO recorded
    # principal — and EXCLUDES the attributed ones (bidirectional).
    assert _ids(_body(h, "?actor=unattributed")) == ["tr-plain"]


def test_config_hash_filter(make_app: AppBuilder) -> None:
    h = make_app(ops_roles=("ops",))
    _seed_three_traces(h)
    # A 64-hex config_hash distinct from the ambient frozen one — the schema
    # enforces the sha256-hex shape, so the fixture uses a valid digest.
    other_cfg = "b11" + "0" * 61
    _seed(
        h,
        [
            new_audit_event(
                action="pdp.decide",
                resource="tool:y",
                outcome=AuditOutcome.success,
                actor="user:ada",
                detail="{}",
                trace_id="tr-cfg",
                config_hash=other_cfg,
                occurred_at=_at(40),
            )
        ],
    )
    assert _ids(_body(h, f"?config_hash={other_cfg}")) == ["tr-cfg"]
    assert "tr-cfg" not in _ids(_body(h, "?config_hash=" + "f" * 64))


def test_window_filters_on_trace_start(make_app: AppBuilder) -> None:
    h = make_app(ops_roles=("ops",))
    a, b, c = _seed_three_traces(h)

    # from/to select on the trace's STARTED_AT (min occurred_at) — a trace is
    # one object with one start, not a bag of rows.
    assert _ids(_body(h, "?from=2026-07-20T12:05:00%2B00:00")) == [c, b]
    assert _ids(_body(h, "?to=2026-07-20T12:05:00%2B00:00")) == [a]
    assert _ids(
        _body(
            h,
            "?from=2026-07-20T12:05:00%2B00:00&to=2026-07-20T12:15:00%2B00:00",
        )
    ) == [b]


def test_empty_index(make_app: AppBuilder) -> None:
    h = make_app(ops_roles=("ops",))
    body = _body(h)
    assert body["traces"] == []
    assert body["truncated"] is False
    assert body["verification"]["valid"] is True
