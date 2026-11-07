# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-T2X-5-RECORDER-WIRE — the acceptance battery for the recorder BINDING.

NE-T2X-5 (PR #342) shipped a correct, dual-backend, parity-proven
``context_provisional`` tier escalation — but NOTHING IN PRODUCTION ever called
``current_provisional_recorder().record(...)``, so the control was INERT: a
turn that retrieved unconfirmed LLM-extracted graph facts and then called
``write.internal.*`` left the turn accumulator empty, ``context_provisional``
false, and both PDPs allowed the write with NO human approval (exactly the
A-P1-5 risk design §6.3 exists to close).

This is the test suite that proves the binding this unit ships actually FIRES,
end to end. PART A drives ``build_chat_bindings``' ``_search`` handler directly
against a fake ``KnowledgeRead`` (the ``test_retrieval_top_k.py`` shape) —
fast, no app — and proves the binding-level trio: records on provisional
input, records NOTHING on an all-vouched control, and is a safe no-op with no
active scope. PART B drives a REAL SSE chat turn through the REAL app, the
REAL in-process Cedar PDP (``CedarPdpFactory``, the ne-runtime Cedar-guardian
precedent — cedarpy imported directly, NO ``importorskip``), and the REAL
``DurableApprovalGate``: an extracted-context retrieval followed by a
``write.internal.*`` call REQUIRES a named human approval; the byte-identical
sequence over backbone-only facts does not — the value-discriminating pair
this unit's acceptance bar demands, not a flag-presence check.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any

import cedarpy  # noqa: F401 -- guardian import: must be real, never importorskip'd.

from ne.api.tools import TURN_PRINCIPAL, build_chat_bindings
from ne.brain_graph.config import BrainGraphSettings
from ne.brain_graph.testing.fixtures import make_edge, make_node
from ne.brain_graph.testing.memory import InMemoryGraphStore
from ne.contracts.runtime import ToolSpec
from ne.contracts.types import (
    ApprovalTier,
    GraphEntityRef,
    GraphHop,
    GraphPath,
    Principal,
    RetrievalResult,
)
from ne.govern.cedar.evaluator import CedarEvaluator
from ne.govern.cedar.pdp import CedarGovernedPdp
from ne.govern.config import ArgSpec, ToolArgKind, ToolArgPolicy
from ne.runtime.config import ApprovalSettings
from ne.runtime.provisional import current_provisional_context, provisional_context_scope
from ne.runtime.tools import ToolBinding

from .conftest import AppBuilder, chunk, run, text_completion, tool_call_completion

_PRINCIPAL = Principal(id="u1", groups=("eng",))
_WRITE_ACTION = "write.internal.notes.append"


# --- PART A: binding-level (fast, no app) -----------------------------------


class _RecordingRetriever:
    """Fake KnowledgeRead: returns a canned result (the test_retrieval_top_k shape)."""

    def __init__(self, result: RetrievalResult) -> None:
        self._result = result

    async def search(
        self,
        query: str,
        *,
        principal: Principal,
        top_k: int = 10,
        filters: Mapping[str, Any] | None = None,
    ) -> RetrievalResult:
        return self._result

    async def retrieve(self, chunk_ids: Sequence[str], *, principal: Principal) -> RetrievalResult:
        raise NotImplementedError


def _extracted_hop() -> GraphHop:
    """One hop: a backbone subject, an EXTRACTED object, and an EXTRACTED hop itself.

    Pinned dedup count == 2: the extracted entity 'b' (1) + the extracted hop
    itself (1). Subject 'a' is assurance='source' and never counts.
    """
    subject = GraphEntityRef(
        entity_class="Account", entity_key="a", display="A", assurance="source"
    )
    obj = GraphEntityRef(entity_class="Account", entity_key="b", display="B", assurance="extracted")
    return GraphHop(
        subject=subject,
        edge_kind="ABOUT",
        obj=obj,
        source_system="ne:llm",
        source_ref="llm:aaaaaaaaaaaaaaaa:e1",
        assurance="extracted",
    )


def _backbone_hop() -> GraphHop:
    """The identical topology, every assurance label 'source' (the control)."""
    return GraphHop(
        subject=GraphEntityRef(entity_class="Account", entity_key="a", display="A"),
        edge_kind="ABOUT",
        obj=GraphEntityRef(entity_class="Account", entity_key="b", display="B"),
        source_system="salesforce",
        source_ref="Account:b",
    )


async def _call_search_in_scope(retriever: Any, *, extraction_mode: str = "read_flagged") -> str:
    (binding,) = build_chat_bindings(retriever, extraction_mode=extraction_mode)
    token = TURN_PRINCIPAL.set(_PRINCIPAL)
    try:
        return await binding.handler({"query": "q"})
    finally:
        TURN_PRINCIPAL.reset(token)


def test_search_records_provisional_contribution_into_active_scope() -> None:
    paths = (GraphPath(hops=(_extracted_hop(),)),)
    result = RetrievalResult(chunks=(), citations=(), graph_paths=paths)
    retriever = _RecordingRetriever(result)

    async def _t() -> Any:
        with provisional_context_scope():
            await _call_search_in_scope(retriever, extraction_mode="read_flagged")
            return current_provisional_context()

    snapshot = asyncio.run(_t())
    assert snapshot is not None
    assert snapshot.provisional_source_count == 2
    assert snapshot.provisional_source_refs == ("llm:aaaaaaaaaaaaaaaa:e1",)
    assert snapshot.extraction_mode == "read_flagged"
    assert snapshot.provisional is True


def test_search_with_only_vouched_assurance_records_nothing() -> None:
    """BIDIRECTIONAL CONTROL: an all-backbone retrieval leaves the accumulator
    EMPTY — count 0, mode None, provisional False. Not merely 'flag false': the
    guard cannot be satisfied by a handler that records unconditionally."""
    paths = (GraphPath(hops=(_backbone_hop(),)),)
    result = RetrievalResult(chunks=(), citations=(), graph_paths=paths)
    retriever = _RecordingRetriever(result)

    async def _t() -> Any:
        with provisional_context_scope():
            await _call_search_in_scope(retriever, extraction_mode="read_flagged")
            return current_provisional_context()

    snapshot = asyncio.run(_t())
    assert snapshot is not None
    assert snapshot.provisional_source_count == 0
    assert snapshot.provisional_source_refs == ()
    assert snapshot.extraction_mode is None
    assert snapshot.provisional is False


def test_search_outside_any_scope_is_a_safe_noop() -> None:
    paths = (GraphPath(hops=(_extracted_hop(),)),)
    result = RetrievalResult(chunks=(), citations=(), graph_paths=paths)
    retriever = _RecordingRetriever(result)

    # No active scope at all — the handler must not raise.
    asyncio.run(_call_search_in_scope(retriever, extraction_mode="read_flagged"))

    # A subsequently opened FRESH scope shows an empty accumulator — nothing
    # leaked across the no-scope call.
    async def _t() -> Any:
        with provisional_context_scope():
            return current_provisional_context()

    snapshot = asyncio.run(_t())
    assert snapshot is not None
    assert snapshot.provisional_source_count == 0
    assert snapshot.provisional is False


# --- PART B: end-to-end discriminating pair through the REAL app -----------


class CedarPdpFactory:
    """A ``policy_decision_point`` plugin factory over the REAL Cedar engine
    (the ne-runtime Cedar-guardian precedent) — no scripted transport decides
    the escalation; the real bundle in ``ne.govern.cedar.decision`` does.

    **Naming caveat (Argus P3):** ``conftest.py``'s injection seam registers
    whatever factory it is handed under the plugin NAME ``"opa"``, so a reader
    grepping for which backend decided an escalation in this test will see the
    string ``"opa"``. The engine really is **Cedar** — this class returns a
    :class:`CedarGovernedPdp`. The name is a pre-existing property of the
    harness seam, not a claim about the backend; noted here so the mismatch is
    a documented quirk rather than a future misread.
    """

    @staticmethod
    def from_settings(_settings: Any, *, sink: Any) -> CedarGovernedPdp:
        return CedarGovernedPdp(CedarEvaluator(), sink)


def _write_binding(ran: list[str]) -> ToolBinding:
    async def _handler(args: Mapping[str, object]) -> str:
        ran.append(str(args.get("note", "")))
        return "ok"

    return ToolBinding(
        spec=ToolSpec(
            name=_WRITE_ACTION,
            description="append a note",
            tier=ApprovalTier.TIER_1,
            input_schema={
                "type": "object",
                "properties": {"note": {"type": "string"}},
                "required": ["note"],
            },
        ),
        handler=_handler,
    )


def _seed_graph(store: InMemoryGraphStore, *, extracted: bool) -> None:
    """Anchor node 'a' (source_ref='ref-c1', joins chunk c1's T1 citation), an
    edge ABOUT a->b, and node 'b' — all acl=('eng',). In the extracted arm,
    node 'b' and the edge are stamped ``source_system='ne:llm'`` +
    ``assurance='extracted'`` (the read-path's ``_assurance_of`` fail-closed
    truth-table ONLY honors a stored ``assurance`` prop for ``ne:llm``-sourced
    elements — a non-'ne:llm' element always reads 'source' regardless of any
    smuggled assurance prop). In the backbone arm every element keeps the
    fixture's default ``source_system='fixture'`` (reads 'source')."""

    async def setup() -> None:
        node_b_props: dict[str, Any] = {}
        edge_props: dict[str, Any] = {}
        if extracted:
            node_b_props = {"source_system": "ne:llm", "assurance": "extracted"}
            edge_props = {"source_system": "ne:llm", "assurance": "extracted"}
        await store.upsert_nodes(
            [
                make_node(
                    "a",
                    "Account",
                    ("eng",),
                    source_ref="ref-c1",
                    source_status="no_source",
                ),
                make_node("b", "Account", ("eng",), source_status="no_source", **node_b_props),
            ]
        )
        edge = make_edge("ABOUT", "a", "b", ("eng",))
        edge["source_status"] = "no_source"
        if extracted:
            edge = {**edge, "source_ref": "llm:aaaaaaaaaaaaaaaa:e1", **edge_props}
        await store.upsert_edges([edge])

    run(setup())


def _build_e2e_app(make_app: AppBuilder, *, extracted: bool, ran: list[str]) -> Any:
    graph_store = InMemoryGraphStore()
    _seed_graph(graph_store, extracted=extracted)
    h = make_app(
        brain_graph_settings=BrainGraphSettings(enabled=True, extraction_mode="read_flagged"),
        graph_store=graph_store,
        chunks=[chunk("c1", acl=["eng"], text="alpha")],
        pdp_factory=CedarPdpFactory,
        approver_roles=("approvers",),
        approvals=ApprovalSettings(poll_interval_s=0.05),
        extra_tool_bindings=(_write_binding(ran),),
        extra_tool_policies={
            _WRITE_ACTION: ToolArgPolicy(
                args={"note": ArgSpec(kind=ToolArgKind.text, required=True)}
            )
        },
        completions=[
            tool_call_completion("read.knowledge.search", {"query": "alpha"}),
            tool_call_completion(_WRITE_ACTION, {"note": "n"}),
            text_completion("done"),
        ],
    )
    return h


def test_extracted_context_write_requires_approval_end_to_end(make_app: AppBuilder) -> None:
    ran: list[str] = []
    h = _build_e2e_app(make_app, extracted=True, ran=ran)

    async def _t() -> None:
        loop = asyncio.get_running_loop()
        ada = h.bearer("user:ada", groups=["eng"], department="engineering")
        appr = h.bearer("user:approver", groups=["approvers"])
        async with h.client() as writer, h.client() as approver:
            created = await writer.post(
                "/api/v1/chat/sessions", json={"agent_name": "console-chat"}, headers=ada
            )
            assert created.status_code == 200
            sid = created.json()["session_id"]

            task = asyncio.create_task(
                writer.post(
                    f"/api/v1/chat/sessions/{sid}/messages",
                    json={"prompt": "search then write"},
                    headers=ada,
                )
            )

            deadline = loop.time() + 10.0
            pending = None
            while loop.time() < deadline:
                r = await approver.get("/api/v1/approvals/pending", headers=appr)
                assert r.status_code == 200
                match = [x for x in r.json()["approvals"] if x["action"] == _WRITE_ACTION]
                if match:
                    pending = match[0]
                    break
                await asyncio.sleep(0.05)
            assert pending is not None, "the tier-2 escalation never produced a pending approval"
            # The REAL Cedar bundle escalated tier 1 (write.internal.*) -> tier 2
            # (design §6.3's ESC rule): a bare tier-1 declaration alone would
            # never require approval — only the escalation makes this block.
            assert pending["tier"] == 2
            # The write has NOT executed while the approval is pending.
            assert ran == []

            resp = await approver.post(
                f"/api/v1/approvals/{pending['request_id']}/approve", json={}, headers=appr
            )
            assert resp.status_code == 200
            assert resp.json()["status"] == "approved"

            sse = await task
            assert sse.status_code == 200

    run(_t())
    assert ran == ["n"]


def test_backbone_context_write_executes_without_approval_end_to_end(make_app: AppBuilder) -> None:
    ran: list[str] = []
    h = _build_e2e_app(make_app, extracted=False, ran=ran)

    async def _t() -> None:
        ada = h.bearer("user:ada", groups=["eng"], department="engineering")
        appr = h.bearer("user:approver", groups=["approvers"])
        async with h.client() as writer, h.client() as approver:
            created = await writer.post(
                "/api/v1/chat/sessions", json={"agent_name": "console-chat"}, headers=ada
            )
            assert created.status_code == 200
            sid = created.json()["session_id"]

            r = await writer.post(
                f"/api/v1/chat/sessions/{sid}/messages",
                json={"prompt": "search then write"},
                headers=ada,
            )
            assert r.status_code == 200

            # The write executed inline — no approval row for it, ever.
            pending = await approver.get("/api/v1/approvals/pending", headers=appr)
            assert pending.status_code == 200
            match = [x for x in pending.json()["approvals"] if x["action"] == _WRITE_ACTION]
            assert match == [], "backbone-only context must never require approval"

    run(_t())
    assert ran == ["n"]
