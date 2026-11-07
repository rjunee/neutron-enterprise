# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
#
# Portions derived from jupyter-naas/abi
# libs/naas-abi-core/naas_abi_core/services/triple_store/TripleStoreService.py
# (lines 436-604) @ a3e15eff03902091f23acfd9edbc95dc9695183f — MIT License,
# Copyright (C) 2025 Naas.ai. The sha256 hash-diff idempotent schema-reload
# algorithm (store a content hash per schema graph; on reload compare and rewrite
# only changed graphs) is carried over. De-naased: ABI's base64-in-graph content
# store, its ThreadPoolExecutor + event-bus publish, and its per-file mtime
# bookkeeping are dropped; the state is one (graph_iri, sha256) triple per corpus
# graph in a bookkeeping named graph, driven by the vendored-corpus manifest.
"""Idempotent, restart-safe ontology hot-reload (NE-085a).

:func:`sync_ontologies` loads the vendored BFO/CCO corpus into the backend,
rewriting ONLY the graphs whose pinned sha256 differs from what is recorded in
the bookkeeping graph ``urn:ne:ontology:sync-state``. Re-running against an
already-loaded store is a no-op (zero backend writes); a changed manifest pin
swaps exactly the changed graphs.

Schema-writer containment (fail-closed): this module writes ONLY named graphs
under ``urn:ne:ontology:`` — it can never touch instance-data graphs. A corpus
IRI outside that namespace is a hard error.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from ..ontology import OntologyManifest, graph_iri_for, iter_corpus
from .protocols import DatasetSpec, TripleStoreBackend

#: Bookkeeping graph holding one ``<graph> sync:sha256 "<hex>"`` triple per graph.
SYNC_STATE_GRAPH = "urn:ne:ontology:sync-state"
#: The only namespace prefix this writer may touch.
_ONTOLOGY_PREFIX = "urn:ne:ontology:"
_SHA_PREDICATE = "urn:ne:ontology:sync#sha256"


class SyncReport(BaseModel):
    """Outcome of one :func:`sync_ontologies` run."""

    model_config = ConfigDict(frozen=True)

    loaded: int = 0
    """Corpus graphs newly loaded (absent from prior sync state)."""
    replaced: int = 0
    """Corpus graphs reloaded because their pinned sha256 changed."""
    skipped: int = 0
    """Corpus graphs left untouched (pinned sha256 unchanged)."""


def _guard_ontology_iri(graph_iri: str) -> None:
    if not graph_iri.startswith(_ONTOLOGY_PREFIX):
        raise ValueError(
            f"sync refuses non-ontology graph IRI {graph_iri!r}: this writer may "
            f"only touch {_ONTOLOGY_PREFIX}* schema graphs (fail-closed containment)"
        )


def _sha_by_graph(manifest: OntologyManifest) -> dict[str, str]:
    """Map every corpus graph IRI to its pinned manifest sha256."""
    mapping: dict[str, str] = {}
    for name, source in manifest.sources.items():
        for entry in source.files:
            mapping[graph_iri_for(name, entry.local_path)] = entry.sha256
    return mapping


async def _read_state(backend: TripleStoreBackend) -> dict[str, str]:
    rows = await backend.select(
        f"SELECT ?g ?h WHERE {{ GRAPH <{SYNC_STATE_GRAPH}> {{ ?g <{_SHA_PREDICATE}> ?h }} }}",
        dataset=DatasetSpec(),
    )
    return {row["g"]: row["h"] for row in rows if "g" in row and "h" in row}


def _state_turtle(state: dict[str, str]) -> bytes:
    lines = [
        f"<{graph}> <{_SHA_PREDICATE}> {_quote(sha)} ." for graph, sha in sorted(state.items())
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _quote(literal: str) -> str:
    escaped = literal.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


async def sync_ontologies(backend: TripleStoreBackend, manifest: OntologyManifest) -> SyncReport:
    """Hash-diff load the vendored corpus into ``backend``; rewrite only changes."""
    pinned = _sha_by_graph(manifest)
    for graph_iri in pinned:
        _guard_ontology_iri(graph_iri)

    stored = await _read_state(backend)
    loaded = replaced = skipped = 0

    for graph_iri, _path, data in iter_corpus(manifest):
        want = pinned[graph_iri]
        have = stored.get(graph_iri)
        if have == want:
            skipped += 1
            continue
        _guard_ontology_iri(graph_iri)
        await backend.replace_graph_turtle(data, graph_iri=graph_iri)
        if have is None:
            loaded += 1
        else:
            replaced += 1

    if loaded or replaced:
        # Only rewrite the bookkeeping graph when something actually changed, so a
        # clean re-sync issues zero backend writes.
        await backend.replace_graph_turtle(_state_turtle(pinned), graph_iri=SYNC_STATE_GRAPH)

    return SyncReport(loaded=loaded, replaced=replaced, skipped=skipped)
