# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The Agent-declared knowledge boundary is a RETRIEVAL filter, not a label.

THE DEFECT THESE PIN. ``deploy/agents/registry.toml`` has the Meridian Analyst
declare ``knowledge_sources = [{ source_id = "meridian-servicing", ... }]``. That
declaration was consumed only by admission/readiness, never by retrieval, so
``read.knowledge.search`` swept the WHOLE chunk store. With a 500-chunk unrelated
fixture corpus resident alongside the 23-chunk demo corpus — and the fixture
holding a lexical decoy ("Meridian Robotics Inc") — the wrong corpus won the
ranking and the Agent honestly refused, naming documents it should never have
been able to read at all. An Agent granted one governed source could read every
other source in the deployment.

THE PAIRED MUTATIONS, one test each way:

* the Agent retrieves from its DECLARED source and not from another
  <-> a declared scope that resolves to NOTHING fails loudly rather than widening
* authorization filtering still applies ON TOP of scope
  <-> a chunk the principal may not see is still refused, in or out of scope

The end-to-end half runs over a REAL ``QdrantChunkStore`` (``:memory:``) behind a
REAL ``HybridRetriever`` with the REAL ``PrincipalAclFilter``, so the whole
composition is exercised: carrier -> resolver -> caller filter ->
``and_combine_filters`` -> ``_build_filter`` -> Qdrant. Nothing about the
narrowing is asserted against a mock.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

import pytest
from qdrant_client import AsyncQdrantClient

from ne.api.knowledge_scope import (
    CONNECTOR_FILTER_KEY,
    KNOWLEDGE_SOURCE_CONNECTORS,
    KnowledgeScopeResolver,
    KnowledgeScopeUnresolved,
    current_run_knowledge_scope,
    declared_source_ids,
    run_knowledge_scope,
)
from ne.api.tools import TURN_PRINCIPAL, build_chat_bindings
from ne.brain.qdrant import QdrantChunkStore
from ne.brain.retriever import HybridRetriever
from ne.brain.testing.embedding import HashEmbedder, HashSparseEncoder
from ne.brain.trimming import PrincipalAclFilter
from ne.contracts.types import Chunk, Principal, RetrievalResult, ScoredChunk

_M365 = "m365-sharepoint"
_ACME = "acme-fixture"

#: The Analyst's own declaration, verbatim from ``deploy/agents/registry.toml``.
_DECLARED = frozenset({"meridian-servicing"})

_OPS = Principal(id="ops@meridian.test", groups=("ops",))


# --- the resolution table ------------------------------------------------------


def test_the_declared_meridian_source_resolves_to_the_m365_corpus() -> None:
    """The logical registry name resolves to the concrete connector on a chunk."""
    assert KnowledgeScopeResolver().filters(_DECLARED) == {CONNECTOR_FILTER_KEY: [_M365]}


def test_the_filter_key_is_the_nested_payload_path_qdrant_actually_stores() -> None:
    """``QdrantChunkStore`` writes ``chunk.model_dump()``, so metadata is nested.

    A bare ``"connector"`` key would compile to a top-level payload path, match
    zero points, and — because a disjoint filter degrades to an empty result
    rather than an error — look exactly like "the corpus has nothing relevant".
    """
    assert CONNECTOR_FILTER_KEY == "metadata.connector"


def test_an_unknown_source_id_refuses_instead_of_widening() -> None:
    with pytest.raises(KnowledgeScopeUnresolved, match="unresolvable"):
        KnowledgeScopeResolver().filters({"meridian-servicing-typo"})


def test_a_scope_declaring_nothing_refuses_instead_of_widening() -> None:
    """An Agent that declared no knowledge source may not read the whole store."""
    with pytest.raises(KnowledgeScopeUnresolved, match="no searchable corpus"):
        KnowledgeScopeResolver().filters(())


def test_a_graph_only_source_alone_refuses_instead_of_widening() -> None:
    """``meridian-governed-local-t3`` is a T3 release, not a chunk-store corpus."""
    assert KNOWLEDGE_SOURCE_CONNECTORS["meridian-governed-local-t3"] == ()
    with pytest.raises(KnowledgeScopeUnresolved, match="no searchable corpus"):
        KnowledgeScopeResolver().filters({"meridian-governed-local-t3"})


def test_a_graph_only_source_beside_a_searchable_one_narrows_to_the_searchable() -> None:
    resolved = KnowledgeScopeResolver().filters(
        ["meridian-servicing", "meridian-governed-local-t3"]
    )
    assert resolved == {CONNECTOR_FILTER_KEY: [_M365]}


