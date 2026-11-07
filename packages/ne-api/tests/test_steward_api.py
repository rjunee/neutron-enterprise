# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Steward surfaces (§1.7) over the REAL ProposalReviewService + proposal store.

The governed spine is NEVER mocked: every approve/reject/apply rides the real
NE-066 review gate over SQLite, so the transition map, the rejection→signal loop,
and the dual-appended audit chain are all exercised through HTTP. Only the model/
embedder/qdrant edges are faked (conftest).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa

from ne.api.steward_metrics import iso_week_start
from ne.audit import list_records
from ne.flywheel.review import ne_proposal_reviews
from ne.flywheel.store import ne_correction_signals

from .conftest import AppBuilder, get_state, run, seed_proposal

_ROLES = ("steward",)


def _steward(h: Any) -> dict[str, str]:
    return h.bearer("user:steward", groups=["steward"])


def _count_reviews(state: Any) -> int:
    with state.review_service.engine.connect() as conn:
        return int(
            conn.execute(sa.select(sa.func.count()).select_from(ne_proposal_reviews)).scalar_one()
        )


def _find_no_key(node: Any, key: str) -> None:
    if isinstance(node, dict):
        assert key not in node, f"{key!r} key leaked in {node!r}"
        for v in node.values():
            _find_no_key(v, key)
    elif isinstance(node, list):
        for v in node:
            _find_no_key(v, key)


def test_proposals_list_non_terminal_only_and_never_leaks_raw_text(make_app: AppBuilder) -> None:
    h = make_app(steward_roles=_ROLES)
    base = datetime(2026, 6, 1, tzinfo=UTC)
    p1 = seed_proposal(h.app, lane="content", created_at=base + timedelta(seconds=1))
    p2 = seed_proposal(h.app, lane="content", created_at=base + timedelta(seconds=2))
    p3 = seed_proposal(h.app, lane="retrieval", created_at=base + timedelta(seconds=3))
    p4 = seed_proposal(h.app, lane="policy", created_at=base + timedelta(seconds=4))

    async def _t() -> None:
        async with h.client() as client:
            hdr = _steward(h)
            assert (
                await client.post(
                    f"/api/v1/steward/proposals/{p1}/approve", json={"note": ""}, headers=hdr
                )
            ).status_code == 200
            assert (
                await client.post(
                    f"/api/v1/steward/proposals/{p2}/reject",
                    json={"code": "incorrect", "note": ""},
                    headers=hdr,
                )
            ).status_code == 200
            assert (
                await client.post(
                    f"/api/v1/steward/proposals/{p3}/approve", json={"note": ""}, headers=hdr
                )
            ).status_code == 200
            assert (
                await client.post(
                    f"/api/v1/steward/proposals/{p3}/apply",
                    json={"applied_ref": "pr#1", "note": ""},
                    headers=hdr,
                )
            ).status_code == 200

            r = await client.get("/api/v1/steward/proposals", headers=hdr)
            assert r.status_code == 200
            proposals = r.json()["proposals"]
            # Only non-terminal rows: p1 (approved) + p4 (proposed), oldest-first.
            assert [item["proposal_id"] for item in proposals] == [p1, p4]
            assert proposals[0]["status"] == "approved"
            assert proposals[1]["status"] == "proposed"
            # No summary/payload key anywhere; the raw payload sentinel never renders.
            _find_no_key(r.json(), "summary")
            _find_no_key(r.json(), "payload")
            assert "SMUGGLE-PAYLOAD" not in r.text
            # content lane renders a git unified diff.
            assert proposals[0]["artifact"]["kind"] == "git_unified_diff"

    run(_t())


def test_approve_twice_second_is_409(make_app: AppBuilder) -> None:
    h = make_app(steward_roles=_ROLES)
    pid = seed_proposal(h.app)

    async def _t() -> None:
        async with h.client() as client:
            hdr = _steward(h)
            r1 = await client.post(
                f"/api/v1/steward/proposals/{pid}/approve", json={"note": ""}, headers=hdr
            )
            assert r1.status_code == 200
            assert r1.json()["to_status"] == "approved"
            r2 = await client.post(
                f"/api/v1/steward/proposals/{pid}/approve", json={"note": ""}, headers=hdr
            )
            assert r2.status_code == 409
            assert isinstance(r2.json()["detail"], str)

    run(_t())


