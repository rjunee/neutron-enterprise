# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Knowledge domain Protocols (SPEC section 4).

Retrieval, ingestion, and the tiered graph/triple stores. Every I/O-crossing
method is ``async``. The graph (T2) and triple/shape (T3) surfaces are shipped
now so tiered deployments do not require a contract version bump later.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from ne.contracts.frozen import AnyMap, FrozenDict
from ne.contracts.types import (
    Chunk,
    Principal,
    RetrievalResult,
    ScoredChunk,
    SparseVector,
)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class KnowledgeItem(_Frozen):
    """A unit submitted for ingestion into the knowledge base."""

    item_id: str
    content: str
    metadata: AnyMap = FrozenDict()
    provenance: str


class UpsertReceipt(_Frozen):
    """Outcome of a :meth:`KnowledgeWrite.upsert` call."""

    accepted: tuple[str, ...]
    quarantined: tuple[str, ...]


class ShapeReport(_Frozen):
    """Result of SHACL/shape validation over a graph (T3)."""

    conforms: bool
    violations: tuple[str, ...]


class KnowledgeRead(Protocol):
    """Principal-scoped retrieval returning chunks + citations.

    ``search`` runs a principal-scoped query; ``retrieve`` is a principal-scoped
    fetch-by-id that is ACL-consistent with ``search`` (see below).
    """

    async def search(
        self,
        query: str,
        *,
        principal: Principal,
        top_k: int = 10,
        filters: Mapping[str, Any] | None = None,
    ) -> RetrievalResult: ...

    async def retrieve(self, chunk_ids: Sequence[str], *, principal: Principal) -> RetrievalResult:
        """Principal-scoped fetch-by-id, ACL-consistent with search.

        A principal can never ``retrieve()`` a chunk ``search()`` would trim; for
        citation/evidence replay (NE-062).
        """
        ...


class KnowledgeWrite(Protocol):
    """Ingestion, curation, and quarantine of knowledge items."""

    async def upsert(self, items: Sequence[KnowledgeItem]) -> UpsertReceipt: ...

    async def curate(self, item_id: str, patch: Mapping[str, Any], *, provenance: str) -> None: ...

    async def quarantine(self, item_id: str, reason: str) -> None: ...


class ChunkStore(Protocol):
    """Dense/sparse vector store over chunks."""

    async def upsert_chunks(self, chunks: Sequence[Chunk]) -> None: ...

    async def get(self, chunk_ids: Sequence[str]) -> Sequence[Chunk]:
        """Unscoped fetch-by-id for audit reconstruction.

        Serves NE-030 ("why did the system say X"); NOT principal-trimmed —
        callers above the audit layer must use :meth:`KnowledgeRead.retrieve`.
        """
        ...

    async def query(
        self,
        *,
        dense: Sequence[float] | None = None,
        sparse: SparseVector | None = None,
        top_k: int = 10,
        filters: Mapping[str, Any] | None = None,
    ) -> Sequence[ScoredChunk]:
        """Dense/sparse hybrid vector query.

        At least one of dense/sparse must be provided; adapters MUST raise
        ValueError when both are None.
        """
        ...

    async def delete(self, chunk_ids: Sequence[str]) -> None: ...


class Embedder(Protocol):
    """Dense text embedder."""

    @property
    def model_id(self) -> str: ...

    @property
    def dimension(self) -> int: ...

    async def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]: ...


class SparseEncoder(Protocol):
    """Learned sparse (e.g. SPLADE) encoder."""

    @property
    def model_id(self) -> str: ...

    async def encode(self, texts: Sequence[str]) -> Sequence[SparseVector]: ...


class Reranker(Protocol):
    """Cross-encoder reranker."""

    @property
    def model_id(self) -> str: ...

    async def rerank(
        self, query: str, chunks: Sequence[Chunk], *, top_k: int = 10
    ) -> Sequence[ScoredChunk]: ...


class GraphStore(Protocol):
    """Property-graph store (T2, v1.1 — openCypher subset per Apache AGE)."""

    async def upsert_nodes(self, nodes: Sequence[Mapping[str, Any]]) -> None: ...

    async def upsert_edges(self, edges: Sequence[Mapping[str, Any]]) -> None: ...

    async def cypher(
        self, query: str, params: Mapping[str, Any] | None = None
    ) -> Sequence[Mapping[str, Any]]: ...


class TripleStore(Protocol):
    """RDF triple store (T3)."""

    async def sparql(self, query: str) -> Sequence[Mapping[str, Any]]: ...

    async def insert_triples(self, triples: Sequence[tuple[str, str, str]]) -> None: ...


class ShapeValidator(Protocol):
    """SHACL-style shape validation over a graph (T3)."""

    async def validate(self, graph: Mapping[str, Any]) -> ShapeReport: ...