def test_the_resolved_filter_is_never_an_empty_mapping() -> None:
    """``{}`` and ``None`` both read as 'no filter' downstream — neither may escape."""
    for source_ids in ((), ("meridian-governed-local-t3",), ("unknown",)):
        with pytest.raises(KnowledgeScopeUnresolved):
            KnowledgeScopeResolver().filters(source_ids)


# --- the carrier ---------------------------------------------------------------


def test_an_empty_bound_scope_is_not_the_same_as_no_run() -> None:
    """The distinction the whole contract rests on: ``frozenset()`` != ``None``."""
    assert current_run_knowledge_scope() is None
    with run_knowledge_scope(()):
        assert current_run_knowledge_scope() == frozenset()
    assert current_run_knowledge_scope() is None


def test_the_carrier_resets_even_when_the_run_raises() -> None:
    with pytest.raises(RuntimeError, match="boom"), run_knowledge_scope(_DECLARED):
        raise RuntimeError("boom")
    assert current_run_knowledge_scope() is None


def test_declared_source_ids_includes_optional_grants() -> None:
    """An optional grant that IS admitted is a source the Agent may really read."""

    class _Grant:
        def __init__(self, source_id: str) -> None:
            self.source_id = source_id

    class _Definition:
        knowledge_sources = (_Grant("meridian-servicing"),)
        optional_knowledge_sources = (_Grant("meridian-governed-local-t3"),)

    assert declared_source_ids(_Definition()) == {  # type: ignore[arg-type]
        "meridian-servicing",
        "meridian-governed-local-t3",
    }


# --- the tool handler ----------------------------------------------------------


class _RecordingRetriever:
    """Fake ``KnowledgeRead`` recording exactly what the handler asked the store for."""

    def __init__(self) -> None:
        self.search_calls: list[dict[str, Any]] = []

    async def search(
        self,
        query: str,
        *,
        principal: Principal,
        top_k: int = 10,
        filters: Mapping[str, Any] | None = None,
    ) -> RetrievalResult:
        self.search_calls.append({"query": query, "top_k": top_k, "filters": filters})
        return RetrievalResult(chunks=(), citations=())

    async def retrieve(self, chunk_ids: Sequence[str], *, principal: Principal) -> RetrievalResult:
        raise NotImplementedError


async def _call_search(retriever: Any) -> str:
    (binding,) = build_chat_bindings(retriever, extraction_mode="ingest_review")
    token = TURN_PRINCIPAL.set(_OPS)
    try:
        return await binding.handler({"query": "DR-4830 Meridian auto loan hardship"})
    finally:
        TURN_PRINCIPAL.reset(token)


def test_a_run_scoped_search_carries_the_declared_source_filter() -> None:
    retriever = _RecordingRetriever()
    with run_knowledge_scope(_DECLARED):
        asyncio.run(_call_search(retriever))
    assert retriever.search_calls[0]["filters"] == {CONNECTOR_FILTER_KEY: [_M365]}


def test_an_unresolvable_run_scope_never_reaches_the_store() -> None:
    """The anti-widening mutation: refuse, and do not search at all.

    Silently searching everything because a declared scope did not match is
    precisely how this hid. The retriever must record ZERO calls.
    """
    retriever = _RecordingRetriever()
    with run_knowledge_scope({"meridian-servicing-typo"}), pytest.raises(KnowledgeScopeUnresolved):
        asyncio.run(_call_search(retriever))
    assert retriever.search_calls == []


def test_an_agent_declaring_no_source_never_reaches_the_store() -> None:
    retriever = _RecordingRetriever()
    with run_knowledge_scope(()), pytest.raises(KnowledgeScopeUnresolved):
        asyncio.run(_call_search(retriever))
    assert retriever.search_calls == []


def test_an_unscoped_chat_turn_is_unfiltered() -> None:
    """No Agent Run in scope: the principal's own ACL stays the only boundary."""
    retriever = _RecordingRetriever()
    assert current_run_knowledge_scope() is None
    asyncio.run(_call_search(retriever))
    assert retriever.search_calls[0]["filters"] is None


# --- end to end over the real store --------------------------------------------


class _Expander:
    """The ``GroupExpander`` seam: a principal sees its own id and its groups."""

    def expand(self, principal: Principal | str) -> frozenset[str]:
        if isinstance(principal, str):
            return frozenset({principal})
        return frozenset({principal.id, *principal.groups})