def test_apply_from_proposed_is_409(make_app: AppBuilder) -> None:
    h = make_app(steward_roles=_ROLES)
    pid = seed_proposal(h.app)

    async def _t() -> None:
        async with h.client() as client:
            r = await client.post(
                f"/api/v1/steward/proposals/{pid}/apply",
                json={"applied_ref": "pr#1", "note": ""},
                headers=_steward(h),
            )
            assert r.status_code == 409  # human approval is a distinct recorded act

    run(_t())


def test_apply_empty_applied_ref_is_422(make_app: AppBuilder) -> None:
    h = make_app(steward_roles=_ROLES)
    pid = seed_proposal(h.app)
    state = get_state(h.app)

    async def _t() -> None:
        async with h.client() as client:
            hdr = _steward(h)
            for ref in ("", "   "):
                r = await client.post(
                    f"/api/v1/steward/proposals/{pid}/apply",
                    json={"applied_ref": ref, "note": ""},
                    headers=hdr,
                )
                assert r.status_code == 422
            assert _count_reviews(state) == 0  # no review row appended

    run(_t())


def test_unknown_proposal_is_404(make_app: AppBuilder) -> None:
    h = make_app(steward_roles=_ROLES)

    async def _t() -> None:
        async with h.client() as client:
            hdr = _steward(h)
            approve = await client.post(
                "/api/v1/steward/proposals/nope/approve", json={"note": ""}, headers=hdr
            )
            reject = await client.post(
                "/api/v1/steward/proposals/nope/reject",
                json={"code": "incorrect", "note": ""},
                headers=hdr,
            )
            apply_ = await client.post(
                "/api/v1/steward/proposals/nope/apply",
                json={"applied_ref": "r", "note": ""},
                headers=hdr,
            )
            for r in (approve, reject, apply_):
                assert r.status_code == 404
                assert r.json()["detail"] == "proposal not found"

    run(_t())


def test_unknown_rejection_code_is_422(make_app: AppBuilder) -> None:
    h = make_app(steward_roles=_ROLES)
    pid = seed_proposal(h.app)

    async def _t() -> None:
        async with h.client() as client:
            r = await client.post(
                f"/api/v1/steward/proposals/{pid}/reject",
                json={"code": "meh", "note": ""},
                headers=_steward(h),
            )
            assert r.status_code == 422  # RejectionCode enum validation

    run(_t())


def test_reject_emits_thumbs_down_signal_through_http(make_app: AppBuilder) -> None:
    h = make_app(steward_roles=_ROLES)
    pid = seed_proposal(h.app)
    state = get_state(h.app)

    async def _t() -> None:
        async with h.client() as client:
            r = await client.post(
                f"/api/v1/steward/proposals/{pid}/reject",
                json={"code": "incorrect", "note": ""},
                headers=_steward(h),
            )
            assert r.status_code == 200

    run(_t())

    assert state.review_service is not None
    with state.review_service.engine.connect() as conn:
        rows = conn.execute(sa.select(ne_correction_signals)).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.signal_kind == "thumbs_down"
    assert row.subject_ref == f"proposal:{pid}"
    assert row.actor == "user:steward"
    assert row.surface == "console"


