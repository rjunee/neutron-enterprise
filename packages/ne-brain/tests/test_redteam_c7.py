# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-REDTEAM — adversarial retrieval against the C7 permission-trim chokepoint.

Real ``AsyncQdrantClient(':memory:')`` + real ``QdrantChunkStore`` + REAL
ne-permissions ``PrincipalStore``/``PrincipalExpander`` + real
``PrincipalAclFilter``, composed EXACTLY like the production root (mirrors
``test_c7_provable_trimming.py``'s harness — replicated inline, never importing its
private ``_Env``/``env``). All chunks share identical text so an UNTRIMMED search
would rank every one at the top; any absence is provably an ACL trim, not a
relevance miss.

The attacker probes for chunks it lacks ACLs for via crafted caller filters
(wildcard / exact-hidden-label / reserved ``$``-token) and a label-prefix
collision. The property under attack: **under-return, never over-return** — on
BOTH the search and retrieve paths.

Each test states its MUTATION-CHECK proving non-vacuity.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from qdrant_client import AsyncQdrantClient, models

from ne.brain.pgvector.filters import compile_filters
from ne.brain.qdrant import QdrantChunkStore, _build_filter
from ne.brain.retriever import HybridRetriever
from ne.brain.testing.embedding import HashEmbedder, HashSparseEncoder
from ne.brain.trimming import PrincipalAclFilter
from ne.contracts.knowledge import KnowledgeRead
from ne.contracts.types import Chunk, Principal
from ne.permissions.expand import PrincipalExpander
from ne.permissions.models import PrincipalKind, SyncedPrincipal
from ne.permissions.store import PrincipalStore, ensure_permissions_schema

NOW = datetime(2026, 7, 10, tzinfo=UTC)
_TEXT = "quarterly revenue report"

# The attacker (u-att) is a store-known principal whose ONLY group is "group:eng"
# — a PREFIX of the "group:engineering" label a hidden chunk carries.
_CORPUS = [
    Chunk(chunk_id="c-att", document_id="d", text=_TEXT, acl=("u-att",)),  # attacker's own
    Chunk(chunk_id="c-secret", document_id="d", text=_TEXT, acl=("group:secret",)),  # hidden
    Chunk(chunk_id="c-eng-full", document_id="d", text=_TEXT, acl=("group:engineering",)),  # prefix
]
_ALL_IDS = [c.chunk_id for c in _CORPUS]
_TOP_K = 20

P_ATT = Principal(id="u-att")


@dataclass
class _RedEnv:
    retriever: HybridRetriever
    principals: PrincipalStore


@pytest.fixture
def redenv(tmp_path: Path) -> _RedEnv:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'principals.db'}")
    ensure_permissions_schema(engine)
    pstore = PrincipalStore(engine)
    pstore.upsert_principals(
        [SyncedPrincipal(principal_id="u-att", kind=PrincipalKind.USER)],
        source="test",
        now=NOW,
    )
    # u-att is a member of group:eng only — a PREFIX of group:engineering.
    pstore.set_member_groups("u-att", ["group:eng"], source="test", now=NOW)

    expander = PrincipalExpander(pstore)
    hook = PrincipalAclFilter(expander)

    client = AsyncQdrantClient(":memory:")
    embedder = HashEmbedder(32)
    store = QdrantChunkStore(
        client, f"redc7_{uuid.uuid4().hex}", embedder, HashSparseEncoder(), group_expander=expander
    )

    async def setup() -> None:
        await store.ensure_collection()
        await store.upsert_chunks(_CORPUS)

    asyncio.run(setup())
    retriever = HybridRetriever(
        store, embedder, HashSparseEncoder(), acl_filters=hook, audit_sink=None
    )
    return _RedEnv(retriever=retriever, principals=pstore)


async def _search_ids(reader: KnowledgeRead, principal: Principal, **kw: object) -> set[str]:
    result = await reader.search(_TEXT, principal=principal, top_k=_TOP_K, **kw)  # type: ignore[arg-type]
    return {sc.chunk.chunk_id for sc in result.chunks}


async def _retrieve_ids(reader: KnowledgeRead, principal: Principal, ids: list[str]) -> set[str]:
    result = await reader.retrieve(ids, principal=principal)
    return {sc.chunk.chunk_id for sc in result.chunks}


# --- (a) wildcard / exact-hidden-label caller filter cannot WIDEN --------------


