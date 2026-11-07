# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The trace read surface (NE-167a) — ops-gated tree assembly, agent-native parity.

Proves: ops-gate parity with /v1/audit/* (empty ops_roles ⇒ 403 for everyone,
audited); unknown trace_id ⇒ 404; the seq-ordered bracket tree assembly (a
subagent.dispatch/complete pair nests a subagent.route + child model call, the
complete folds into the span rather than appearing as a sibling); typed
per-node fields (model/lane/cost/tokens/redaction-category-counts/knowledge
acl_trim_delta/pdp/approval); per-model aggregates + run rollup; the reused
chain-verification verdict (and that a direct SQL tamper flips it); the
defensive pre-NE-163a posture (missing cost/token/response_model provenance
never errors); C7 (an un-enumerated provenance sentinel never crosses the
assembler); and read.trace agent-native parity through the SAME governed
executor (structural equality with the HTTP route body, ops-role enforcement,
fail-closed on an unset turn principal, catalog membership).
"""

from __future__ import annotations

import json
import uuid

import sqlalchemy as sa

from ne.api.tools import TRACE_TOOL_NAME, TURN_PRINCIPAL
from ne.contracts.types import Principal
from ne.events import new_approval_event, new_audit_event
from ne.runtime.errors import ToolExecutionError
from ne.schema import ApprovalDecision, AuditOutcome

from .conftest import AppBuilder, get_state, run


def _seed_trace(h, tid: str, *, with_cost: bool = True) -> None:
    """Append one run's correlated audit chain, all sharing ``trace_id=tid``.

    Mirrors the REAL emitters' exact detail/provenance shapes (gateway.py:252-351,
    retriever.py:415, pdp.py:120-139, dispatch.py:304/329/415-423). Also appends
    one OTHER-trace event to prove trace filtering.
    """
    state = get_state(h.app)

    async def _append() -> None:
        # (1) root model call — the C7 sentinel rides an UN-ENUMERATED provenance
        # key that must never cross the assembler.
        provenance = {
            "task": "chat",
            "lane": "private-vllm",
            "model_name": "qwen",
            "acl_labels": "SECRET//ACL-LABEL",
        }
        if with_cost:
            provenance.update(
                {
                    "cost_usd": "0.0021",
                    "input_tokens": "120",
                    "output_tokens": "45",
                    "response_model": "qwen-2.5",
                    # B12 keys, as the gateway writes them (stringified ints) —
                    # plus the principal, which the assembler must NOT surface
                    # (C7: no principal labels cross; the distinct sentinel
                    # value proves non-leak without colliding with the fixture
                    # actor strings).
                    "latency_ms": "1234",
                    "ttft_ms": "56",
                    "principal": "user:principal-sentinel",
                }
            )
        await state.sink.append(
            new_audit_event(
                action="ne.gateway.complete",
                resource="openai/stub",
                outcome=AuditOutcome.success,
                actor="user:ada",
                detail="task=chat lane=private-vllm model=qwen",
                trace_id=tid,
                provenance=provenance,
            )
        )
        # (2) knowledge retrieval
        await state.sink.append(
            new_audit_event(
                action="knowledge.retrieve",
                resource="knowledge://chunks",
                outcome=AuditOutcome.success,
                actor="user:ada",
                detail="candidates=8 returned=5 acl_trim_delta=3",
                trace_id=tid,
            )
        )
        # (3) pdp decision
        await state.sink.append(
            new_audit_event(
                action="pdp.decide",
                resource="tool:read.knowledge.search",
                outcome=AuditOutcome.success,
                actor="user:ada",
                detail=json.dumps(
                    {"effect": "allow", "reason": "ok", "tier": 0, "policy_version": "v1"}
                ),
                trace_id=tid,
                provenance={
                    "source": "ne-govern",
                    "policy_version": "v1",
                    "requested_action": "read.knowledge.search",
                    "effect": "allow",
                },
            )
        )
        # (4) pre-egress redaction
        await state.sink.append(
            new_audit_event(
                action="redact.pre_egress",
                resource="openai/stub",
                outcome=AuditOutcome.success,
                actor="user:ada",
                detail="pre-egress redaction: hits=2 categories=ssn=1,email=1",
                trace_id=tid,
                provenance={
                    "task": "chat",
                    "lane": "private-vllm",
                    "model_name": "qwen",
                    "redaction_hits": "2",
                    "redaction_categories": "ssn=1,email=1",
                },
            )
        )
        # (5) sub-agent dispatch (opens the bracket)
        await state.sink.append(
            new_audit_event(
                action="subagent.dispatch",
                resource="subagent:summarizer",
                outcome=AuditOutcome.success,
                actor="user:ada",
                detail=json.dumps({"agent": "summarizer", "depth": 1}),
                trace_id=tid,
                provenance={"source": "ne-runtime", "component": "dispatch"},
            )
        )
        # (6) per-child routing decision (nests under the open subagent span)
        await state.sink.append(
            new_audit_event(
                action="subagent.route",
                resource="subagent:summarizer",
                outcome=AuditOutcome.success,
                actor="user:ada",
                detail=json.dumps(
                    {
                        "agent": "summarizer",
                        "route_task": "summarize",
                        "route_lane": "private-vllm",
                        "tier": "private",
                        "task_source": "definition",
                        "lane_source": "definition",
                        "hint_clamped": False,
                    }
                ),
                trace_id=tid,
                provenance={"source": "ne-runtime", "component": "dispatch"},
            )
        )
        # (7) child model call — deliberately WITHOUT NE-163a keys (defensive path).
        await state.sink.append(
            new_audit_event(
                action="ne.gateway.complete",
                resource="private/phi",
                outcome=AuditOutcome.success,
                actor="user:ada",
                detail="task=summarize lane=private-vllm model=phi",
                trace_id=tid,
                provenance={"task": "summarize", "lane": "private-vllm", "model_name": "phi"},
            )
        )
        # (8) sub-agent completion (closes the bracket — folds, no sibling node)
        await state.sink.append(
            new_audit_event(
                action="subagent.complete",
                resource="subagent:summarizer",
                outcome=AuditOutcome.success,
                actor="user:ada",
                detail=json.dumps({"agent": "summarizer"}),
                trace_id=tid,
                provenance={"source": "ne-runtime", "component": "dispatch"},
            )
        )
        # (9) an approval lifecycle event
        await state.sink.append(
            new_approval_event(
                approval_id="ap-1",
                tier=2,
                decision=ApprovalDecision.approved,
                subject_ref="tool:write.x",
                actor="user:bob",
                trace_id=tid,
            )
        )
        # (10) a guardrail denial
        await state.sink.append(
            new_audit_event(
                action="guardrail.block",
                resource="tool:write.external.email",
                outcome=AuditOutcome.denied,
                actor="user:ada",
                detail="rule=recipients violation=recipient-not-allowed",
                trace_id=tid,
            )
        )
        # (11) an unrecognized action — must render as raw kind='event' (law 5).
        await state.sink.append(
            new_audit_event(
                action="custom.thing",
                resource="thing:1",
                outcome=AuditOutcome.success,
                actor="user:ada",
                detail="raw detail text",
                trace_id=tid,
            )
        )
        # OTHER-trace event — must never appear in this trace's assembly.
        await state.sink.append(
            new_audit_event(
                action="thing.other",
                resource="r",
                outcome=AuditOutcome.success,
                actor="user:ada",
                detail="unrelated",
                trace_id="other-" + uuid.uuid4().hex,
            )
        )

    run(_append())


def test_trace_requires_ops_role(make_app: AppBuilder) -> None:
    h = make_app(ops_roles=("ops",))
    tid = uuid.uuid4().hex
    _seed_trace(h, tid)

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get(f"/api/v1/trace/{tid}", headers=h.bearer("u", groups=["not-ops"]))
            assert r.status_code == 403

    run(_t())


def test_trace_empty_ops_roles_fails_closed(make_app: AppBuilder) -> None:
    h = make_app()  # ops_roles default ()
    tid = uuid.uuid4().hex
    _seed_trace(h, tid)

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get(
                f"/api/v1/trace/{tid}", headers=h.bearer("rich", groups=["ops", "admins"])
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


def test_trace_unknown_trace_404(make_app: AppBuilder) -> None:
    h = make_app(ops_roles=("ops",))

    async def _t() -> None:
        async with h.client() as client:
            headers = h.bearer("user:ops", groups=["ops"])
            r = await client.get("/api/v1/trace/does-not-exist", headers=headers)
            assert r.status_code == 404

    run(_t())


def test_trace_tree_spans_aggregates_and_verdict(make_app: AppBuilder) -> None:
    h = make_app(ops_roles=("ops",))
    tid = uuid.uuid4().hex
    _seed_trace(h, tid)

    async def _t() -> dict:
        async with h.client() as client:
            headers = h.bearer("user:ops", groups=["ops"])
            r = await client.get(f"/api/v1/trace/{tid}", headers=headers)
            assert r.status_code == 200
            return r.json()

    body = run(_t())
    assert set(body) == {"trace_id", "rollup", "models", "nodes", "verification"}
    assert body["trace_id"] == tid

    nodes = body["nodes"]
    # 8 top-level nodes: root model call, knowledge, pdp, redaction, subagent,
    # approval, guardrail, custom.thing — subagent.complete folded away.
    kinds = [n["kind"] for n in nodes]
    assert kinds == [
        "model_call",
        "knowledge",
        "pdp",
        "redaction",
        "subagent",
        "approval",
        "guardrail",
        "event",
    ]

    root_call = nodes[0]["model_call"]
    assert root_call["model_name"] == "qwen"
    assert root_call["response_model"] == "qwen-2.5"
    assert root_call["cost_usd"] == 0.0021
    assert root_call["input_tokens"] == 120
    assert root_call["output_tokens"] == 45
    # B12: the stringified provenance ints parse to numbers on the span...
    assert root_call["latency_ms"] == 1234
    assert root_call["ttft_ms"] == 56
    # ...and the principal provenance key deliberately does NOT cross the
    # assembler (C7 — per-principal reads are chain SQL, not trace fields).
    assert "principal" not in root_call
    assert "principal-sentinel" not in json.dumps(body)

    knowledge = nodes[1]["knowledge"]
    assert knowledge == {"candidates": 8, "returned": 5, "acl_trim_delta": 3}

    pdp = nodes[2]["pdp"]
    assert pdp["effect"] == "allow"
    assert pdp["tier"] == 0
    assert pdp["policy_version"] == "v1"
    assert pdp["requested_action"] == "read.knowledge.search"

    redaction = nodes[3]["redaction"]
    assert redaction["hits"] == 2
    assert redaction["categories"] == {"ssn": 1, "email": 1}

    subagent_node = nodes[4]
    assert subagent_node["kind"] == "subagent"
    span = subagent_node["subagent"]
    assert span["agent"] == "summarizer"
    assert span["depth"] == 1
    assert span["completed"] is True
    assert span["ended_at"] is not None
    assert span["duration_ms"] >= 0
    assert span["outcome"] == "success"

    children = subagent_node["children"]
    assert [c["kind"] for c in children] == ["subagent_route", "model_call"]
    route = children[0]["route"]
    assert route["agent"] == "summarizer"
    assert route["route_task"] == "summarize"
    assert route["route_lane"] == "private-vllm"
    assert route["tier"] == "private"
    assert route["hint_clamped"] is False
    child_call = children[1]["model_call"]
    assert child_call["model_name"] == "phi"
    assert child_call["response_model"] is None
    assert child_call["cost_usd"] is None
    assert child_call["input_tokens"] is None
    assert child_call["output_tokens"] is None
    # B12 absent side: a row without the keys is unknown — null, never 0.
    assert child_call["latency_ms"] is None
    assert child_call["ttft_ms"] is None

    approval = nodes[5]["approval"]
    assert approval == {
        "approval_id": "ap-1",
        "tier": 2,
        "decision": "approved",
        "subject_ref": "tool:write.x",
    }

    guardrail_node = nodes[6]
    guardrail_detail = guardrail_node["guardrail"]["detail"]
    assert guardrail_detail == "rule=recipients violation=recipient-not-allowed"

    unknown_node = nodes[7]
    assert unknown_node["kind"] == "event"
    assert unknown_node["action"] == "custom.thing"
    assert unknown_node["detail"] == "raw detail text"

    models = {m["model"]: m for m in body["models"]}
    assert models["qwen-2.5"]["calls"] == 1
    assert models["qwen-2.5"]["input_tokens"] == 120
    assert models["qwen-2.5"]["output_tokens"] == 45
    assert models["qwen-2.5"]["cost_usd"] == 0.0021
    assert models["phi"]["calls"] == 1
    assert models["phi"]["cost_usd"] is None

    rollup = body["rollup"]
    assert rollup["total_events"] == 11
    assert rollup["total_cost_usd"] == 0.0021

    assert body["verification"]["valid"] is True

    # The other-trace event never crosses into this assembly.
    assert "thing.other" not in json.dumps(body)


def test_trace_defensive_missing_cost_fields(make_app: AppBuilder) -> None:
    h = make_app(ops_roles=("ops",))
    tid = uuid.uuid4().hex
    _seed_trace(h, tid, with_cost=False)

    async def _t() -> dict:
        async with h.client() as client:
            headers = h.bearer("user:ops", groups=["ops"])
            r = await client.get(f"/api/v1/trace/{tid}", headers=headers)
            assert r.status_code == 200
            return r.json()

    body = run(_t())
    root_call = body["nodes"][0]["model_call"]
    assert root_call["model_name"] == "qwen"
    assert root_call["response_model"] is None
    assert root_call["cost_usd"] is None
    assert root_call["input_tokens"] is None
    assert root_call["output_tokens"] is None
    assert root_call["latency_ms"] is None
    assert root_call["ttft_ms"] is None
    assert body["rollup"]["total_cost_usd"] is None


def test_trace_zero_latency_parses_to_zero_not_absent(make_app: AppBuilder) -> None:
    """The B12 absent-vs-zero bidirectional, zero side: a provenance
    `latency_ms: "0"` / `ttft_ms: "0"` (a real sub-millisecond measurement) is
    served as 0 — a value the console renders "0 ms" — never collapsed into
    null/absent. The absent side is pinned above (missing keys ⇒ null)."""
    h = make_app(ops_roles=("ops",))
    tid = uuid.uuid4().hex
    state = get_state(h.app)

    async def _append() -> None:
        await state.sink.append(
            new_audit_event(
                action="ne.gateway.stream",
                resource="private/qwen",
                outcome=AuditOutcome.success,
                actor="ne-gateway",
                detail="task=chat lane=private-vllm model=qwen",
                trace_id=tid,
                provenance={
                    "task": "chat",
                    "lane": "private-vllm",
                    "model_name": "qwen",
                    "latency_ms": "0",
                    "ttft_ms": "0",
                },
            )
        )

    run(_append())

    async def _t() -> dict:
        async with h.client() as client:
            headers = h.bearer("user:ops", groups=["ops"])
            r = await client.get(f"/api/v1/trace/{tid}", headers=headers)
            assert r.status_code == 200
            return r.json()

    body = run(_t())
    call = body["nodes"][0]["model_call"]
    assert call["latency_ms"] == 0  # == 0 is False for null: served, not absent
    assert call["ttft_ms"] == 0


def test_trace_c7_no_unknown_provenance_leak(make_app: AppBuilder) -> None:
    h = make_app(ops_roles=("ops",))
    tid = uuid.uuid4().hex
    _seed_trace(h, tid)

    async def _t() -> None:
        async with h.client() as client:
            headers = h.bearer("user:ops", groups=["ops"])
            r = await client.get(f"/api/v1/trace/{tid}", headers=headers)
            assert r.status_code == 200
            assert "SECRET//ACL-LABEL" not in r.text

    run(_t())


def test_trace_tampered_row_flips_verdict(make_app: AppBuilder) -> None:
    h = make_app(ops_roles=("ops",))
    tid = uuid.uuid4().hex
    _seed_trace(h, tid)

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
            seq, raw_payload = row
            payload = json.loads(raw_payload) if isinstance(raw_payload, str) else dict(raw_payload)
            payload["actor"] = "forged"
            conn.execute(
                sa.text("UPDATE ne_audit_log SET payload = :p WHERE seq = :seq"),
                {"p": json.dumps(payload), "seq": seq},
            )
    finally:
        engine.dispose()

    async def _t() -> None:
        async with h.client() as client:
            headers = h.bearer("user:ops", groups=["ops"])
            r = await client.get(f"/api/v1/trace/{tid}", headers=headers)
            assert r.status_code == 200
            assert r.json()["verification"]["valid"] is False

    run(_t())


def test_trace_boundary_column_tamper_flips_verdict(make_app: AppBuilder) -> None:
    """A denormalized ``trace_id`` COLUMN tamper on the trace's first row must
    still flip the verdict to invalid, even though it makes ``replay()`` omit
    that row (and would shrink an occurred_at-derived verify WINDOW to exclude
    it too). Regression for the P1 finding: verifying only the window implied
    by ``replay()``'s own (tamperable-column-filtered) result let this exact
    attack hide behind a ``valid=True`` verdict over a silently-truncated trace.
    """
    h = make_app(ops_roles=("ops",))
    tid = uuid.uuid4().hex
    _seed_trace(h, tid)

    engine = sa.create_engine(h.audit_url)
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("DROP TRIGGER IF EXISTS ne_audit_log_no_update")
            row = conn.execute(
                sa.text("SELECT seq FROM ne_audit_log WHERE trace_id = :tid ORDER BY seq LIMIT 1"),
                {"tid": tid},
            ).first()
            assert row is not None
            seq = row[0]
            # Tamper ONLY the denormalized trace_id column — leave payload,
            # record_hash, and prev_hash untouched, exactly the attack the P1
            # finding describes (a column swap the hash chain itself never sees).
            conn.execute(
                sa.text("UPDATE ne_audit_log SET trace_id = :bogus WHERE seq = :seq"),
                {"bogus": "evil-" + uuid.uuid4().hex, "seq": seq},
            )
    finally:
        engine.dispose()

    async def _t() -> dict:
        async with h.client() as client:
            headers = h.bearer("user:ops", groups=["ops"])
            r = await client.get(f"/api/v1/trace/{tid}", headers=headers)
            assert r.status_code == 200
            return r.json()

    body = run(_t())
    # replay() legitimately omits the column-tampered row (it no longer matches
    # trace_id=tid by column) — proving the attack really does shrink the
    # assembled trace, which is exactly why the verdict must not be windowed
    # to that shrunken range.
    assert body["rollup"]["total_events"] == 10
    assert body["verification"]["valid"] is False


def test_read_trace_tool_matches_route(make_app: AppBuilder) -> None:
    h = make_app(ops_roles=("ops",))
    tid = uuid.uuid4().hex
    _seed_trace(h, tid)

    async def _route_body() -> dict:
        async with h.client() as client:
            headers = h.bearer("user:ops", groups=["ops"])
            r = await client.get(f"/api/v1/trace/{tid}", headers=headers)
            assert r.status_code == 200
            return r.json()

    route_body = run(_route_body())

    state = get_state(h.app)
    principal = Principal(id="user:ops", groups=("ops",))

    async def _tool_text() -> str:
        token = TURN_PRINCIPAL.set(principal)
        try:
            return await state.executor.execute(
                TRACE_TOOL_NAME, {"trace_id": tid}, principal=principal
            )
        finally:
            TURN_PRINCIPAL.reset(token)

    tool_text = run(_tool_text())
    tool_body = json.loads(tool_text)

    # ``verification.checked`` (and its echo inside ``detail``'s "N record(s)
    # verified" text) is a FULL (unbounded) chain-walk count (the P1 fix — see
    # trace_assembly.py's "VERIFICATION IS FULL-CHAIN, NOT WINDOWED" note): it
    # counts every row in the whole audit log at call time, not just this
    # trace's rows. The governed executor itself audits every tool execution
    # (``tool.execute``), so the intervening tool call above appends one more
    # row before its own verify walk runs — ``checked``/``detail`` legitimately
    # grow between the two calls. Everything else (the assembled trace itself,
    # and ``valid``/break fields) must still match exactly.
    assert tool_body["verification"]["checked"] >= route_body["verification"]["checked"]

    def _normalize(body: dict) -> dict:
        verification = {**body["verification"], "checked": None, "detail": None}
        return {**body, "verification": verification}

    assert _normalize(tool_body) == _normalize(route_body)


def test_read_trace_tool_requires_ops(make_app: AppBuilder) -> None:
    h = make_app(ops_roles=("ops",))
    tid = uuid.uuid4().hex
    _seed_trace(h, tid)

    state = get_state(h.app)
    principal = Principal(id="user:nope", groups=("not-ops",))

    async def _run_tool() -> None:
        token = TURN_PRINCIPAL.set(principal)
        try:
            await state.executor.execute(TRACE_TOOL_NAME, {"trace_id": tid}, principal=principal)
        finally:
            TURN_PRINCIPAL.reset(token)

    try:
        run(_run_tool())
        raised = False
    except ToolExecutionError:
        raised = True
    assert raised


def test_read_trace_tool_unset_principal_fails_closed(make_app: AppBuilder) -> None:
    h = make_app(ops_roles=("ops",))
    tid = uuid.uuid4().hex
    _seed_trace(h, tid)

    state = get_state(h.app)
    principal = Principal(id="user:ops", groups=("ops",))

    async def _run_tool() -> None:
        # TURN_PRINCIPAL deliberately left unset (default None) — the handler
        # must fail closed even though a principal is passed to execute().
        await state.executor.execute(TRACE_TOOL_NAME, {"trace_id": tid}, principal=principal)

    try:
        run(_run_tool())
        raised = False
    except ToolExecutionError:
        raised = True
    assert raised


def test_read_trace_tool_in_catalog(make_app: AppBuilder) -> None:
    h = make_app(ops_roles=("ops",))
    state = get_state(h.app)
    assert TRACE_TOOL_NAME in {s.name for s in state.executor.tool_specs}


# ---------------------------------------------------------------------------
# B14 (NE-OBS-U5) — the execution→approval join on the tool node.
# ---------------------------------------------------------------------------


def _seed_join_trace(h, tid: str) -> dict[str, str]:
    """A two-leg-shaped trace at the ASSEMBLY level: two executed writes, each
    stamped with a DIFFERENT approval id (the anti-constant discrimination —
    each node must thread ITS OWN), plus one pre-B14 execution with no stamp
    (the absence leg), plus the matching approval lifecycle events."""
    state = get_state(h.app)
    ids = {
        "grant_a": str(uuid.uuid4()),
        "grant_b": str(uuid.uuid4()),
    }

    async def _append() -> None:
        for key, action_name in (("grant_a", "write.crm.note"), ("grant_b", "write.erp.update")):
            await state.sink.append(
                new_approval_event(
                    approval_id=ids[key],
                    tier=2,
                    decision=ApprovalDecision.approved,
                    subject_ref=f"tool:{action_name}",
                    actor="user:carol",
                    trace_id=tid,
                )
            )
            # The C6 executor's tool.execute emit shape (middleware.py), with
            # the B14 stamp — plus an UN-ENUMERATED provenance sentinel that
            # must never cross the assembler (same C7 discipline as _seed_trace).
            await state.sink.append(
                new_audit_event(
                    action="tool.execute",
                    resource=f"tool:{action_name}",
                    outcome=AuditOutcome.success,
                    actor="user:ada",
                    detail=json.dumps({"what_action": action_name}),
                    trace_id=tid,
                    provenance={
                        "source": "ne-runtime",
                        "component": "middleware",
                        "approval_id": ids[key],
                        "approver_note": "SECRET//JOIN-SENTINEL",
                    },
                )
            )
        # PRE-B14 leg: an execution recorded before the field existed.
        await state.sink.append(
            new_audit_event(
                action="tool.execute",
                resource="tool:write.legacy.append",
                outcome=AuditOutcome.success,
                actor="user:ada",
                detail=json.dumps({"what_action": "write.legacy.append"}),
                trace_id=tid,
                provenance={"source": "ne-runtime", "component": "middleware"},
            )
        )

    run(_append())
    return ids


def test_tool_node_threads_its_own_approval_id(make_app: AppBuilder) -> None:
    """Each executed write's tool node carries the approval id STAMPED ON ITS
    OWN RECORD — two different ids in one trace prove the assembler threads
    per-record values (a constant or a trace-wide fold fails one of the two) —
    and the pre-B14 record serves ``approval_id: null``, never an inferred id,
    even though granted approval events sit adjacent in the same trace."""
    h = make_app(ops_roles=("ops",))
    tid = uuid.uuid4().hex
    ids = _seed_join_trace(h, tid)

    async def _t() -> dict:
        async with h.client() as client:
            r = await client.get(f"/api/v1/trace/{tid}", headers=h.bearer("u", groups=["ops"]))
            assert r.status_code == 200
            assert "SECRET//JOIN-SENTINEL" not in r.text  # C7: no wildcard copy
            return r.json()

    body = run(_t())
    tool_nodes = [n for n in body["nodes"] if n["kind"] == "tool"]
    assert [n["action"] for n in tool_nodes] == ["tool.execute"] * 3

    by_resource = {n["resource"]: n for n in tool_nodes}
    node_a = by_resource["tool:write.crm.note"]
    node_b = by_resource["tool:write.erp.update"]
    legacy = by_resource["tool:write.legacy.append"]

    # THE JOIN, per record: each node's id equals ITS OWN stamp...
    assert node_a["tool"]["approval_id"] == ids["grant_a"]
    assert node_b["tool"]["approval_id"] == ids["grant_b"]
    # ...and the two differ (fixture non-degeneracy control).
    assert ids["grant_a"] != ids["grant_b"]

    # ABSENCE: the pre-B14 record has no stamp and serves null — NOT one of
    # the ids from the adjacent approval events (an inferred join would have
    # exactly one to reach for).
    assert legacy["tool"]["approval_id"] is None

    # detail threading survived the node becoming typed.
    assert json.loads(node_a["detail"]) == {"what_action": "write.crm.note"}

    # The typed span carries EXACTLY the enumerated fields (C12/C7: nothing
    # rides along with the join). NE-TRACE-LEGIBILITY added the verified-
    # executor lifts — names and counts only — and on a WRITE tool's record
    # every one of them is absent, never invented.
    assert set(node_a["tool"]) == {
        "approval_id",
        "template_id",
        "template_version",
        "args",
        "result_count",
    }
    assert node_a["tool"]["template_id"] is None
    assert node_a["tool"]["args"] == []
    assert node_a["tool"]["result_count"] is None