def _chunk(chunk_id: str, *, connector: str, text: str, acl: tuple[str, ...]) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        document_id=chunk_id,
        text=text,
        metadata={"connector": connector, "source_system": connector},
        acl=acl,
    )


#: The demo corpus, the decoy that beat it, and a chunk the ops principal may not read.
_CORPUS = (
    _chunk(
        "m365-sop",
        connector=_M365,
        text=(
            "Disaster Hardship Response SOP (2026): DR-4830 disaster declaration "
            "affected counties, Meridian auto loan hardship offer eligibility."
        ),
        acl=("ops",),
    ),
    _chunk(
        "acme-decoy",
        connector=_ACME,
        text="Renewal contract for Meridian Robotics Inc. Shipment confirmation and travel policy.",
        acl=("ops",),
    ),
    _chunk(
        "m365-restricted",
        connector=_M365,
        text="DR-4830 Meridian auto loan hardship: restricted servicing notes.",
        acl=("legal",),
    ),
)


@pytest.fixture
def live_retriever() -> HybridRetriever:
    """A REAL Qdrant-backed hybrid retriever holding both corpora."""
    store = QdrantChunkStore(
        AsyncQdrantClient(":memory:"),
        f"scope_{uuid.uuid4().hex}",
        HashEmbedder(32),
        HashSparseEncoder(),
    )
    asyncio.run(store.ensure_collection())
    asyncio.run(store.upsert_chunks(_CORPUS))
    return HybridRetriever(
        store,
        HashEmbedder(32),
        HashSparseEncoder(),
        PrincipalAclFilter(_Expander()),
        audit_sink=None,
    )


def _served(retriever: HybridRetriever, *, principal: Principal) -> set[str]:
    """Every chunk id the governed search actually served."""
    result = asyncio.run(
        retriever.search("DR-4830 Meridian hardship", principal=principal, top_k=10)
    )
    return {scored.chunk.chunk_id for scored in result.chunks}


def test_without_a_scope_the_unrelated_fixture_corpus_is_reachable(
    live_retriever: HybridRetriever,
) -> None:
    """The defect, reproduced: unscoped, the decoy is a legitimate candidate.

    This is the baseline the scoped test is measured against — if the fixture
    ever stopped being reachable for an unrelated reason, the scoped assertion
    below would pass vacuously.
    """
    assert "acme-decoy" in _served(live_retriever, principal=_OPS)


def test_a_scoped_search_reads_the_declared_corpus_and_not_the_other(
    live_retriever: HybridRetriever,
) -> None:
    """The fix, end to end through the real filter composition."""
    filters = KnowledgeScopeResolver().filters(_DECLARED)
    result = asyncio.run(
        live_retriever.search(
            "DR-4830 Meridian hardship", principal=_OPS, top_k=10, filters=filters
        )
    )
    served = {scored.chunk.chunk_id for scored in result.chunks}
    assert served == {"m365-sop"}, "scope must admit the declared corpus only"


def test_authorization_still_applies_on_top_of_scope(
    live_retriever: HybridRetriever,
) -> None:
    """Scope is ON TOP of authorization, never instead of it.

    ``m365-restricted`` is INSIDE the declared scope and still refused, because
    the ACL hook AND-combines with the caller filter rather than being replaced
    by it. Both legs are proved at once: the in-scope authorized chunk is served,
    the in-scope unauthorized chunk is not.
    """
    filters = KnowledgeScopeResolver().filters(_DECLARED)
    result = asyncio.run(
        live_retriever.search(
            "DR-4830 Meridian hardship", principal=_OPS, top_k=10, filters=filters
        )
    )
    served = {scored.chunk.chunk_id for scored in result.chunks}
    assert "m365-restricted" not in served
    assert "m365-sop" in served


def test_a_principal_outside_the_scoped_corpus_acl_is_served_nothing(
    live_retriever: HybridRetriever,
) -> None:
    """Scope cannot grant what ACL withholds — narrowing only, never widening."""
    stranger = Principal(id="stranger@meridian.test", groups=("marketing",))
    filters = KnowledgeScopeResolver().filters(_DECLARED)
    result = asyncio.run(
        live_retriever.search(
            "DR-4830 Meridian hardship", principal=stranger, top_k=10, filters=filters
        )
    )
    assert result.chunks == ()