def test_body_smuggling_is_422_and_legal_call_records_bearer_identity(
    make_app: AppBuilder,
) -> None:
    h = make_app(steward_roles=_ROLES)
    smuggle_target = seed_proposal(h.app)
    legal = seed_proposal(h.app)
    with_sid = seed_proposal(h.app)
    without_sid = seed_proposal(h.app)
    state = get_state(h.app)

    async def _t() -> None:
        async with h.client() as client:
            hdr = _steward(h)
            for extra in ({"actor": "mallory"}, {"surface": "slack"}, {"session_id": "x"}):
                r = await client.post(
                    f"/api/v1/steward/proposals/{smuggle_target}/approve",
                    json={"note": "", **extra},
                    headers=hdr,
                )
                assert r.status_code == 422
            assert _count_reviews(state) == 0  # nothing written by any smuggle

            # A legal approve records the BEARER as actor + console surface.
            r = await client.post(
                f"/api/v1/steward/proposals/{legal}/approve", json={"note": ""}, headers=hdr
            )
            assert r.status_code == 200
            body = r.json()
            assert body["actor"] == "user:steward"
            assert body["surface"] == "console"

            # A minted sid claim becomes the provenance session_id.
            r_sid = await client.post(
                f"/api/v1/steward/proposals/{with_sid}/approve",
                json={"note": ""},
                headers=h.bearer("user:steward", groups=["steward"], sid="sid-123"),
            )
            assert r_sid.status_code == 200
            assert r_sid.json()["session_id"] == "sid-123"

            # No sid → a non-empty server-generated id, never anything client-sent.
            r_no = await client.post(
                f"/api/v1/steward/proposals/{without_sid}/approve",
                json={"note": ""},
                headers=hdr,
            )
            assert r_no.status_code == 200
            sess = r_no.json()["session_id"]
            assert sess and sess not in {"sid-123", "x", ""}

    run(_t())

    # The persisted review row matches the returned view (actor + surface).
    assert state.review_service is not None
    with state.review_service.engine.connect() as conn:
        row = conn.execute(
            sa.select(ne_proposal_reviews).where(ne_proposal_reviews.c.proposal_id == legal)
        ).one()
    assert row.actor == "user:steward"
    assert row.surface == "console"


def test_empty_steward_roles_fail_closed_and_audited(make_app: AppBuilder) -> None:
    h = make_app(steward_roles=())  # empty ⇒ nobody, even a group-rich principal
    pid = seed_proposal(h.app)

    async def _t() -> None:
        async with h.client() as client:
            headers = h.bearer("user:rich", groups=["steward", "admins", "ops"])
            r1 = await client.get("/api/v1/steward/proposals", headers=headers)
            assert r1.status_code == 403
            r2 = await client.post(
                f"/api/v1/steward/proposals/{pid}/approve", json={"note": ""}, headers=headers
            )
            assert r2.status_code == 403

    run(_t())

    engine = sa.create_engine(h.audit_url)
    try:
        rows = list_records(engine, action="api.steward.unauthorized")
    finally:
        engine.dispose()
    assert len(rows) >= 1
    assert rows[0].actor == "user:rich"


def test_unauthorized_malformed_body_is_403_not_422(make_app: AppBuilder) -> None:
    h = make_app(steward_roles=_ROLES)
    pid = seed_proposal(h.app)

    async def _t() -> None:
        async with h.client() as client:
            headers = h.bearer("user:wrong", groups=["eng"])  # lacks the steward role
            r = await client.post(
                f"/api/v1/steward/proposals/{pid}/approve",
                json={"actor": "mallory", "garbage": 1},  # malformed (extra=forbid)
                headers=headers,
            )
            assert r.status_code == 403  # role-before-body-validation, not 422

    run(_t())

    engine = sa.create_engine(h.audit_url)
    try:
        rows = list_records(engine, action="api.steward.unauthorized")
    finally:
        engine.dispose()
    assert len(rows) >= 1


def test_wrong_role_vs_right_role(make_app: AppBuilder) -> None:
    h = make_app(steward_roles=_ROLES)
    pid = seed_proposal(h.app)

    async def _t() -> None:
        async with h.client() as client:
            ok = await client.post(
                f"/api/v1/steward/proposals/{pid}/approve",
                json={"note": ""},
                headers=h.bearer("user:s", groups=["steward"]),
            )
            assert ok.status_code == 200
            bad = await client.get(
                "/api/v1/steward/proposals", headers=h.bearer("user:e", groups=["eng"])
            )
            assert bad.status_code == 403

    run(_t())


