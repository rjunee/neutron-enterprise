# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""T-LR-1: the native GraphRetriever is fail-closed trim-before-return (C7).

* a heavily-trimmed principal gets ONLY visible expansion (no invisible neighbor
  ever appears in graph_paths);
* a T1 verification error PROPAGATES (deny-by-erroring, no silent degradation);
* the backend is the config flip and 'lightrag' without a wired sidecar FAILS
  CLOSED (no silent fallback to native).
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Sequence
from typing import Any

import pytest

from ne.brain_graph.config import BrainGraphSettings
from ne.brain_graph.lightrag import GraphExpansionSidecar, LightRagSidecar
from ne.brain_graph.lightrag.retrieval import GraphRetriever
from ne.brain_graph.read import EntityGraph
from ne.brain_graph.testing.fixtures import make_edge, make_node
from ne.brain_graph.testing.memory import InMemoryGraphStore
from ne.contracts.types import Citation, Principal, RetrievalResult


def _sidecar_conforms(s: LightRagSidecar) -> GraphExpansionSidecar:
    """Static (pyright-checked) proof that ``LightRagSidecar`` satisfies the seam."""
    return s


class _FakeT1:
    def __init__(self, citations: Sequence[Citation]) -> None:
        self._result = RetrievalResult(chunks=(), citations=tuple(citations))

    async def search(
        self, query: str, *, principal: Principal, top_k: int = 10, filters: Any = None
    ) -> RetrievalResult:
        return self._result

    async def retrieve(self, chunk_ids: Any, *, principal: Principal) -> RetrievalResult:
        return self._result


class _RaisingT1:
    async def search(
        self, query: str, *, principal: Principal, top_k: int = 10, filters: Any = None
    ) -> RetrievalResult:
        raise RuntimeError("T1 verification failed")

    async def retrieve(self, chunk_ids: Any, *, principal: Principal) -> RetrievalResult:
        raise RuntimeError("T1 verification failed")


class _RetrieveRaisesT1:
    """Search succeeds (anchors the query) but chunk verification (retrieve) raises."""

    def __init__(self, citations: Sequence[Citation]) -> None:
        self._result = RetrievalResult(chunks=(), citations=tuple(citations))

    async def search(
        self, query: str, *, principal: Principal, top_k: int = 10, filters: Any = None
    ) -> RetrievalResult:
        return self._result

    async def retrieve(self, chunk_ids: Any, *, principal: Principal) -> RetrievalResult:
        raise RuntimeError("chunk verification failed")


class _FakeSidecar:
    """A fake LightRAG sidecar returning fixed (untrusted) candidates."""

    def __init__(self, *, entity_keys: Sequence[str], chunk_ids: Sequence[str] = ()) -> None:
        self._entity_keys = list(entity_keys)
        self._chunk_ids = list(chunk_ids)
        self.calls = 0
        self.ensured = 0
        self.query_thread: int | None = None

    def ensure(self) -> None:
        self.ensured += 1

    def query(self, query_text: str, *, top_k: int = 10) -> dict[str, list[str]]:
        self.calls += 1
        self.query_thread = threading.get_ident()
        return {"entity_keys": list(self._entity_keys), "chunk_ids": list(self._chunk_ids)}


def _cite(source_ref: str) -> Citation:
    return Citation(
        chunk_id="c", document_id="d", source_system="salesforce", source_ref=source_ref
    )


def _mixed_store() -> InMemoryGraphStore:
    store = InMemoryGraphStore()
    anchor = make_node("anchor", "Ticket", ("tenant",), source_ref="Case:K1")
    visible = make_node("vis", "Account", ("tenant",), source_ref="Account:A1")
    hidden = make_node("hid", "Account", ("secret",), source_ref="Account:A9")
    asyncio.run(store.upsert_nodes([anchor, visible, hidden]))
    asyncio.run(
        store.upsert_edges(
            [
                make_edge("BELONGS_TO", "anchor", "vis", ("tenant",)),
                make_edge("BELONGS_TO", "anchor", "hid", ("tenant",)),
            ]
        )
    )
    return store


def test_native_returns_only_visible_expansion() -> None:
    store = _mixed_store()
    graph = EntityGraph(store, group_expander=None, audit_sink=None)
    retriever = GraphRetriever(graph, _FakeT1([_cite("Case:K1")]), BrainGraphSettings())
    result = asyncio.run(retriever.search("q", principal=Principal(id="u", groups=("tenant",))))
    # Only the visible neighbor may be cited; the 'secret' node never appears.
    keys = {
        ref.entity_key
        for path in result.graph_paths
        for hop in path.hops
        for ref in (hop.subject, hop.obj)
    }
    assert "hid" not in keys
    assert keys <= {"anchor", "vis"}


def test_t1_verification_error_propagates() -> None:
    store = _mixed_store()
    graph = EntityGraph(store, group_expander=None, audit_sink=None)
    retriever = GraphRetriever(graph, _RaisingT1(), BrainGraphSettings())
    with pytest.raises(RuntimeError, match="T1 verification failed"):
        asyncio.run(retriever.search("q", principal=Principal(id="u", groups=("tenant",))))