def test_the_legal_principal_still_reads_the_restricted_chunk_in_scope(
    live_retriever: HybridRetriever,
) -> None:
    """The ACL leg is genuinely evaluated, not incidentally empty."""
    legal = Principal(id="counsel@meridian.test", groups=("legal",))
    filters = KnowledgeScopeResolver().filters(_DECLARED)
    result = asyncio.run(
        live_retriever.search(
            "DR-4830 Meridian hardship", principal=legal, top_k=10, filters=filters
        )
    )
    served = {scored.chunk.chunk_id for scored in result.chunks}
    assert served == {"m365-restricted"}


def test_the_handler_and_the_store_agree_on_the_filter_shape(
    live_retriever: HybridRetriever,
) -> None:
    """The filter the handler builds is the filter the real store accepts.

    Guards the seam between the two halves above: a resolver change that broke
    the payload path would leave every unit test green while the live search
    silently returned nothing.
    """
    recorded = _RecordingRetriever()
    with run_knowledge_scope(_DECLARED):
        asyncio.run(_call_search(recorded))
    handler_filters = recorded.search_calls[0]["filters"]
    result = asyncio.run(
        live_retriever.search(
            "DR-4830 Meridian hardship", principal=_OPS, top_k=10, filters=handler_filters
        )
    )
    assert {scored.chunk.chunk_id for scored in result.chunks} == {"m365-sop"}


def test_scored_chunks_stay_well_formed_under_a_scope(
    live_retriever: HybridRetriever,
) -> None:
    """Citations track the served chunks — scope must not desynchronize them."""
    filters = KnowledgeScopeResolver().filters(_DECLARED)
    result = asyncio.run(
        live_retriever.search(
            "DR-4830 Meridian hardship", principal=_OPS, top_k=10, filters=filters
        )
    )
    assert all(isinstance(scored, ScoredChunk) for scored in result.chunks)
    assert {c.chunk_id for c in result.citations} == {
        scored.chunk.chunk_id for scored in result.chunks
    }


# --- the mapping cannot drift away from the deployment -------------------------


def test_the_m365_connector_value_is_the_connector_default_not_a_guess() -> None:
    """The table's right-hand side is the value the connector actually stamps.

    A resolved filter naming a connector no chunk carries would return an empty
    result rather than an error — quiet in exactly the way this whole defect was
    quiet. Pinning it to ``M365ConnectorSettings.source_name``'s default makes a
    rename a failing test instead of a silently empty demo.
    """
    from ne.connect.m365.config import M365ConnectorSettings

    assert M365ConnectorSettings.model_fields["source_name"].default == _M365
    assert KNOWLEDGE_SOURCE_CONNECTORS["meridian-servicing"] == (_M365,)


def test_every_source_the_registry_declares_has_a_resolution() -> None:
    """An Agent may not declare a knowledge source the resolver cannot resolve.

    Catches the operator-facing failure mode at build time: adding a
    ``knowledge_sources`` entry without a mapping would otherwise surface as a
    refused search during a live run.
    """
    from pathlib import Path

    from ne.api.agent_manifest import load_agent_manifest

    registry = Path(__file__).parents[3] / "deploy/agents/registry.toml"
    declared = {
        grant.source_id
        for definition in load_agent_manifest(registry).manifest.agents
        for grant in (*definition.knowledge_sources, *definition.optional_knowledge_sources)
    }
    assert declared, "the registry must declare at least one knowledge source"
    assert declared <= set(KNOWLEDGE_SOURCE_CONNECTORS), (
        f"unresolvable registry source id(s): {sorted(declared - set(KNOWLEDGE_SOURCE_CONNECTORS))}"
    )


def test_the_analyst_is_scoped_to_a_searchable_corpus() -> None:
    """The one Agent that actually searches must resolve to a real corpus.

    NE-ANALYST-VERIFIED-LANE added `meridian-governed-local-t3` to this Agent's
    declared sources so the `read.kg.*` tools can read the Tier-3 release. That
    source serves NO chunk-store corpus, and this assertion is what proves the
    addition did not widen retrieval by a single connector: the filter is still
    the M365 corpus alone, exactly as before the grant.
    """
    from pathlib import Path

    from ne.api.agent_manifest import load_agent_manifest

    registry = Path(__file__).parents[3] / "deploy/agents/registry.toml"
    analyst = next(
        definition
        for definition in load_agent_manifest(registry).manifest.agents
        if definition.label == "Meridian Analyst"
    )
    assert "read.knowledge.search" in analyst.tool_grants
    assert declared_source_ids(analyst) == frozenset(
        {"meridian-servicing", "meridian-governed-local-t3"}
    )
    assert KnowledgeScopeResolver().filters(declared_source_ids(analyst)) == {
        CONNECTOR_FILTER_KEY: [_M365]
    }