def test_digest_http_shape_and_counts(make_app: AppBuilder) -> None:
    h = make_app(steward_roles=_ROLES)
    c1 = seed_proposal(h.app, lane="content")
    seed_proposal(h.app, lane="content")
    pol = seed_proposal(h.app, lane="policy")

    async def _t() -> None:
        async with h.client() as client:
            hdr = _steward(h)
            before = iso_week_start(datetime.now(UTC))
            await client.post(
                f"/api/v1/steward/proposals/{c1}/approve", json={"note": ""}, headers=hdr
            )
            await client.post(
                f"/api/v1/steward/proposals/{pol}/reject",
                json={"code": "incorrect", "note": ""},
                headers=hdr,
            )

            r = await client.get("/api/v1/steward/digest", headers=hdr)
            after = iso_week_start(datetime.now(UTC))
            assert r.status_code == 200
            body = r.json()
            # NE-SKILL-4: the optional rot card rides the digest (null with no run).
            assert set(body) == {
                "week_start",
                "week_end",
                "generated_at",
                "totals",
                "by_lane",
                "skills",
                "scope",
            }
            assert body["scope"] == "principal_visible"
            assert body["skills"] is None
            assert set(body["totals"]) == {"proposed", "approved", "rejected", "applied"}
            # YYYY-MM-DD, 7 days apart.
            ws = datetime.fromisoformat(body["week_start"]).date()
            we = datetime.fromisoformat(body["week_end"]).date()
            assert (we - ws).days == 7
            if before == after:
                assert body["totals"] == {"proposed": 3, "approved": 1, "rejected": 1, "applied": 0}
                assert [ld["lane"] for ld in body["by_lane"]] == ["content", "policy"]

            # An explicit past week has zero activity → all-zero + empty by_lane.
            past = await client.get("/api/v1/steward/digest?week_start=2020-01-06", headers=hdr)
            assert past.status_code == 200
            pb = past.json()
            assert pb["totals"] == {"proposed": 0, "approved": 0, "rejected": 0, "applied": 0}
            assert pb["by_lane"] == []
            assert pb["week_start"] == "2020-01-06"
            assert pb["week_end"] == "2020-01-13"

    run(_t())


def test_digest_malformed_week_start_is_422(make_app: AppBuilder) -> None:
    h = make_app(steward_roles=_ROLES)

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get(
                "/api/v1/steward/digest?week_start=not-a-date", headers=_steward(h)
            )
            assert r.status_code == 422
            assert isinstance(r.json()["detail"], str)

    run(_t())


def test_latency_http_nulls_when_empty_and_populated(make_app: AppBuilder) -> None:
    h = make_app(steward_roles=_ROLES)

    async def _t() -> None:
        async with h.client() as client:
            hdr = _steward(h)
            r0 = await client.get("/api/v1/steward/latency", headers=hdr)
            assert r0.status_code == 200
            assert r0.json() == {
                "median_wait_s": None,
                "p90_wait_s": None,
                "decided_count": 0,
                "pending_count": 0,
                "oldest_pending_age_s": None,
                "generated_at": r0.json()["generated_at"],
            }

            pid = seed_proposal(h.app, created_at=datetime.now(UTC) - timedelta(seconds=5))
            r1 = await client.get("/api/v1/steward/latency", headers=hdr)
            b1 = r1.json()
            assert b1["pending_count"] == 1
            assert b1["oldest_pending_age_s"] >= 0
            assert b1["median_wait_s"] is None
            assert b1["decided_count"] == 0

            await client.post(
                f"/api/v1/steward/proposals/{pid}/approve", json={"note": ""}, headers=hdr
            )
            r2 = await client.get("/api/v1/steward/latency", headers=hdr)
            b2 = r2.json()
            assert b2["decided_count"] == 1
            assert isinstance(b2["median_wait_s"], float) and b2["median_wait_s"] >= 0
            assert isinstance(b2["p90_wait_s"], float) and b2["p90_wait_s"] >= 0
            assert b2["pending_count"] == 0
            assert b2["oldest_pending_age_s"] is None

    run(_t())


def test_steward_routes_503_when_flywheel_unconfigured(make_app: AppBuilder) -> None:
    h = make_app(kpi_configured=False, steward_roles=_ROLES)  # empty signal URL

    async def _t() -> None:
        async with h.client() as client:
            hdr = _steward(h)
            assert (await client.get("/api/v1/steward/proposals", headers=hdr)).status_code == 503
            assert (await client.get("/api/v1/steward/digest", headers=hdr)).status_code == 503
            assert (await client.get("/api/v1/steward/latency", headers=hdr)).status_code == 503
            assert (
                await client.post(
                    "/api/v1/steward/proposals/x/approve", json={"note": ""}, headers=hdr
                )
            ).status_code == 503
            # The 403 role check still wins for an unauthorized caller (no 503 oracle).
            bad = await client.get(
                "/api/v1/steward/proposals", headers=h.bearer("user:e", groups=["eng"])
            )
            assert bad.status_code == 403

    run(_t())