def test_wildcard_or_exact_secret_label_caller_filter_cannot_widen(redenv: _RedEnv) -> None:
    """The attacker supplies a caller ``filters={'acl': [...]}`` — a wildcard ``*``,
    then the EXACT hidden label ``group:secret`` — trying to widen its view. The
    caller filter is AND-combined (intersected) with the principal ACL, never a
    replacement, so the hidden chunk is absent on the search path; retrieve-by-id is
    equally trimmed.

    MUTATION-CHECK: if the caller filter REPLACED the principal ACL (instead of
    ``and_combine_filters`` intersecting it in ne/brain/retriever.py:277-280),
    naming ``group:secret`` in the caller position would surface ``c-secret``.
    """

    async def scenario() -> None:
        # Control: the attacker CAN see its own chunk — the harness is not vacuously
        # empty.
        assert "c-att" in await _search_ids(redenv.retriever, P_ATT)

        for crafted in (["*"], ["group:secret"], ["*", "group:secret"]):
            ids = await _search_ids(redenv.retriever, P_ATT, filters={"acl": crafted})
            assert "c-secret" not in ids, f"hidden chunk leaked via caller filter {crafted!r}"

        # Retrieve-by-id of the known hidden id is trimmed identically.
        assert await _retrieve_ids(redenv.retriever, P_ATT, ["c-secret"]) == set()

    asyncio.run(scenario())


# --- (b) reserved $-KEY rejected at both compilers; $-VALUE is a literal -------


def test_reserved_dollar_key_rejected_and_reserved_value_is_literal() -> None:
    """An injected unknown ``$``-prefixed KEY is rejected fail-closed by BOTH the
    qdrant (``_build_filter``) and pgvector (``compile_filters``) compilers (cross-
    compiler parity), while a reserved-LOOKING ACL VALUE (``$any`` / ``*``) is
    treated as an opaque literal label — the anti-smuggling boundary is at the KEY
    level, so a value can never be reinterpreted as an OR-operator.

    MUTATION-CHECK: a compiler that silently IGNORED an unknown ``$``-key (instead
    of raising) would admit an attacker-injected branch; a compiler that
    SPECIAL-CASED a ``$``-prefixed VALUE would mishandle a legitimate literal label.
    """
    # Cross-compiler parity: an unknown reserved key fails closed on both seams.
    with pytest.raises(ValueError, match="unknown reserved filter key"):
        _build_filter({"$evil": [{"acl": ["x"]}]})
    with pytest.raises(ValueError, match="unknown reserved filter key"):
        compile_filters({"$evil": [{"acl": ["x"]}]})

    # A reserved-looking VALUE is an ordinary literal label (matches nothing here).
    flt = _build_filter({"acl": ["$any", "*"]})
    assert flt is not None and isinstance(flt.must, list) and len(flt.must) == 1
    cond = flt.must[0]
    assert isinstance(cond, models.FieldCondition)
    assert isinstance(cond.match, models.MatchAny)
    assert cond.key == "acl"
    assert list(cond.match.any) == ["$any", "*"]

    # pgvector: the literal "$any" value is parameterized as an ordinary acl label.
    cond_pg = compile_filters({"acl": ["$any"]})
    assert cond_pg is not None
    assert json.dumps({"acl": "$any"}) in cond_pg.params


# --- (c) label PREFIX collision does not leak (exact match only) --------------


def test_label_prefix_collision_non_leak_qdrant(redenv: _RedEnv) -> None:
    """The attacker's group ``group:eng`` is a PREFIX of the ``group:engineering``
    label on ``c-eng-full``. ACL matching is EXACT, so the chunk is invisible on
    both the search and retrieve paths — a genuinely new surface (T3 covers the
    exact-match property at the graph store; this pins it at the T1 qdrant store).

    MUTATION-CHECK: if the qdrant store used a prefix/substring match instead of an
    exact keyword ``MatchAny`` on the ``acl`` field, ``group:eng`` would leak
    ``c-eng-full``.
    """

    async def scenario() -> None:
        searched = await _search_ids(redenv.retriever, P_ATT)
        assert "c-eng-full" not in searched, "prefix group leaked the exact-labelled chunk"
        assert await _retrieve_ids(redenv.retriever, P_ATT, ["c-eng-full"]) == set()
        # And retrieve of ALL ids returns only what the attacker may actually see.
        assert await _retrieve_ids(redenv.retriever, P_ATT, _ALL_IDS) == {"c-att"}

    asyncio.run(scenario())
