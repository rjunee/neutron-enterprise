# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Governance-wiring test (§5.3-4, 104a scope) — the single most valuable test."""

from __future__ import annotations

import sqlalchemy as sa

from ne.brain.pipeline import RetrievalPipeline
from ne.brain.retriever import HybridRetriever
from ne.brain_graph.config import BrainGraphSettings
from ne.brain_graph.lightrag.retrieval import GraphRetriever
from ne.brain_graph.testing.memory import InMemoryGraphStore
from ne.contracts import get_config_hash

from .conftest import (
    AppBuilder,
    chunk,
    get_state,
    parse_sse,
    run,
    text_completion,
    tool_call_completion,
)


def test_config_hash_frozen_before_any_request(make_app: AppBuilder) -> None:
    h = make_app()
    state = get_state(h.app)
    # Frozen eagerly at factory time — succeeds BEFORE any request is served.
    assert get_config_hash() == state.config_hash


def test_one_sink_and_one_gate_identity_shared(make_app: AppBuilder) -> None:
    h = make_app()
    state = get_state(h.app)
    # ONE audit sink, identity-shared across the executor and the gate.
    assert state.executor._sink is state.sink
    assert state.gate._sink is state.sink
    # The executor's approval gate IS the wired gate (one gate).
    assert state.executor._approval_gate is state.gate


def test_end_to_end_governed_turn(make_app: AppBuilder) -> None:
    h = make_app(
        completions=[
            tool_call_completion("read.knowledge.search", {"query": "q"}),
            text_completion("answer"),
        ],
        chunks=[chunk("c1", acl=["eng"], text="alpha")],
    )
    state = get_state(h.app)

    async def _t() -> None:
        headers = h.bearer("user:ada", groups=["eng"], department="engineering")
        async with h.client() as client:
            created = await client.post(
                "/api/v1/chat/sessions", json={"agent_name": "console-chat"}, headers=headers
            )
            assert created.status_code == 200
            sid = created.json()["session_id"]
            r = await client.post(
                f"/api/v1/chat/sessions/{sid}/messages", json={"prompt": "hi"}, headers=headers
            )
            assert r.status_code == 200
            parsed = parse_sse(r.text)
            done = parsed.of_type("done")[0]
            assert done.data is not None
            assert done.data["output"] == "answer"
            # every frame's event field equals its payload type
            for frame in parsed.frames:
                assert frame.data is not None
                assert frame.event == frame.data["type"]

    run(_t())

    # The knowledge tool ran through the retriever with a REAL acl filter.
    assert any(f is not None and "acl" in f for f in h.chunk_store.seen_filters)

    # The audit chain carries governed records stamped with the frozen config_hash.
    engine = sa.create_engine(h.audit_url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(sa.text("SELECT action, config_hash FROM ne_audit_log")).fetchall()
    finally:
        engine.dispose()
    actions = [row[0] for row in rows]
    assert any(a == "tool.execute" for a in actions), actions
    assert any(a == "pdp.decide" for a in actions), actions
    assert all(row[1] == state.config_hash for row in rows)


def test_retriever_audit_sink_is_the_platform_sink_by_identity(make_app: AppBuilder) -> None:
    """V6 identity pin (design §3-V6; the ``test_identity_sink_wire_pin.py``
    precedent, PRs #339/#343): a PRESENCE check is satisfiable by ANY sink the
    factory invents — proven by the NE-VG-2 substitution mutation applied in
    this worktree (a fresh, same-engine sink bypassing the (4c)
    ``ToolTransitObservingSink`` wrap at ``platform.py:1003`` left every
    store-level emission test GREEN, including
    ``test_retrieve_route_emits_read_audit_event``). ``is`` identity closes
    that residual.

    The test harness's ``AppSeams`` always injects a ``PassthroughReranker``
    (never ``None`` — see ``conftest.py``'s ``make_app``), so the default app's
    ``state.retriever`` is a :class:`RetrievalPipeline` wrapping the ONE
    :class:`HybridRetriever` built at ``platform.py:1003``.
    """
    h = make_app()
    state = get_state(h.app)
    assert isinstance(state.retriever, RetrievalPipeline)
    hybrid = state.retriever._retriever
    assert isinstance(hybrid, HybridRetriever)
    assert hybrid._audit_sink is state.sink


def test_graph_audit_sink_is_the_platform_sink_by_identity(make_app: AppBuilder) -> None:
    """Same V6 identity pin for the T2 ``EntityGraph`` binding at
    ``platform.py:1078``, plus the T1 ``HybridRetriever`` it wraps (both ride
    the SAME (4c)-wrapped sink; the graph tier composes ABOVE the reranked T1
    pipeline, per ``platform.py``'s own composition-order docstring)."""
    h = make_app(
        brain_graph_settings=BrainGraphSettings(enabled=True),
        graph_store=InMemoryGraphStore(),
    )
    state = get_state(h.app)
    assert state.entity_graph is not None
    assert state.entity_graph._audit_sink is state.sink
    assert isinstance(state.retriever, GraphRetriever)
    hybrid = state.retriever._t1._retriever  # type: ignore[attr-defined]
    assert isinstance(hybrid, HybridRetriever)
    assert hybrid._audit_sink is state.sink


def test_department_claim_union_at_app_seam(make_app: AppBuilder) -> None:
    # An identity seam WITHOUT 'department' in principal_claims still yields a
    # working chat session — create_app's _ensure_department_claim union (A1).
    h = make_app(identity_department_in_claims=False)

    async def _t() -> None:
        headers = h.bearer("user:ada", groups=["eng"], department="engineering")
        async with h.client() as client:
            created = await client.post(
                "/api/v1/chat/sessions", json={"agent_name": "console-chat"}, headers=headers
            )
            assert created.status_code == 200

    run(_t())