def test_lightrag_backend_without_sidecar_fails_closed() -> None:
    store = _mixed_store()
    graph = EntityGraph(store, group_expander=None, audit_sink=None)
    settings = BrainGraphSettings(graph_expansion_backend="lightrag")
    retriever = GraphRetriever(graph, _FakeT1([_cite("Case:K1")]), settings)
    # The config flip fails closed (deny-by-erroring) rather than silently using
    # the native path when the sidecar is not wired.
    with pytest.raises(RuntimeError, match="lightrag"):
        asyncio.run(retriever.search("q", principal=Principal(id="u", groups=("tenant",))))


def test_native_backend_is_the_default() -> None:
    assert BrainGraphSettings().graph_expansion_backend == "native"


def _lightrag_retriever(store: InMemoryGraphStore, sidecar: Any) -> GraphRetriever:
    graph = EntityGraph(store, group_expander=None, audit_sink=None)
    settings = BrainGraphSettings(graph_expansion_backend="lightrag")
    return GraphRetriever(graph, _FakeT1([_cite("Case:K1")]), settings, sidecar=sidecar)


def test_lightrag_drops_invisible_candidates() -> None:
    # T-LR-1: the sidecar returns an INVISIBLE entity ('hid', acl 'secret') among
    # the candidates. It must be re-validated against the trimmed EntityGraph and
    # DROPPED — never appearing in graph_paths.
    store = _mixed_store()
    sidecar = _FakeSidecar(entity_keys=["hid", "vis", "anchor"])
    retriever = _lightrag_retriever(store, sidecar)
    result = asyncio.run(retriever.search("q", principal=Principal(id="u", groups=("tenant",))))
    keys = {
        ref.entity_key
        for path in result.graph_paths
        for hop in path.hops
        for ref in (hop.subject, hop.obj)
    }
    assert sidecar.calls == 1
    assert "hid" not in keys  # invisible candidate dropped trim-before-return
    assert keys <= {"anchor", "vis"}
    assert result.graph_paths  # verified anchors DID yield a trimmed path


def test_lightrag_chunk_verification_error_propagates() -> None:
    # T-LR-1: a chunk-verification error (t1.retrieve raising) PROPAGATES — no
    # silent degradation to the native path.
    store = _mixed_store()
    sidecar = _FakeSidecar(entity_keys=["vis"], chunk_ids=["cX"])
    graph = EntityGraph(store, group_expander=None, audit_sink=None)
    settings = BrainGraphSettings(graph_expansion_backend="lightrag")
    retriever = GraphRetriever(
        graph, _RetrieveRaisesT1([_cite("Case:K1")]), settings, sidecar=sidecar
    )
    with pytest.raises(RuntimeError, match="chunk verification failed"):
        asyncio.run(retriever.search("q", principal=Principal(id="u", groups=("tenant",))))


def test_lightrag_all_invisible_candidates_yields_no_paths() -> None:
    # A principal who can see NONE of the sidecar's candidates gets empty paths.
    store = _mixed_store()
    sidecar = _FakeSidecar(entity_keys=["hid"])
    graph = EntityGraph(store, group_expander=None, audit_sink=None)
    settings = BrainGraphSettings(graph_expansion_backend="lightrag")
    # A T1 with NO citations so there is no source-ref anchor either.
    retriever = GraphRetriever(graph, _FakeT1([]), settings, sidecar=sidecar)
    result = asyncio.run(retriever.search("q", principal=Principal(id="x", groups=("other",))))
    assert result.graph_paths == ()


def test_lightrag_query_dispatched_off_event_loop() -> None:
    # P3-3: the blocking sidecar query must run OFF the event-loop thread
    # (asyncio.to_thread), so a venv-build/subprocess never freezes the loop.
    store = _mixed_store()
    sidecar = _FakeSidecar(entity_keys=["vis", "anchor"])
    retriever = _lightrag_retriever(store, sidecar)

    loop_thread: dict[str, int] = {}

    async def _scenario() -> None:
        loop_thread["id"] = threading.get_ident()
        await retriever.search("q", principal=Principal(id="u", groups=("tenant",)))

    asyncio.run(_scenario())
    assert sidecar.query_thread is not None
    assert sidecar.query_thread != loop_thread["id"]


def test_startup_ensures_sidecar_off_loop() -> None:
    # P3-3: startup() front-loads the (blocking) sidecar ensure exactly once for
    # the lightrag backend, and is a safe no-op for the native/no-sidecar path.
    store = _mixed_store()
    sidecar = _FakeSidecar(entity_keys=["vis"])
    retriever = _lightrag_retriever(store, sidecar)
    asyncio.run(retriever.startup())
    assert sidecar.ensured == 1

    graph = EntityGraph(store, group_expander=None, audit_sink=None)
    native = GraphRetriever(graph, _FakeT1([_cite("Case:K1")]), BrainGraphSettings())
    asyncio.run(native.startup())  # no sidecar → completes without error
