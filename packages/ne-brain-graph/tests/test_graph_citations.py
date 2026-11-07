# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""T-CIT-1/T-CIT-2: graph-aware citations are trimmed by construction (ADR-0005).

T-CIT-1: a multi-hop fixture yields ``graph_paths`` whose hop carries the exact
edge_kind + source_system/source_ref, and the populated result wire-round-trips.
T-CIT-2: a path whose ONLY route crosses an invisible middle node is ABSENT even
when both endpoints are visible (the §1.3.2 through-node trimming property).
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

from ne.brain_graph.config import BrainGraphSettings
from ne.brain_graph.lightrag.retrieval import GraphRetriever, paths_to_graph_paths
from ne.brain_graph.model import EntityEdge, EntityNode, GraphPathRecord
from ne.brain_graph.read import EntityGraph
from ne.brain_graph.testing.fixtures import make_edge, make_node
from ne.brain_graph.testing.memory import InMemoryGraphStore
from ne.contracts.types import Citation, Principal, RetrievalResult

_TENANT = Principal(id="u", groups=("tenant",))


class _FakeT1:
    def __init__(self, citations: Sequence[Citation]) -> None:
        self._result = RetrievalResult(chunks=(), citations=tuple(citations))

    async def search(
        self, query: str, *, principal: Principal, top_k: int = 10, filters: Any = None
    ) -> RetrievalResult:
        return self._result

    async def retrieve(self, chunk_ids: Any, *, principal: Principal) -> RetrievalResult:
        return self._result


def _cite(source_ref: str) -> Citation:
    return Citation(
        chunk_id="c", document_id="d", source_system="salesforce", source_ref=source_ref
    )


def test_cit1_multi_hop_yields_exact_hop_and_round_trips() -> None:
    store = InMemoryGraphStore()
    tkt = make_node("tkt", "Ticket", ("tenant",), source_ref="Case:K1", display_name="Login")
    acct = make_node("acct", "Account", ("tenant",), source_ref="Account:A1", display_name="Acme")
    edge = make_edge("BELONGS_TO", "tkt", "acct", ("tenant",))
    asyncio.run(store.upsert_nodes([tkt, acct]))
    asyncio.run(store.upsert_edges([edge]))

    graph = EntityGraph(store, group_expander=None, audit_sink=None)
    retriever = GraphRetriever(graph, _FakeT1([_cite("Case:K1")]), BrainGraphSettings())
    result = asyncio.run(retriever.search("q", principal=_TENANT))

    assert len(result.graph_paths) == 1
    [path] = result.graph_paths
    assert len(path.hops) == 1
    hop = path.hops[0]
    assert hop.edge_kind == "BELONGS_TO"
    assert hop.source_system == "fixture"
    assert hop.source_ref == "tkt:acct"
    assert {hop.subject.entity_key, hop.obj.entity_key} == {"tkt", "acct"}

    # ADR-0015 (NE-T2X-5-ENDPOINT-EVIDENCE-REF): each endpoint carries ITS OWN
    # evidencing ref, distinct from the hop's asserting-edge ref — _ref()
    # threads node.source_ref rather than dropping it.
    ref_by_entity_key = {hop.subject.entity_key: hop.subject.source_ref}
    ref_by_entity_key[hop.obj.entity_key] = hop.obj.source_ref
    assert ref_by_entity_key["tkt"] == "Case:K1"
    assert ref_by_entity_key["acct"] == "Account:A1"
    assert ref_by_entity_key["tkt"] != hop.source_ref
    assert ref_by_entity_key["acct"] != hop.source_ref

    # Wire round-trip of the populated result.
    restored = RetrievalResult.model_validate(result.model_dump())
    assert restored == result


def test_cit2_path_through_invisible_middle_is_absent() -> None:
    store = InMemoryGraphStore()
    # Both endpoints visible; the ONLY route crosses an invisible middle node M.
    tkt = make_node("tkt", "Ticket", ("tenant",), source_ref="Case:K1")
    acct = make_node("acct", "Account", ("tenant",), source_ref="Account:A1")
    middle = make_node("mid", "Person", ("secret",), source_ref="Contact:C9")
    e1 = make_edge("REQUESTER", "tkt", "mid", ("tenant",))
    e2 = make_edge("WORKS_AT", "mid", "acct", ("tenant",))
    asyncio.run(store.upsert_nodes([tkt, acct, middle]))
    asyncio.run(store.upsert_edges([e1, e2]))

    graph = EntityGraph(store, group_expander=None, audit_sink=None)
    # Both endpoints are anchors (two citations), so the pair IS attempted.
    retriever = GraphRetriever(
        graph,
        _FakeT1([_cite("Case:K1"), _cite("Account:A1")]),
        BrainGraphSettings(retrieval_expansion_hops=3, retrieval_max_paths=5),
    )
    result = asyncio.run(retriever.search("q", principal=_TENANT))
    # The route tkt -> mid -> acct crosses the invisible M, so no path is cited.
    assert result.graph_paths == ()


def test_paths_to_graph_paths_threads_each_endpoints_own_ref() -> None:
    """ADR-0015 (NE-T2X-5-ENDPOINT-EVIDENCE-REF): the exact defect shape at the
    layer that used to drop the datum — an extracted subject's writer-stamped
    llm:<sha16>:<ref> survives to GraphEntityRef.source_ref, a source-backed
    object's own ref survives too, and the hop keeps the EDGE's distinct ref —
    never each other's."""
    subject = EntityNode(
        key="p1",
        entity_class="Person",
        display_name="Jane",
        source_system="ne:llm",
        source_ref="llm:fedcba9876543210:doc-b#c2",
        assurance="extracted",
    )
    obj = EntityNode(
        key="a1",
        entity_class="Account",
        display_name="Acme",
        source_system="salesforce",
        source_ref="Account:A1",
        assurance="source",
    )
    edge = EntityEdge(
        key="e1",
        edge_kind="WORKS_AT",
        from_key="p1",
        to_key="a1",
        source_system="salesforce",
        source_ref="Contact:C1",
        assurance="source",
    )
    record = GraphPathRecord(nodes=(subject, obj), edges=(edge,))

    [path] = paths_to_graph_paths([record])
    [hop] = path.hops

    assert hop.subject.source_ref == "llm:fedcba9876543210:doc-b#c2"
    assert hop.obj.source_ref == "Account:A1"
    assert hop.source_ref == "Contact:C1"
    # Distinct, pairwise — never each other's ref.
    refs = {hop.subject.source_ref, hop.obj.source_ref, hop.source_ref}
    assert len(refs) == 3
