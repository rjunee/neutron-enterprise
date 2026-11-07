# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Contract-shape sweep — response key sets as LITERALS mirroring types.ts.

A drifted key is a console break, so every field set is asserted as a literal set.
"""

from __future__ import annotations

from .conftest import (
    AppBuilder,
    chunk,
    parse_sse,
    run,
    text_completion,
)

_DEPT = "engineering"


def _auth(h):  # type: ignore[no-untyped-def]
    return h.bearer("user:ada", groups=["eng"], department=_DEPT)


def test_me_keys(make_app: AppBuilder) -> None:
    h = make_app()

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get("/api/v1/me", headers=_auth(h))
            assert r.status_code == 200
            # ``entitlements`` names the coarse surfaces this principal's groups
            # satisfy, computed from the SAME fail-closed role tuples the guarded
            # routes check. It exists so the console can stop issuing governed
            # reads the server has already said it will refuse — that traffic was
            # ~100 audited denials per audit page and it moved a business metric
            # while a reviewer browsed. It grants nothing: every route still runs
            # its own check. ``claims`` are still never echoed.
            assert set(r.json()) == {"id", "groups", "entitlements"}
            # Fail closed: this deployment configures no role tuples, so the
            # principal satisfies no surface.
            assert r.json()["entitlements"] == []

    run(_t())


def test_create_session_keys(make_app: AppBuilder) -> None:
    h = make_app()

    async def _t() -> None:
        async with h.client() as client:
            r = await client.post(
                "/api/v1/chat/sessions", json={"agent_name": "console-chat"}, headers=_auth(h)
            )
            assert r.status_code == 200
            assert set(r.json()) == {"session_id", "agent_name"}

    run(_t())


def test_brain_search_and_retrieve_shapes(make_app: AppBuilder) -> None:
    h = make_app(chunks=[chunk("c1", acl=["eng"], text="alpha")])

    async def _t() -> None:
        async with h.client() as client:
            r = await client.post(
                "/api/v1/brain/search", json={"query": "alpha", "top_k": 5}, headers=_auth(h)
            )
            assert r.status_code == 200
            body = r.json()
            # NE-BRAIN-T2-WIRING: graph_paths is an additive key (F3). make_app wires
            # no graph tier ⇒ it is always [] — the console can ignore it.
            # NE-T2X-4: provisional_source_count is a REQUIRED, no-default key —
            # 0 when there are no graph_paths to derive it from.
            expected_keys = {"chunks", "citations", "graph_paths", "provisional_source_count"}
            assert set(body) == expected_keys
            assert body["graph_paths"] == []
            assert body["provisional_source_count"] == 0
            assert body["chunks"], "expected the eng-labeled chunk"
            sc = body["chunks"][0]
            assert set(sc) == {"chunk", "score"}
            assert set(sc["chunk"]) == {"chunk_id", "document_id", "text", "metadata"}
            cit = body["citations"][0]
            assert set(cit) == {"chunk_id", "document_id", "source_system", "source_ref"}

            r2 = await client.post(
                "/api/v1/brain/retrieve", json={"chunk_ids": ["c1"]}, headers=_auth(h)
            )
            assert r2.status_code == 200
            assert set(r2.json()) == expected_keys
            assert r2.json()["graph_paths"] == []
            assert r2.json()["provisional_source_count"] == 0

    run(_t())


def test_sse_payload_shapes(make_app: AppBuilder) -> None:
    h = make_app(completions=[text_completion("final answer")])

    async def _t() -> None:
        async with h.client() as client:
            created = await client.post(
                "/api/v1/chat/sessions", json={"agent_name": "console-chat"}, headers=_auth(h)
            )
            sid = created.json()["session_id"]
            r = await client.post(
                f"/api/v1/chat/sessions/{sid}/messages", json={"prompt": "hi"}, headers=_auth(h)
            )
            assert r.status_code == 200
            parsed = parse_sse(r.text)
            token = parsed.of_type("token")[0]
            done = parsed.of_type("done")[0]
            assert token.data is not None and set(token.data) == {"type", "delta"}
            assert done.data is not None and set(done.data) == {
                "type",
                "session_id",
                "output",
                "message_id",
            }
            assert isinstance(done.data["message_id"], str) and done.data["message_id"]

    run(_t())


def test_sse_error_payload_shape(make_app: AppBuilder) -> None:
    from .conftest import RaisingGateway

    h = make_app(gateway=RaisingGateway())

    async def _t() -> None:
        async with h.client() as client:
            created = await client.post(
                "/api/v1/chat/sessions", json={"agent_name": "console-chat"}, headers=_auth(h)
            )
            sid = created.json()["session_id"]
            r = await client.post(
                f"/api/v1/chat/sessions/{sid}/messages", json={"prompt": "hi"}, headers=_auth(h)
            )
            assert r.status_code == 200
            err = parse_sse(r.text).of_type("error")[0]
            assert err.data is not None and set(err.data) == {"type", "detail"}

    run(_t())


def test_4xx_bodies_are_string_detail(make_app: AppBuilder) -> None:
    h = make_app()

    async def _t() -> None:
        async with h.client() as client:
            # A validation 422 whose detail is a SINGLE flattened string.
            r = await client.post(
                "/api/v1/brain/search", json={"query": "x", "top_k": 0}, headers=_auth(h)
            )
            assert r.status_code == 422
            body = r.json()
            assert set(body) == {"detail"}
            assert isinstance(body["detail"], str)

            # A 404 from an unknown session — also {"detail": str}.
            r2 = await client.post(
                "/api/v1/chat/sessions/nope/messages", json={"prompt": "hi"}, headers=_auth(h)
            )
            assert r2.status_code == 404
            assert isinstance(r2.json()["detail"], str)

    run(_t())


def test_ops_wire_shapes_are_literal_key_sets() -> None:
    # The wire schemas are the frozen console contract — assert each field set as a
    # LITERAL (a drifted key is a console break). Asserted on the pydantic models
    # directly so the contract holds independent of any single populated response.
    from ne.api.wire import (
        ApprovalActionResultView,
        AuditAnchorView,
        AuditEventsPageView,
        AuditRecordView,
        ChainVerificationView,
        EvalRunView,
        KpiEntryView,
        KpiSummaryView,
        PendingApprovalView,
        ReplayResultView,
    )

    # authorization_expires_at admitted 2026-08-29 — the run owner's frozen
    # authorization expiry, i.e. the REAL decision deadline for a run-bound
    # approval. `expires_at` above is the approval REQUEST's own deadline (24 h
    # by default) and the queue previously served only that, so approvers were
    # shown a day for decisions that died in ~15 minutes
    # (docs/architecture/approval-horizon.md). Optional and None for a
    # standalone approval, which has no owner snapshot.
    assert set(PendingApprovalView.model_fields) == {
        "request_id",
        "action",
        "tier",
        "principal_id",
        "requested_at",
        "sla_deadline",
        "expires_at",
        "escalated",
        "authorization_expires_at",
    }
    assert set(ApprovalActionResultView.model_fields) == {"status", "approver", "decided_at"}
    assert set(AuditRecordView.model_fields) == {
        "id",
        "seq",
        "event_id",
        "event_type",
        "config_hash",
        "actor",
        "occurred_at",
        "kpi_tag",
        "trace_id",
        "action",
        "resource",
        "outcome",
        "payload",
        "prev_hash",
        "record_hash",
    }
    # `basis` and `contradiction` admitted 2026-09-11. The page verdict is a
    # COMPOSITE (a prefix a full verify blessed up to CHAIN_ANCHOR_MAX_AGE_S ago
    # plus the rows appended since), and with neither field on the wire the
    # console could not tell an anchored verdict from a full walk: it labelled
    # anchored counts "the entire audit chain verified", let an anchored pass
    # withdraw a break a full walk had confirmed, and could never latch the
    # one-shot contradiction the verifier emits when the ledger stops extending
    # a head it verified.
    assert set(ChainVerificationView.model_fields) == {
        "valid",
        "basis",
        "contradiction",
        "checked",
        "first_break_seq",
        "first_break_event_id",
        "reason",
        "detail",
    }
    assert set(AuditEventsPageView.model_fields) == {"events", "verification", "anchor"}
    assert set(AuditAnchorView.model_fields) == {"seq", "status", "head_seq"}
    assert set(ReplayResultView.model_fields) == {"events", "config_hashes", "context_ids"}
    assert set(EvalRunView.model_fields) == {
        "run_id",
        "suite",
        "config_hash",
        "golden_set_hash",
        "judge_model_id",
        "judge_family",
        "rubric_versions",
        "metrics",
        "thresholds",
        "statuses",
        "judge_error_rate",
        "passed",
        "created_at",
    }
    assert set(KpiEntryView.model_fields) == {
        "kpi_tag",
        "value",
        "measurement_status",
        "unit",
        "dimensions",
        "window_start",
        "window_end",
    }
    assert set(KpiSummaryView.model_fields) == {"kpis", "generated_at"}


def test_steward_wire_shapes_are_literal_key_sets() -> None:
    from ne.api.wire import (
        DiffArtifactView,
        ReviewEventView,
        StewardDigestView,
        StewardLaneDigestView,
        StewardLatencyView,
        StewardProposalsView,
        StewardProposalView,
        StewardSkillsDigestView,
        StewardSkillsFailureView,
        StewardSkillsNearDupView,
        StewardSkillsOverlapView,
        StewardSkillsRetirementView,
        StewardSkillsStaleProposalView,
        StewardSkillsUnknownToolsView,
        StewardTotalsView,
    )

    assert set(StewardProposalView.model_fields) == {
        "proposal_id",
        "lane",
        "kind",
        "status",
        "cluster_id",
        "confidence",
        "priority",
        "evidence_event_ids",
        "evidence_signal_ids",
        "config_hash",
        "created_at",
        "artifact",
        "skill",
    }
    # Structural proof: NO summary/payload key exists on the wire model.
    assert "summary" not in StewardProposalView.model_fields
    assert "payload" not in StewardProposalView.model_fields
    assert set(DiffArtifactView.model_fields) == {
        "proposal_id",
        "lane",
        "kind",
        "path",
        "title",
        "patch",
        "pr_body",
    }
    assert set(StewardProposalsView.model_fields) == {"proposals"}
    assert set(ReviewEventView.model_fields) == {
        "review_id",
        "proposal_id",
        "action",
        "from_status",
        "to_status",
        "actor",
        "surface",
        "session_id",
        "rejection_code",
        "note",
        "applied_ref",
        "config_hash",
        "created_at",
    }
    # The StoredReviewEvent surrogate `id` is DROPPED on the wire.
    assert "id" not in ReviewEventView.model_fields
    assert set(StewardDigestView.model_fields) == {
        "week_start",
        "week_end",
        "generated_at",
        "scope",
        "totals",
        "by_lane",
        "skills",
    }
    # NE-SKILL-4 rot card views (field-by-field, never model_dump of internals).
    assert set(StewardSkillsDigestView.model_fields) == {
        "ran_at",
        "retirement_candidates",
        "failure_flags",
        "trigger_overlaps",
        "near_dups",
        "unknown_requires_tools",
        "stale_proposals",
    }
    assert set(StewardSkillsRetirementView.model_fields) == {"name", "reason", "fired_count"}
    assert set(StewardSkillsFailureView.model_fields) == {
        "name",
        "known_fires",
        "failed",
        "ratio",
    }
    assert set(StewardSkillsOverlapView.model_fields) == {"phrase", "names"}
    assert set(StewardSkillsNearDupView.model_fields) == {"a", "b", "method", "score"}
    assert set(StewardSkillsUnknownToolsView.model_fields) == {"name", "unknown"}
    assert set(StewardSkillsStaleProposalView.model_fields) == {
        "proposal_id",
        "kind",
        "name",
        "age_days",
    }
    assert set(StewardTotalsView.model_fields) == {"proposed", "approved", "rejected", "applied"}
    assert set(StewardLaneDigestView.model_fields) == {
        "lane",
        "proposed",
        "approved",
        "rejected",
        "applied",
    }
    assert set(StewardLatencyView.model_fields) == {
        "median_wait_s",
        "p90_wait_s",
        "decided_count",
        "pending_count",
        "oldest_pending_age_s",
        "generated_at",
    }


def test_ops_4xx_bodies_are_string_detail(make_app: AppBuilder) -> None:
    h = make_app(approver_roles=("approvers",), ops_roles=("ops",))

    async def _t() -> None:
        async with h.client() as client:
            # 403 role pre-check (approvals) — string detail.
            r = await client.get(
                "/api/v1/approvals/pending", headers=h.bearer("u", groups=["nope"])
            )
            assert r.status_code == 403
            assert set(r.json()) == {"detail"} and isinstance(r.json()["detail"], str)

            # 422 malformed audit filter — string detail.
            r2 = await client.get(
                "/api/v1/audit/events?from=bad", headers=h.bearer("o", groups=["ops"])
            )
            assert r2.status_code == 422
            assert set(r2.json()) == {"detail"} and isinstance(r2.json()["detail"], str)

    run(_t())


def test_me_entitlements_report_the_surfaces_a_role_satisfies() -> None:
    """The surfaces a principal's groups satisfy, by the SAME rule the routes use.

    Pure unit over :func:`ne.api.routers.me.entitlements_for` — no request needed,
    because the rule is the thing under test. It mirrors ``authz.require_role``:
    an empty configured tuple admits nobody (fail closed) and disjoint groups
    admit nobody. A regression here would either strand a console user behind a
    lock they should not see, or send them back to polling surfaces the server
    refuses — which is the traffic this field exists to stop.
    """
    from types import SimpleNamespace

    from ne.api.routers.me import entitlements_for

    settings = SimpleNamespace(
        approver_roles=("approvers",),
        steward_roles=("stewards",),
        ops_roles=("ops",),
    )

    assert entitlements_for(["approvers"], settings) == ["approvals"]
    assert entitlements_for(["ops"], settings) == ["ops"]
    assert entitlements_for(["approvers", "ops"], settings) == ["approvals", "ops"]
    # CONTROL: a group that satisfies nothing yields nothing — not everything.
    assert entitlements_for(["all-employees"], settings) == []
    # CONTROL: fail closed. An UNCONFIGURED role tuple admits nobody, even a
    # principal whose group name happens to match the surface name.
    empty = SimpleNamespace(approver_roles=(), steward_roles=(), ops_roles=())
    assert entitlements_for(["approvers", "ops", "stewards"], empty) == []
