# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""No-bypass / identity — the security core (C6/C7 + identity-from-bearer)."""

from __future__ import annotations

import pathlib
import re
from typing import Any

from .conftest import AppBuilder, chunk, run

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "ne" / "api"


def _assert_no_acl_key(node: Any) -> None:
    if isinstance(node, dict):
        assert "acl" not in node, f"acl key leaked in {node!r}"
        for v in node.values():
            _assert_no_acl_key(v)
    elif isinstance(node, list):
        for v in node:
            _assert_no_acl_key(v)


def test_brain_search_trims_to_group_labels(make_app: AppBuilder) -> None:
    h = make_app(
        chunks=[chunk("c1", acl=["eng"], text="alpha"), chunk("c2", acl=["sales"], text="beta")]
    )

    async def _t() -> None:
        headers = h.bearer("user:ada", groups=["eng"], department="engineering")
        async with h.client() as client:
            r = await client.post(
                "/api/v1/brain/search", json={"query": "x", "top_k": 10}, headers=headers
            )
            assert r.status_code == 200
            ids = {sc["chunk"]["chunk_id"] for sc in r.json()["chunks"]}
            assert ids == {"c1"}  # ONLY the eng-labeled chunk; never sales
            _assert_no_acl_key(r.json())

    run(_t())


def test_groupless_principal_sees_only_self_label(make_app: AppBuilder) -> None:
    h = make_app(
        chunks=[
            chunk("mine", acl=["user:lonely"], text="mine"),
            chunk("foreign", acl=["eng"], text="foreign"),
        ]
    )

    async def _t() -> None:
        headers = h.bearer("user:lonely", groups=[], department="engineering")
        async with h.client() as client:
            r = await client.post(
                "/api/v1/brain/search", json={"query": "x", "top_k": 10}, headers=headers
            )
            assert r.status_code == 200
            ids = {sc["chunk"]["chunk_id"] for sc in r.json()["chunks"]}
            assert ids == {"mine"}  # the self-label filter matches nothing foreign

    run(_t())


def test_no_acl_key_in_retrieve(make_app: AppBuilder) -> None:
    h = make_app(chunks=[chunk("c1", acl=["eng"], text="alpha")])

    async def _t() -> None:
        headers = h.bearer("user:ada", groups=["eng"], department="engineering")
        async with h.client() as client:
            r = await client.post(
                "/api/v1/brain/retrieve", json={"chunk_ids": ["c1"]}, headers=headers
            )
            assert r.status_code == 200
            _assert_no_acl_key(r.json())

    run(_t())


def test_chat_session_body_cannot_set_department(make_app: AppBuilder) -> None:
    h = make_app()

    async def _t() -> None:
        headers = h.bearer("user:ada", groups=["eng"], department="engineering")
        async with h.client() as client:
            r = await client.post(
                "/api/v1/chat/sessions",
                json={"agent_name": "console-chat", "department": "other"},
                headers=headers,
            )
            assert r.status_code == 422  # extra=forbid — body cannot set department

    run(_t())


def test_cross_department_session_id_is_404(make_app: AppBuilder) -> None:
    h = make_app()

    async def _t() -> None:
        eng = h.bearer("user:eng", groups=["eng"], department="engineering")
        sales = h.bearer("user:sal", groups=["sales"], department="sales")
        async with h.client() as client:
            created = await client.post(
                "/api/v1/chat/sessions", json={"agent_name": "console-chat"}, headers=eng
            )
            sid = created.json()["session_id"]
            # A sales-department principal posting into an engineering session id -> 404.
            r = await client.post(
                f"/api/v1/chat/sessions/{sid}/messages", json={"prompt": "hi"}, headers=sales
            )
            assert r.status_code == 404

    run(_t())


def test_brain_search_rejects_user_filters(make_app: AppBuilder) -> None:
    h = make_app()

    async def _t() -> None:
        headers = h.bearer("user:ada", groups=["eng"], department="engineering")
        async with h.client() as client:
            r = await client.post(
                "/api/v1/brain/search",
                json={"query": "x", "top_k": 5, "filters": {"acl": ["sales"]}},
                headers=headers,
            )
            assert r.status_code == 422  # no user filters accepted

    run(_t())


