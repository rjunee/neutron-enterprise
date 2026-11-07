# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
#
# Portions derived from jupyter-naas/abi
# libs/naas-abi-core/naas_abi_core/services/triple_store/oxigraph_server.py
# @ a3e15eff03902091f23acfd9edbc95dc9695183f — MIT License, Copyright (C) 2025
# Naas.ai. The pyoxigraph.Store wrapping (open, bulk_load, query, RdfFormat /
# QueryResultsFormat / QueryTriples handling) is adapted from that file; ABI's
# HTTP/FastAPI server shell is dropped — this is an in-process, no-docker
# dev-tier backend.
"""In-process oxigraph :class:`TripleStoreBackend` (NE-085a).

``OxigraphBackend`` implements the package-local
:class:`~ne.brain_ontology.store.protocols.TripleStoreBackend` over an embedded
``pyoxigraph.Store`` (``path=None`` → in-memory). It is the no-docker dev/test
substrate that makes the T3 conformance suite runnable without a Fuseki daemon —
so, unlike T1, T3 gets a REAL backend in every unit-test run.

The frozen-``TripleStore`` surface is now the gateway's job
(:class:`~ne.brain_ontology.gateway.ValidatingTripleStore`); this backend is
package-private and is only ever reached through the gateway (backend-privacy
invariant, brain-tier3-spec §5.1). All reads are dataset-scoped via
:class:`DatasetSpec`; all methods parse local bytes only (C2 — no URL/location
parsing).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import ClassVar

from pyoxigraph import (
    BlankNode,
    DefaultGraph,
    NamedNode,
    QueryBoolean,
    QuerySolutions,
    QueryTriples,
    RdfFormat,
    Store,
    serialize,
)

from .protocols import DatasetSpec

_QueryResult = QuerySolutions | QueryBoolean | QueryTriples

# The IRI-vs-literal rule (an object is an IRI iff it is an absolute http/https/urn
# IRI) now lives in the gateway, which builds the delta rdflib graph before writes
# reach this backend (it receives already-serialized N-Triples / Turtle bytes).


class OxigraphBackend:
    """Embedded pyoxigraph backend satisfying the package-local ``TripleStoreBackend``."""

    #: An embedded ``Store`` object IS the snapshot: a governed local-T3 release
    #: writer builds its own distinct ``Store``, and nothing outside this process
    #: can reach the one a selected release pins. See
    #: :class:`~ne.brain_ontology.store.protocols.ImmutableReleaseReadBackend`.
    supports_immutable_release_snapshot: ClassVar[bool] = True

    def __init__(self, path: str | None = None) -> None:
        # path=None → in-memory store (the dev/test default).
        self._store = Store(path=path)

    def _query(self, query: str, dataset: DatasetSpec) -> _QueryResult:
        """Run ``query`` scoped by ``dataset``.

        Empty ``default_graphs`` → union of all graphs
        (``use_default_graph_as_union``). A non-empty set restricts the query's
        default graph to those named graphs and exposes ``named_graphs`` for
        ``GRAPH ?g`` matching.
        """
        if not dataset.default_graphs and not dataset.named_graphs:
            return self._store.query(query, use_default_graph_as_union=True)
        default_graph: list[NamedNode | BlankNode | DefaultGraph] | None = [
            NamedNode(g) for g in dataset.default_graphs
        ] or None
        named_graphs: list[NamedNode | BlankNode] | None = [
            NamedNode(g) for g in dataset.named_graphs
        ] or None
        return self._store.query(
            query,
            use_default_graph_as_union=not dataset.default_graphs,
            default_graph=default_graph,
            named_graphs=named_graphs,
        )

    async def select(self, query: str, *, dataset: DatasetSpec) -> Sequence[Mapping[str, str]]:
        result = self._query(query, dataset)
        assert isinstance(result, QuerySolutions)
        variables = [str(v.value) for v in result.variables]
        rows: list[dict[str, str]] = []
        for solution in result:
            row: dict[str, str] = {}
            for index, name in enumerate(variables):
                term = solution[index]
                if term is not None:
                    row[name] = term.value
            rows.append(row)
        return rows

    async def ask(self, query: str, *, dataset: DatasetSpec) -> bool:
        result = self._query(query, dataset)
        assert isinstance(result, QueryBoolean)
        return bool(result)

    async def construct_nt(self, query: str, *, dataset: DatasetSpec) -> bytes:
        result = self._query(query, dataset)
        assert isinstance(result, QueryTriples)
        out = serialize(result, format=RdfFormat.N_TRIPLES)
        assert out is not None  # output=None → bytes are returned
        return out

    async def insert_nt(self, data: bytes, *, graph_iri: str) -> None:
        if not data.strip():
            return
        self._store.bulk_load(data, format=RdfFormat.N_TRIPLES, to_graph=NamedNode(graph_iri))

    async def load_turtle(self, data: bytes, *, graph_iri: str) -> None:
        self._store.bulk_load(data, format=RdfFormat.TURTLE, to_graph=NamedNode(graph_iri))

    async def replace_graph_turtle(self, data: bytes, *, graph_iri: str) -> None:
        # PUT semantics: drop the graph's current contents, then load the new bytes.
        self._store.clear_graph(NamedNode(graph_iri))
        self._store.bulk_load(data, format=RdfFormat.TURTLE, to_graph=NamedNode(graph_iri))

    async def delete_graph(self, graph_iri: str) -> None:
        # remove_graph drops the graph entirely (so it disappears from graphs());
        # tolerant of an absent graph.
        self._store.remove_graph(NamedNode(graph_iri))

    async def graphs(self) -> Sequence[str]:
        return [str(g.value) for g in self._store.named_graphs()]

    async def health(self) -> bool:
        # Embedded engine: reachable iff the object exists.
        return True


class ReadOnlyOxigraphBackend:
    """A read-only view over ONE ``OxigraphBackend``, with no mutation surface at all.

    This is not a refusing wrapper that raises on write — the write methods are
    STRUCTURALLY ABSENT, so the wrapper is not a ``TripleStoreBackend`` and no
    product writer can be handed one by accident. It satisfies only
    :class:`~ne.brain_ontology.store.protocols.ImmutableReleaseReadBackend`.
    """

    supports_immutable_release_snapshot: ClassVar[bool] = True

    def __init__(self, inner: OxigraphBackend) -> None:
        self._inner = inner

    async def select(self, query: str, *, dataset: DatasetSpec) -> Sequence[Mapping[str, str]]:
        return await self._inner.select(query, dataset=dataset)

    async def ask(self, query: str, *, dataset: DatasetSpec) -> bool:
        return await self._inner.ask(query, dataset=dataset)

    async def construct_nt(self, query: str, *, dataset: DatasetSpec) -> bytes:
        return await self._inner.construct_nt(query, dataset=dataset)

    async def graphs(self) -> Sequence[str]:
        return await self._inner.graphs()

    async def health(self) -> bool:
        return await self._inner.health()
