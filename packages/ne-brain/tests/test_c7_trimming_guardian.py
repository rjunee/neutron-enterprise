# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""THE C7 security-trimming structural guardian (plan §2.1).

Makes it impossible to add a principal-scoped read path in ne-brain that silently
bypasses query-time ACL trimming. Three layers:

(a) STRUCTURAL SWEEP — AST-walk ``src/ne/brain`` and collect every class with an
    ``async def search`` OR ``async def retrieve`` that takes a ``principal`` arg
    (the KnowledgeRead principal-scoped surface). The set is PINNED to
    ``REGISTERED_PRINCIPAL_READ_PATHS``: a new such surface fails CI until it is
    registered here AND covered by layer (b). ``QdrantChunkStore``'s
    ``get``/``get_filtered``/``query`` are asserted principal-FREE, so the
    unscoped-by-design store fetch can never masquerade as a scoped path.
(b) BEHAVIORAL NO-BYPASS — for every registered class, prove BOTH paths route
    through the ACL hook: a match-nothing ``{}`` hook yields empty results with
    the store PROVABLY untouched; a non-empty hook makes the store's
    ``query()``/``get_filtered()`` receive the ACL filter server-side.
(c) PRODUCTION-HOOK INVARIANT — ``PrincipalAclFilter`` never returns ``None``/``{}``,
    so the two escape branches above are unreachable when wired for production.