def test_source_scan_guardian() -> None:
    py_files = sorted(_SRC.rglob("*.py"))
    assert py_files
    hybrid_hits: list[str] = []
    entity_graph_files: list[pathlib.Path] = []
    for path in py_files:
        text = path.read_text(encoding="utf-8")
        # C6: the executor is only ever built via the factory, never directly.
        assert "GovernedToolExecutor(" not in text, f"{path} constructs the executor directly"
        # HITL: ne-api never constructs an ApprovalOutcome — the REAL gate decides
        # (no parallel state machine). Grep-provable across every src file.
        assert "ApprovalOutcome(" not in text, f"{path} constructs an ApprovalOutcome"
        for line in text.splitlines():
            if "HybridRetriever(" in line:
                hybrid_hits.append(line)
        if "EntityGraph(" in text:
            entity_graph_files.append(path)

    # C7: exactly one HybridRetriever construction, and it passes acl_filters=.
    assert len(hybrid_hits) == 1, f"expected one HybridRetriever(, got {hybrid_hits}"
    assert "acl_filters=" in hybrid_hits[0]

    # NE-BRAIN-T2-WIRING F2 (C7 seam guardian): exactly ONE EntityGraph construction,
    # in platform.py, wiring the SAME expander instance as the chunk retriever
    # (grep-provable same-instance — the HybridRetriever(acl_filters=) pattern).
    assert entity_graph_files == [_SRC / "platform.py"], entity_graph_files
    platform_text = (_SRC / "platform.py").read_text(encoding="utf-8")
    assert platform_text.count("EntityGraph(") == 1
    assert "group_expander=expander" in platform_text

    # No route module imports the runtime middleware or the gateway directly.
    for path in (_SRC / "routers").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"import ne\.runtime\.middleware", text), path
        assert not re.search(r"import ne\.gateway", text), path


def test_steward_source_scan_guardian() -> None:
    """Grep-provable steward review invariants (spec §4-3/§4-4/§4-6)."""
    py_files = sorted(_SRC.rglob("*.py"))
    assert py_files

    provenance_files: list[pathlib.Path] = []
    for path in py_files:
        text = path.read_text(encoding="utf-8")
        # (a) ne-api NEVER writes ne_proposal_reviews — the REAL service owns every
        # transition (no parallel state machine, no direct review-row write).
        assert "ne_proposal_reviews.insert" not in text, f"{path} writes ne_proposal_reviews"
        assert "ne_proposal_reviews.update" not in text, f"{path} updates ne_proposal_reviews"
        assert "ne_proposal_reviews.delete" not in text, f"{path} deletes ne_proposal_reviews"
        if "SignalProvenance(" in text:
            provenance_files.append(path)

    # steward_metrics.py is read-only aggregation: no insert/update/delete call.
    metrics_text = (_SRC / "steward_metrics.py").read_text(encoding="utf-8")
    for verb in (".insert(", ".update(", ".delete("):
        assert verb not in metrics_text, f"steward_metrics.py contains a {verb} call"

    # (b) EXACTLY one SignalProvenance construction across src, in provenance.py, with
    # surface=SignalSurface.CONSOLE (server-side-only provenance). NE-137 HOISTED the
    # single construction out of routers/steward.py into ne/api/provenance.py so the
    # chat signal routes reuse the identical server-side-only builder — the
    # single-construction invariant is PRESERVED-AND-HOISTED (deliberate guardian
    # change, flagged in the PR body), not weakened.
    assert provenance_files == [_SRC / "provenance.py"], provenance_files
    provenance_text = (_SRC / "provenance.py").read_text(encoding="utf-8")
    assert provenance_text.count("SignalProvenance(") == 1
    assert "surface=SignalSurface.CONSOLE" in provenance_text
    # The steward router constructs no SignalProvenance itself (it delegates).
    steward_text = (_SRC / "routers" / "steward.py").read_text(encoding="utf-8")
    assert "SignalProvenance(" not in steward_text

    # (c) the steward router never reads identity from the wire (no headers / body actor).
    assert "request.headers" not in steward_text
    assert "body.actor" not in steward_text
