# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Hash-diff idempotent ontology hot-reload (NE-085a)."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence

import pytest

from ne.brain_ontology.ontology import load_manifest
from ne.brain_ontology.store.oxigraph import OxigraphBackend
from ne.brain_ontology.store.protocols import DatasetSpec
from ne.brain_ontology.store.sync import SYNC_STATE_GRAPH, sync_ontologies


class _RecordingBackend:
    """Wraps a real backend, counting write calls (replace/insert/delete/load)."""

    def __init__(self) -> None:
        self._inner = OxigraphBackend()
        self.write_calls = 0

    async def select(self, query: str, *, dataset: DatasetSpec) -> Sequence[Mapping[str, str]]:
        return await self._inner.select(query, dataset=dataset)

    async def ask(self, query: str, *, dataset: DatasetSpec) -> bool:
        return await self._inner.ask(query, dataset=dataset)

    async def construct_nt(self, query: str, *, dataset: DatasetSpec) -> bytes:
        return await self._inner.construct_nt(query, dataset=dataset)

    async def insert_nt(self, data: bytes, *, graph_iri: str) -> None:
        self.write_calls += 1
        await self._inner.insert_nt(data, graph_iri=graph_iri)

    async def load_turtle(self, data: bytes, *, graph_iri: str) -> None:
        self.write_calls += 1
        await self._inner.load_turtle(data, graph_iri=graph_iri)

    async def replace_graph_turtle(self, data: bytes, *, graph_iri: str) -> None:
        self.write_calls += 1
        await self._inner.replace_graph_turtle(data, graph_iri=graph_iri)

    async def delete_graph(self, graph_iri: str) -> None:
        self.write_calls += 1
        await self._inner.delete_graph(graph_iri)

    async def graphs(self) -> Sequence[str]:
        return await self._inner.graphs()

    async def health(self) -> bool:
        return True


def test_first_sync_loads_all_then_resync_is_noop(no_network: None) -> None:
    async def scenario() -> None:
        backend = _RecordingBackend()
        manifest = load_manifest()
        report1 = await sync_ontologies(backend, manifest)
        assert report1.loaded == 13
        assert report1.replaced == 0
        assert report1.skipped == 0
        writes_after_first = backend.write_calls
        assert writes_after_first > 0
        # Immediate re-sync: ZERO backend writes (idempotent).
        report2 = await sync_ontologies(backend, manifest)
        assert report2.skipped == 13
        assert report2.loaded == 0
        assert report2.replaced == 0
        assert backend.write_calls == writes_after_first

    asyncio.run(scenario())


def test_changed_manifest_entry_reloads_exactly_that_graph(no_network: None) -> None:
    async def scenario() -> None:
        backend = _RecordingBackend()
        manifest = load_manifest()
        await sync_ontologies(backend, manifest)
        writes_after_first = backend.write_calls

        # Mutate ONE manifest entry's pinned sha256 (in-memory) so exactly that
        # graph reloads on the next sync.
        source = manifest.sources["bfo"]
        entry = source.files[0]
        changed_entry = entry.model_copy(update={"sha256": "0" * 64})
        changed_source = source.model_copy(update={"files": (changed_entry,)})
        mutated = manifest.model_copy(
            update={"sources": {**manifest.sources, "bfo": changed_source}}
        )
        report = await sync_ontologies(backend, mutated)
        assert report.replaced == 1
        assert report.loaded == 0
        # exactly one corpus-graph replace + one state-graph rewrite.
        assert backend.write_calls == writes_after_first + 2

    asyncio.run(scenario())


def test_state_lives_in_bookkeeping_graph(no_network: None) -> None:
    async def scenario() -> None:
        backend = OxigraphBackend()
        manifest = load_manifest()
        await sync_ontologies(backend, manifest)
        rows = await backend.select(
            f"SELECT ?g WHERE {{ GRAPH <{SYNC_STATE_GRAPH}> {{ ?g ?p ?o }} }}",
            dataset=DatasetSpec(),
        )
        assert len(rows) == 13

    asyncio.run(scenario())


def test_sync_refuses_non_ontology_graph_iri(no_network: None) -> None:
    async def scenario() -> None:
        backend = OxigraphBackend()
        manifest = load_manifest()
        # Rewrite the graph_iri_for machinery by poisoning a source name so a
        # produced IRI escapes urn:ne:ontology: — the writer must refuse.
        from ne.brain_ontology.store import sync as sync_mod

        pinned = {"http://evil/graph": "deadbeef"}

        def _fake_sha_by_graph(_m: object) -> dict[str, str]:
            return pinned

        original = sync_mod._sha_by_graph
        sync_mod._sha_by_graph = _fake_sha_by_graph  # type: ignore[assignment]
        try:
            with pytest.raises(ValueError, match="non-ontology graph IRI"):
                await sync_ontologies(backend, manifest)
        finally:
            sync_mod._sha_by_graph = original  # type: ignore[assignment]

    asyncio.run(scenario())