FUTURE AUTHOR: if you add a principal-scoped ``search``/``retrieve`` to ne-brain,
this guardian WILL fail until you (1) add the class to
``REGISTERED_PRINCIPAL_READ_PATHS`` AND (2) add it to ``_BUILDERS`` so layer (b)
proves it applies the ACL hook on both paths. That friction is intentional: an
un-trimmed principal-scoped read path is a C7 authorization bypass.
"""

from __future__ import annotations

import ast
import asyncio
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from ne.brain.pipeline import CurrentAuthorizedRetrievalPipeline, RetrievalPipeline
from ne.brain.retriever import HybridRetriever
from ne.brain.testing.embedding import HashEmbedder, HashSparseEncoder
from ne.brain.trimming import PrincipalAclFilter
from ne.contracts.knowledge import KnowledgeRead
from ne.contracts.source_authority import (
    AuthorizedArtifactPageStatus,
    AuthorizedRetrievalEnvelope,
)
from ne.contracts.types import Chunk, Principal, RetrievalResult, ScoredChunk

_SRC = Path(__file__).resolve().parents[1] / "src" / "ne" / "brain"

REGISTERED_PRINCIPAL_READ_PATHS = {
    "CurrentAuthorizedRetrievalPipeline",
    "HybridRetriever",
    "RetrievalPipeline",
}

_GUARD_MESSAGE = (
    "C7 guardian: the set of principal-scoped read paths (async search/retrieve "
    "taking a `principal`) in ne-brain changed. A new such surface is an "
    "un-trimmed authorization bypass until you (1) add it to "
    "REGISTERED_PRINCIPAL_READ_PATHS and (2) add it to _BUILDERS so the "
    "behavioral no-bypass layer proves it applies the ACL hook on BOTH paths."
)

_PRINCIPAL = Principal(id="u-only")


# -- Layer (a): structural AST sweep -------------------------------------------


def _param_names(func: ast.AsyncFunctionDef | ast.FunctionDef) -> list[str]:
    a = func.args
    return [arg.arg for arg in (*a.posonlyargs, *a.args, *a.kwonlyargs)]


def _principal_scoped_read_classes() -> set[str]:
    found: set[str] = set()
    for path in _SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for item in node.body:
                if (
                    isinstance(item, ast.AsyncFunctionDef)
                    and item.name in {"search", "retrieve"}
                    and "principal" in _param_names(item)
                ):
                    found.add(node.name)
    return found


def test_a_principal_scoped_read_paths_are_pinned() -> None:
    assert _principal_scoped_read_classes() == REGISTERED_PRINCIPAL_READ_PATHS, _GUARD_MESSAGE


def test_a_chunkstore_fetch_methods_are_principal_free() -> None:
    tree = ast.parse((_SRC / "qdrant.py").read_text(encoding="utf-8"))
    checked: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.ClassDef) and node.name == "QdrantChunkStore"):
            continue
        for item in node.body:
            if isinstance(item, ast.AsyncFunctionDef | ast.FunctionDef) and item.name in {
                "get",
                "get_filtered",
                "query",
            }:
                checked.add(item.name)
                assert "principal" not in _param_names(item), (
                    f"QdrantChunkStore.{item.name} must stay unscoped-by-design "
                    f"(no principal arg). {_GUARD_MESSAGE}"
                )
    assert checked == {"get", "get_filtered", "query"}


# -- Layer (b): behavioral no-bypass -------------------------------------------


class _RecordingHook:
    def __init__(self, mapping: Mapping[str, Any]) -> None:
        self.mapping = mapping
        self.calls = 0

    def __call__(self, principal: Principal) -> Mapping[str, Any]:
        self.calls += 1
        return self.mapping


class _ExplodingStore:
    """Every async method raises — proves the store is never reached."""

    _MSG = "store reached despite match-nothing ACL"

    async def query(self, **kwargs: Any) -> Sequence[Any]:
        raise AssertionError(self._MSG)

    async def get(self, chunk_ids: Sequence[str]) -> Sequence[Chunk]:
        raise AssertionError(self._MSG)

    async def get_filtered(
        self, chunk_ids: Sequence[str], *, filters: Mapping[str, Any] | None = None
    ) -> Sequence[Chunk]:
        raise AssertionError(self._MSG)

    async def upsert_chunks(self, chunks: Sequence[Chunk]) -> None:
        raise AssertionError(self._MSG)

    async def delete(self, chunk_ids: Sequence[str]) -> None:
        raise AssertionError(self._MSG)


class _RecordingStore:
    """Records the filters each fetch method receives; returns nothing."""

    def __init__(self) -> None:
        self.query_filters: list[Any] = []
        self.get_filtered_filters: list[Any] = []
        self.get_calls: list[list[str]] = []

    async def query(
        self, *, dense: Any = None, sparse: Any = None, top_k: int = 10, filters: Any = None
    ) -> Sequence[Any]:
        self.query_filters.append(filters)
        return []

    async def get_filtered(
        self, chunk_ids: Sequence[str], *, filters: Mapping[str, Any] | None = None
    ) -> Sequence[Chunk]:
        self.get_filtered_filters.append(filters)
        return []

    async def get(self, chunk_ids: Sequence[str]) -> Sequence[Chunk]:
        self.get_calls.append(list(chunk_ids))
        return []

    async def upsert_chunks(self, chunks: Sequence[Chunk]) -> None:
        return None

    async def delete(self, chunk_ids: Sequence[str]) -> None:
        return None


class _IdentityReranker:
    @property
    def model_id(self) -> str:
        return "identity"

    async def rerank(
        self, query: str, chunks: Sequence[Chunk], *, top_k: int = 10
    ) -> Sequence[ScoredChunk]:
        return [ScoredChunk(chunk=c, score=1.0) for c in list(chunks)[:top_k]]


def _build_retriever(store: Any, hook: Callable[[Principal], Any]) -> KnowledgeRead:
    return HybridRetriever(
        store, HashEmbedder(32), HashSparseEncoder(), acl_filters=hook, audit_sink=None
    )


def _build_pipeline(store: Any, hook: Callable[[Principal], Any]) -> KnowledgeRead:
    return RetrievalPipeline(_build_retriever(store, hook), _IdentityReranker())


class _CurrentReadAdapter:
    """Expose a legacy ACL-trimmed read as a complete current-read envelope.

    The guardian is testing delegation of the principal and server-side ACL hook,
    not current-source selection (which has its own exact authority battery).
    """

    def __init__(self, delegate: KnowledgeRead) -> None:
        self._delegate = delegate

    async def search_current(
        self,
        query: str,
        *,
        principal: Principal,
        access_context_hash: str,
        request_hash: str,
        top_k: int = 10,
        scan_limit: int = 100,
        filters: Mapping[str, Any] | None = None,
        continuation_token: str | None = None,
    ) -> AuthorizedRetrievalEnvelope:
        del access_context_hash, request_hash, continuation_token
        result = await self._delegate.search(
            query, principal=principal, top_k=top_k, filters=filters
        )
        return AuthorizedRetrievalEnvelope(
            result=result,
            status=AuthorizedArtifactPageStatus.COMPLETE,
            scanned_count=len(result.chunks),
            returned_count=len(result.chunks),
            scan_limit=scan_limit,
        )

    async def retrieve_current(
        self,
        chunk_ids: Sequence[str],
        *,
        principal: Principal,
        access_context_hash: str,
    ) -> AuthorizedRetrievalEnvelope:
        del access_context_hash
        result = await self._delegate.retrieve(chunk_ids, principal=principal)
        return AuthorizedRetrievalEnvelope(
            result=result,
            status=AuthorizedArtifactPageStatus.COMPLETE,
            scanned_count=len(chunk_ids),
            returned_count=len(result.chunks),
            scan_limit=max(1, len(chunk_ids)),
        )


class _CurrentPipelineKnowledgeRead:
    """Drive the current-envelope surface through the guardian's common API."""

    def __init__(self, pipeline: CurrentAuthorizedRetrievalPipeline) -> None:
        self._pipeline = pipeline

    async def search(
        self,
        query: str,
        *,
        principal: Principal,
        top_k: int = 10,
        filters: Mapping[str, Any] | None = None,
    ) -> RetrievalResult:
        envelope = await self._pipeline.search(
            query,
            principal=principal,
            access_context_hash="0" * 64,
            request_hash="1" * 64,
            top_k=top_k,
            filters=filters,
        )
        return envelope.result

    async def retrieve(self, chunk_ids: Sequence[str], *, principal: Principal) -> RetrievalResult:
        envelope = await self._pipeline.retrieve(
            chunk_ids,
            principal=principal,
            access_context_hash="0" * 64,
        )
        return envelope.result


def _build_current_pipeline(store: Any, hook: Callable[[Principal], Any]) -> KnowledgeRead:
    return _CurrentPipelineKnowledgeRead(
        CurrentAuthorizedRetrievalPipeline(
            _CurrentReadAdapter(_build_retriever(store, hook)),  # type: ignore[arg-type]
            _IdentityReranker(),
        )
    )


_BUILDERS: list[tuple[str, Callable[[Any, Any], KnowledgeRead]]] = [
    ("CurrentAuthorizedRetrievalPipeline", _build_current_pipeline),
    ("HybridRetriever", _build_retriever),
    ("RetrievalPipeline", _build_pipeline),
]

# Every registered read-path class must have a builder in layer (b).
assert {name for name, _ in _BUILDERS} == REGISTERED_PRINCIPAL_READ_PATHS


@pytest.mark.parametrize(("name", "build"), _BUILDERS, ids=[b[0] for b in _BUILDERS])
def test_b_match_nothing_hook_yields_empty_without_touching_store(
    name: str, build: Callable[[Any, Any], KnowledgeRead]
) -> None:
    hook = _RecordingHook({})
    store = _ExplodingStore()
    reader = build(store, hook)

    async def scenario() -> None:
        search_result = await reader.search("q", principal=_PRINCIPAL, top_k=5)
        assert search_result.chunks == ()
        retrieve_result = await reader.retrieve(["c1"], principal=_PRINCIPAL)
        assert retrieve_result.chunks == ()

    asyncio.run(scenario())
    assert hook.calls == 2  # once per principal-scoped path, no more


@pytest.mark.parametrize(("name", "build"), _BUILDERS, ids=[b[0] for b in _BUILDERS])
def test_b_nonempty_hook_applies_acl_filter_server_side(
    name: str, build: Callable[[Any, Any], KnowledgeRead]
) -> None:
    hook = _RecordingHook({"acl": ["u-only"]})
    store = _RecordingStore()
    reader = build(store, hook)

    async def scenario() -> None:
        await reader.search("q", principal=_PRINCIPAL, top_k=5)
        await reader.retrieve(["c1", "c2"], principal=_PRINCIPAL)

    asyncio.run(scenario())
    assert store.query_filters, "search() must reach the store's query()"
    for filters in store.query_filters:
        assert filters is not None and filters.get("acl") == ["u-only"]
    assert store.get_filtered_filters == [{"acl": ["u-only"]}]
    assert store.get_calls == []  # scoped retrieve never falls back to unscoped get()


# -- Layer (c): production-hook invariant --------------------------------------


class _EmptyExpander:
    def expand(self, principal: Principal | str) -> frozenset[str]:
        return frozenset()


def test_c_production_hook_never_returns_none_or_empty() -> None:
    # Even over an expander that returns nothing, PrincipalAclFilter emits a
    # non-empty mapping (containing the principal id) — so the None/{} escape
    # branches are unreachable in production.
    hook = PrincipalAclFilter(_EmptyExpander())
    mapping = hook(Principal(id="u-only"))
    assert mapping is not None
    assert mapping != {}
    assert mapping == {"acl": ["u-only"]}
