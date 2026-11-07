# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-BRAIN-T2-WIRING — the composition-root Tier-2 graph wiring proof (F1/F2/F3).

Proves at the ne-api composition root what the 2026-07-09 integration audit
specified: the graph tier composes a ``GraphRetriever`` over the reranked T1
pipeline (F3 no-drop ordering), injects the IDENTICAL ``PrincipalExpander`` into
``EntityGraph`` that the chunk-side ``PrincipalAclFilter`` uses (F2 C7 parity), and
stamps ``BrainGraphSettings`` into the frozen ``config_hash`` ONLY when enabled so
the default-off hash stays byte-identical (F1, the 085c conditional-stamp). The
governed spine is REAL; only the network edges + the graph store are faked (the
trimming-faithful ``InMemoryGraphStore``). No pytest-asyncio (repo policy): async
is driven via the conftest ``run`` helper.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import sqlalchemy as sa
from pydantic import SecretStr
from starlette.routing import Mount

from ne.api import create_app
from ne.api.platform import (
    AppSeams,
    Platform,
    _default_graph_store,
    _RetainedM365GraphSourceAuthorityProvider,
    build_platform,
)
from ne.api.settings import ApiSettings
from ne.api.wire import RetrievalResultView, retrieval_result_view
from ne.audit.config import AuditSettings
from ne.brain.lineage import RetainedSourceAuthority
from ne.brain.pipeline import RetrievalPipeline
from ne.brain.trimming import PrincipalAclFilter
from ne.brain_graph.age.store import AgeGraphStore
from ne.brain_graph.config import BrainGraphSettings, GraphEntityMapping, GraphSourceMapping
from ne.brain_graph.errors import GraphStoreError
from ne.brain_graph.lightrag.retrieval import GraphRetriever
from ne.brain_graph.testing.fixtures import make_edge, make_node
from ne.brain_graph.testing.memory import InMemoryGraphStore
from ne.connect.identity import source_acl_hash, source_ref_for, source_snapshot_hash
from ne.connect.m365.config import M365ConnectorSettings
from ne.contracts import reset_config_hash_for_testing
from ne.contracts.integration import Record
from ne.contracts.plugins import PluginRegistry
from ne.contracts.source_authority import (
    CurrentSourceAuthorityReceipt,
    M365SourcePoint,
    SourceAuthorityDecision,
    SourceAuthorityResolution,
    SourceAuthorityState,
    SourceContentArtifactIdentity,
    SourceContractIdentity,
    SourceIdentity,
)
from ne.contracts.types import (
    Citation,
    GraphEntityRef,
    GraphHop,
    GraphPath,
    Lane,
    Principal,
    RetrievalResult,
)
from ne.gateway.config import GatewaySettings, ModelDef
from ne.identity.config import IdentitySettings, OidcConfig
from ne.permissions.config import PermissionsSettings
from ne.permissions.models import PrincipalKind, SyncedPrincipal
from ne.runtime.config import RuntimeSettings

from .conftest import (
    ISSUER,
    FakeTokenVerifier,
    InMemoryFilteredChunkStore,
    PassthroughReranker,
    RegoFaithfulPdpFactory,
    ScriptedGateway,
    StaticEmbedder,
    StaticSparseEncoder,
    chunk,
    get_state,
    run,
)


def _api_settings(**overrides: object) -> ApiSettings:
    base: dict[str, object] = {
        "qdrant_url": "http://qdrant.test",
        "qdrant_collection": "t",
        "tei_url": "http://tei.test",
    }
    base.update(overrides)
    return ApiSettings(**base)  # type: ignore[arg-type]


def _seams(
    tmp_path: Path,
    *,
    key: str,
    chunks: tuple[Any, ...] = (),
    brain_graph_settings: BrainGraphSettings | None = None,
    graph_store: Any | None = None,
) -> tuple[AppSeams, FakeTokenVerifier, str, InMemoryFilteredChunkStore]:
    """Seams over tmp SQLite with a passthrough rerank edge (network-free search).

    A stable ``key`` reuses the same DB URLs across builds so a config_hash
    comparison isolates the ONLY difference — the graph tier settings.
    """
    registry = PluginRegistry()
    verifier = FakeTokenVerifier()
    registry.register("token_verifier", "fake", lambda oidc: verifier)
    registry.register("policy_decision_point", "opa", RegoFaithfulPdpFactory)

    runtime_url = f"sqlite:///{tmp_path}/runtime-{key}.db"
    audit_url = f"sqlite:///{tmp_path}/audit-{key}.db"
    perms_url = f"sqlite:///{tmp_path}/perms-{key}.db"

    store = InMemoryFilteredChunkStore(chunks)
    seams = AppSeams(
        identity_settings=IdentitySettings(
            verifier="fake",
            oidc=OidcConfig(issuer=ISSUER, audience="ne-api"),
            principal_claims=("email", "preferred_username", "name", "department"),
        ),
        runtime_settings=RuntimeSettings(session_database_url=runtime_url),
        audit_settings=AuditSettings(database_url=audit_url),
        permissions_settings=PermissionsSettings(database_url=perms_url),
        gateway_settings=GatewaySettings(
            models=[
                ModelDef(
                    name="stub",
                    litellm_model="openai/stub",
                    provider="stub",
                    lane=Lane.FRONTIER_API,
                    context_window=32768,
                )
            ]
        ),
        gateway=ScriptedGateway(),
        embedder=StaticEmbedder(),
        sparse_encoder=StaticSparseEncoder(),
        chunk_store=store,
        reranker=PassthroughReranker(),
        plugin_registry=registry,
        brain_graph_settings=brain_graph_settings,
        graph_store=graph_store,
    )
    return seams, verifier, perms_url, store


@dataclass
class PlatformHarness:
    platform: Platform
    perms_url: str
    chunk_store: InMemoryFilteredChunkStore

    def seed_principal(self, principal_id: str, groups: tuple[str, ...] = ()) -> None:
        self.platform.perm_store.upsert_principals(
            [SyncedPrincipal(principal_id=principal_id, kind=PrincipalKind.USER)],
            source="test",
            now=datetime.now(UTC),
        )
        if groups:
            self.platform.perm_store.set_member_groups(
                principal_id, list(groups), source="test", now=datetime.now(UTC)
            )

    def seed_group_edge(self, member: str, groups: tuple[str, ...]) -> None:
        self.platform.perm_store.set_member_groups(
            member, list(groups), source="test", now=datetime.now(UTC)
        )


MakePlatform = Callable[..., PlatformHarness]


@pytest.fixture
def make_platform(tmp_path: Path) -> Iterator[MakePlatform]:
    engines: list[sa.Engine] = []

    def _make(
        *,
        brain_graph_settings: BrainGraphSettings | None = None,
        graph_store: Any | None = None,
        chunks: tuple[Any, ...] = (),
        key: str | None = None,
    ) -> PlatformHarness:
        reset_config_hash_for_testing()
        seams, _verifier, perms_url, store = _seams(
            tmp_path,
            key=key or uuid.uuid4().hex,
            chunks=chunks,
            brain_graph_settings=brain_graph_settings,
            graph_store=graph_store,
        )
        platform = build_platform(_api_settings(), seams)
        engines.extend(platform.engines)
        return PlatformHarness(platform=platform, perms_url=perms_url, chunk_store=store)

    yield _make
    for engine in engines:
        engine.dispose()


# --- C7 topology (mirrors test_c7_graph_provable_trimming.py:50-99) ---------


def _seed_c7_graph(store: InMemoryGraphStore) -> None:
    """Seed the invisible-intermediate topology, anchored at a via source_ref.

    ``a`` (g-eng, source_ref='ref-c1') anchors the T1 citation for chunk c1; the
    ONLY route a..b crosses the invisible node ``x`` (u-other), while a→t is a
    plainly-visible 1-hop for u-ada.
    """

    async def setup() -> None:
        await store.upsert_nodes(
            [
                make_node(
                    "a", "Account", ("g-eng",), source_ref="ref-c1", source_status="no_source"
                ),
                make_node(
                    "t", "Account", ("u-ada",), source_ref="ref-t", source_status="no_source"
                ),
                make_node(
                    "x", "Account", ("u-other",), source_ref="ref-x", source_status="no_source"
                ),
                make_node(
                    "b", "Account", ("u-ada",), source_ref="ref-b", source_status="no_source"
                ),
            ]
        )
        await store.upsert_edges(
            [
                make_edge("ABOUT", "a", "x", ("u-ada",), key="a_x")
                | {"source_status": "no_source"},
                make_edge("ABOUT", "x", "b", ("u-ada",), key="x_b")
                | {"source_status": "no_source"},
                make_edge("ABOUT", "a", "t", ("u-ada",), key="a_t")
                | {"source_status": "no_source"},
            ]
        )

    run(setup())


def _seed_ada(h: PlatformHarness) -> None:
    """u-ada → g-child ⊂ g-eng (the transitive-group topology)."""
    h.seed_principal("u-ada", groups=("g-child",))
    h.seed_group_edge("g-child", ("g-eng",))


def _source_free_chunk(chunk_id: str, *, acl: list[str], text: str) -> Any:
    value = chunk(chunk_id, acl=acl, text=text)
    return value.model_copy(
        update={"metadata": {**dict(value.metadata), "source_status": "no_source"}}
    )


# --- F1: default-off byte-identity + enabled-differs + ER pin ---------------


def test_graph_default_off_config_hash_byte_identical(make_platform: MakePlatform) -> None:
    default = make_platform(key="ident")
    disabled = make_platform(brain_graph_settings=BrainGraphSettings(enabled=False), key="ident")
    assert disabled.platform.config_hash == default.platform.config_hash


def test_graph_enabled_changes_config_hash(make_platform: MakePlatform) -> None:
    from ne.contracts import get_config_hash

    default = make_platform(key="ident")
    enabled = make_platform(
        brain_graph_settings=BrainGraphSettings(enabled=True),
        graph_store=InMemoryGraphStore(),
        key="ident",
    )
    assert enabled.platform.config_hash != default.platform.config_hash
    # The last build's frozen global equals the returned hash (freeze-once discipline).
    assert get_config_hash() == enabled.platform.config_hash


def test_production_graph_store_factory_selects_persistent_age() -> None:
    store = _default_graph_store(
        BrainGraphSettings(
            enabled=True,
            database_url="postgresql://graph.invalid/neutron",
            graph_name="ne_acceptance",
        )
    )
    assert type(store) is AgeGraphStore


def test_graph_enabled_er_threshold_pins_config_hash(make_platform: MakePlatform) -> None:
    # Two enabled builds differing ONLY in er_proposal_threshold ⇒ different hashes:
    # the hash a governed SAME_AS merge stamps pins the ER config that decided it (F1).
    a = make_platform(
        brain_graph_settings=BrainGraphSettings(enabled=True, er_proposal_threshold=0.95),
        graph_store=InMemoryGraphStore(),
        key="ident",
    )
    b = make_platform(
        brain_graph_settings=BrainGraphSettings(enabled=True, er_proposal_threshold=0.9),
        graph_store=InMemoryGraphStore(),
        key="ident",
    )
    assert a.platform.config_hash != b.platform.config_hash


# --- composition spine ------------------------------------------------------


def test_default_composition_has_no_graph_retriever(make_platform: MakePlatform) -> None:
    h = make_platform()
    assert isinstance(h.platform.retriever, RetrievalPipeline)
    assert not isinstance(h.platform.retriever, GraphRetriever)


def test_graph_enabled_composes_graph_retriever_over_reranked_pipeline(
    make_platform: MakePlatform,
) -> None:
    h = make_platform(
        brain_graph_settings=BrainGraphSettings(enabled=True),
        graph_store=InMemoryGraphStore(),
    )
    assert isinstance(h.platform.retriever, GraphRetriever)
    # Graph ABOVE rerank: _t1 is the RetrievalPipeline wrapping the ONE HybridRetriever.
    assert isinstance(h.platform.retriever._t1, RetrievalPipeline)  # type: ignore[attr-defined]


def test_configured_m365_graph_host_uses_the_platform_owned_store_lineage_and_sink(
    make_platform: MakePlatform,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("NE_CONNECT_SYNC_SOURCES", '["m365"]')
    monkeypatch.setenv("NE_CONNECT_SYNC_STATE_PATH", str(tmp_path / "connector-state.db"))
    monkeypatch.setenv("NE_CONNECT_SYNC_QUARANTINE_PATH", str(tmp_path / "connector-quarantine.db"))
    graph_store = InMemoryGraphStore()
    h = make_platform(
        brain_graph_settings=BrainGraphSettings(
            enabled=True,
            database_url=f"sqlite:///{tmp_path}/graph.db",
            ledger_url=f"sqlite:///{tmp_path}/graph-ledger.db",
            sources=(
                GraphSourceMapping(
                    source_system="m365-sharepoint",
                    entities=(
                        GraphEntityMapping(
                            source_id_prefix="drive-a",
                            entity_class="Document",
                            fields={
                                "id": "id",
                                "title": "name",
                                "doc_type": "graph_doc_type",
                            },
                        ),
                    ),
                ),
            ),
        ),
        graph_store=graph_store,
    )

    host = h.platform.m365_graph_sync_host
    assert host is not None
    # The graph-host block must retain the embedder composed for retrieval. The
    # worker consumes this exact public seam to construct M365 source authority;
    # replacing it with ``None`` silently removes the governed M365 cron surface.
    assert isinstance(h.platform.embedder, StaticEmbedder)
    authority_provider = cast(Any, host._source_authority_provider)
    assert host._store is graph_store
    assert host._evidence_recorder is h.platform.document_lineage
    assert authority_provider._lineage is h.platform.document_lineage
    assert authority_provider._reader is h.platform.source_authority_reader
    assert host._audit_sink is h.platform.sink
    mapping = host.mapping_for(
        M365ConnectorSettings(
            tenant_id="tenant",
            client_id="client",
            auth_method="secret",
            client_secret=SecretStr("test-only"),
            drive_ids=("drive-a",),
            owner="owner@example.test",
        )
    )
    assert mapping.entities[0].fields == {
        "id": "id",
        "title": "name",
        "doc_type": "graph_doc_type",
    }
    site_settings = M365ConnectorSettings(
        tenant_id="tenant",
        client_id="client",
        auth_method="secret",
        client_secret=SecretStr("test-only"),
        site_ids=("site-a",),
        owner="owner@example.test",
    )
    assert host.mapping_for(site_settings) is mapping
    with pytest.raises(ValueError, match="exact resolved drive_id prefix coverage"):
        host.mapping_for(site_settings, resolved_drive_ids=("other-drive",))
    with pytest.raises(ValueError, match="exactly one non-empty graph mapping"):
        host.mapping_for(
            M365ConnectorSettings(
                tenant_id="tenant",
                client_id="client",
                auth_method="secret",
                client_secret=SecretStr("test-only"),
                drive_ids=("drive-a",),
                owner="owner@example.test",
                source_name="other-m365-source",
            )
        )


def test_m365_graph_authority_provider_requires_exact_current_t1_admission() -> None:
    record = Record(
        record_id="item-1",
        source_id="drive-a:item-1",
        payload={"id": "item-1", "name": "Policy.docx", "etag": "etag-1"},
    )
    acl = ("g-risk",)
    acl_hash = source_acl_hash(acl)
    snapshot_hash = source_snapshot_hash("m365-sharepoint", record)
    identity = SourceIdentity(
        deployment_id="deployment-1",
        source_kind="m365-sharepoint",
        connector_instance_id="connector-1",
        point=M365SourcePoint(
            graph_tenant_id="tenant-1",
            site_id="",
            drive_id="drive-a",
            item_id="item-1",
        ),
        generation=1,
    )
    contract = SourceContractIdentity(
        source_kind="m365-sharepoint",
        descriptor_schema_version=1,
        descriptor_hash="c" * 64,
    )
    artifact = SourceContentArtifactIdentity(
        source_identity=identity,
        source_snapshot_hash=snapshot_hash,
        content_snapshot_hash="d" * 64,
        acl_snapshot_hash=acl_hash,
        source_contract=contract,
        artifact_schema_version=1,
    )
    receipt = CurrentSourceAuthorityReceipt(
        receipt_id="receipt-1",
        source_identity=identity,
        artifact_identity=artifact,
        source_revision="etag-1",
        source_contract=contract,
        contract_epoch=1,
        acl_snapshot_hash=acl_hash,
        state=SourceAuthorityState.PRESENT,
        admitted_at=datetime(2026, 8, 22, 12, tzinfo=UTC),
        authority_as_of=datetime(2026, 8, 22, 11, tzinfo=UTC),
        audit_event_id="audit-1",
        lineage_id="lineage-1",
        freshness_sla_seconds=Decimal(3600),
    )
    retained = RetainedSourceAuthority(
        document_id="document-1",
        version_seq=1,
        receipt_id=receipt.receipt_id,
        receipt_hash=receipt.receipt_hash,
        source_identity_hash=identity.identity_hash,
        artifact_id=artifact.artifact_id,
        contract_epoch=1,
        state=SourceAuthorityState.PRESENT.value,
    )

    class _Lineage:
        def __init__(self) -> None:
            self.value: RetainedSourceAuthority | None = retained
            self.calls: list[dict[str, str]] = []

        def retained_source_authority_for_snapshot(
            self, **kwargs: str
        ) -> RetainedSourceAuthority | None:
            self.calls.append(kwargs)
            return self.value

    class _Reader:
        decision = SourceAuthorityDecision.AUTHORIZED

        def load_receipt(self, receipt_id: str) -> CurrentSourceAuthorityReceipt | None:
            return receipt if receipt_id == receipt.receipt_id else None

        async def resolve(self, *_args: Any, **_kwargs: Any) -> SourceAuthorityResolution:
            if self.decision is SourceAuthorityDecision.AUTHORIZED:
                return SourceAuthorityResolution(
                    decision=self.decision,
                    current_receipt=receipt,
                )
            return SourceAuthorityResolution(
                decision=self.decision,
                current_receipt=receipt,
                safe_reason="the stamp is stale",
            )

    lineage = _Lineage()
    reader = _Reader()
    provider = _RetainedM365GraphSourceAuthorityProvider(cast(Any, lineage), reader)

    stamps = run(provider.stamps_for(record, acl=acl))
    assert len(stamps) == 1
    assert stamps[0].authority_receipt_id == receipt.receipt_id
    assert lineage.calls[-1] == {
        "source_system": "m365-sharepoint",
        "source_ref": source_ref_for(record),
        "source_snapshot_hash": snapshot_hash,
        "source_acl_hash": acl_hash,
    }

    reader.decision = SourceAuthorityDecision.PERMANENT_WITHHELD
    assert run(provider.stamps_for(record, acl=acl)) == ()

    reader.decision = SourceAuthorityDecision.AUTHORIZED
    lineage.value = RetainedSourceAuthority(
        **{**retained.__dict__, "source_identity_hash": "f" * 64}
    )
    assert run(provider.stamps_for(record, acl=acl)) == ()

    lineage.value = retained
    assert run(provider.stamps_for(record, acl=("g-other",))) == ()
    lineage.value = RetainedSourceAuthority(
        **{**retained.__dict__, "contract_epoch": retained.contract_epoch + 1}
    )
    assert run(provider.stamps_for(record, acl=acl)) == ()

    lineage.value = retained
    revised = record.model_copy(update={"payload": {**dict(record.payload), "etag": "etag-2"}})
    assert run(provider.stamps_for(revised, acl=acl)) == ()

    lineage.value = None
    assert run(provider.stamps_for(record, acl=acl)) == ()


def test_configured_m365_graph_host_fails_closed_without_exact_source_mapping(
    make_platform: MakePlatform,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("NE_CONNECT_SYNC_SOURCES", '["m365"]')
    monkeypatch.setenv("NE_CONNECT_SYNC_STATE_PATH", str(tmp_path / "connector-state.db"))
    monkeypatch.setenv("NE_CONNECT_SYNC_QUARANTINE_PATH", str(tmp_path / "quarantine.db"))

    with pytest.raises(ValueError, match="requires at least one NE_BRAIN_GRAPH_SOURCES mapping"):
        make_platform(
            brain_graph_settings=BrainGraphSettings(
                enabled=True,
                database_url=f"sqlite:///{tmp_path}/graph.db",
            ),
            graph_store=InMemoryGraphStore(),
        )


def test_configured_m365_graph_host_requires_confirmed_edge_writes(
    make_platform: MakePlatform,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("NE_CONNECT_SYNC_SOURCES", '["m365"]')
    monkeypatch.setenv("NE_CONNECT_SYNC_STATE_PATH", str(tmp_path / "connector-state.db"))
    monkeypatch.setenv("NE_CONNECT_SYNC_QUARANTINE_PATH", str(tmp_path / "quarantine.db"))
    monkeypatch.delattr(InMemoryGraphStore, "upsert_edges_confirmed")

    with pytest.raises(ValueError, match="upsert_edges_confirmed"):
        make_platform(
            brain_graph_settings=BrainGraphSettings(
                enabled=True,
                database_url=f"sqlite:///{tmp_path}/graph.db",
                sources=(
                    GraphSourceMapping(
                        source_system="m365-sharepoint",
                        entities=(
                            GraphEntityMapping(
                                source_id_prefix="drive-a",
                                entity_class="Document",
                                fields={
                                    "id": "id",
                                    "title": "name",
                                    "doc_type": "graph_doc_type",
                                },
                            ),
                        ),
                    ),
                ),
            ),
            graph_store=InMemoryGraphStore(),
        )


def test_graph_expander_identity_shared_with_chunk_acl_filter(
    make_platform: MakePlatform,
) -> None:
    # F2 GUARDIAN: the EntityGraph expander and the chunk-side PrincipalAclFilter
    # expander are the SAME PrincipalExpander instance (one label set per principal).
    h = make_platform(
        brain_graph_settings=BrainGraphSettings(enabled=True),
        graph_store=InMemoryGraphStore(),
    )
    retriever: Any = h.platform.retriever
    entity_graph = retriever._graph
    assert entity_graph._group_expander is h.platform.expander
    assert entity_graph._source_authority is h.platform.source_authority_reader
    acl = retriever._t1._retriever._acl_filters
    assert isinstance(acl, PrincipalAclFilter)
    assert acl._expander is h.platform.expander


def test_c7_graph_provable_trimming_through_composed_platform(
    make_platform: MakePlatform,
) -> None:
    # F2 provable trimming with the REAL PrincipalExpander the platform built.
    graph_store = InMemoryGraphStore()
    h = make_platform(
        brain_graph_settings=BrainGraphSettings(enabled=True),
        graph_store=graph_store,
        chunks=(_source_free_chunk("c1", acl=["g-eng"], text="alpha"),),
    )
    _seed_ada(h)
    _seed_c7_graph(graph_store)

    result = run(h.platform.retriever.search("alpha", principal=Principal(id="u-ada"), top_k=10))
    # graph_paths non-empty and contain the a→t hop (plainly visible 1-hop).
    assert result.graph_paths, "expected at least one visible graph path"
    named = {
        ref.entity_key
        for path in result.graph_paths
        for hop in path.hops
        for ref in (hop.subject, hop.obj)
    }
    assert "t" in named  # the a→t hop survives
    # A principal cannot reach a graph node/path they lack ACLs for: never b or x.
    assert "b" not in named
    assert "x" not in named

    # A groupless principal cannot even see chunk c1 → no anchor → no graph paths.
    h.seed_principal("u-lonely")
    groupless = run(
        h.platform.retriever.search("alpha", principal=Principal(id="u-lonely"), top_k=10)
    )
    assert groupless.graph_paths == ()


def test_graph_disabled_degrade_is_logged(
    make_platform: MakePlatform, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="ne.api.platform"):
        make_platform()
    assert any(
        "graph tier (T2) disabled" in r.message
        for r in caplog.records
        if r.name == "ne.api.platform"
    )


def test_lightrag_backend_refused_at_boot(make_platform: MakePlatform) -> None:
    with pytest.raises(ValueError, match="NATIVE"):
        make_platform(
            brain_graph_settings=BrainGraphSettings(
                enabled=True, graph_expansion_backend="lightrag"
            ),
            graph_store=InMemoryGraphStore(),
        )


# --- F3: pure-unit view mapping ---------------------------------------------


def test_retrieval_result_view_maps_graph_paths_field_by_field() -> None:
    hop = GraphHop(
        subject=GraphEntityRef(
            entity_class="Person", entity_key="p1", display="Alice", source_ref="Person:P1"
        ),
        edge_kind="WORKS_AT",
        obj=GraphEntityRef(
            entity_class="Account", entity_key="a1", display="Acme", source_ref="Account:A1"
        ),
        source_system="salesforce",
        source_ref="Contact:C1",
    )
    result = RetrievalResult(
        chunks=(),
        citations=(Citation(chunk_id="c1", document_id="d1", source_system="w", source_ref="r"),),
        graph_paths=(GraphPath(hops=(hop,)),),
    )
    view = retrieval_result_view(result)
    assert isinstance(view, RetrievalResultView)
    assert len(view.graph_paths) == 1
    mapped = view.graph_paths[0].hops[0]
    assert mapped.subject.entity_class == "Person"
    assert mapped.subject.entity_key == "p1"
    assert mapped.subject.display == "Alice"
    assert mapped.subject.assurance == "source"
    assert mapped.edge_kind == "WORKS_AT"
    assert mapped.obj.entity_key == "a1"
    assert mapped.obj.assurance == "source"
    assert mapped.source_system == "salesforce"
    assert mapped.source_ref == "Contact:C1"
    assert mapped.assurance == "source"
    assert view.provisional_source_count == 0

    # ADR-0015 (NE-T2X-5-ENDPOINT-EVIDENCE-REF): three DISTINCT source_refs —
    # subject's own, obj's own, and the edge's — each land on their OWN wire
    # field. A mapping that copies the edge's ref onto an endpoint (or vice
    # versa) must fail this assertion.
    assert mapped.subject.source_ref == "Person:P1"
    assert mapped.obj.source_ref == "Account:A1"
    assert mapped.source_ref == "Contact:C1"
    assert len({mapped.subject.source_ref, mapped.obj.source_ref, mapped.source_ref}) == 3

    empty = retrieval_result_view(RetrievalResult(chunks=(), citations=()))
    assert empty.graph_paths == []
    assert empty.provisional_source_count == 0


# --- F3 (HTTP) + 502 mapping: create_app built directly with graph seams ----


class _ExplodingGraphStore:
    """A graph read store whose ``cypher`` raises ``GraphStoreError`` (with a DSN in
    the message) — proving the 502 detail is the CLASS NAME only, never the DSN."""

    async def cypher(
        self, query: str, params: Mapping[str, Any] | None = None
    ) -> Sequence[Mapping[str, Any]]:
        raise GraphStoreError("graph backend down at postgresql://secret@db.internal/ne")


@dataclass
class GraphAppHarness:
    app: Any
    verifier: FakeTokenVerifier

    def bearer(self, subject: str, *, groups: Sequence[str]) -> dict[str, str]:
        token = self.verifier.mint(subject, groups=groups, department="engineering")
        return {"Authorization": f"Bearer {token}"}

    def client(self) -> httpx.AsyncClient:
        transport = httpx.ASGITransport(app=self.app)
        return httpx.AsyncClient(transport=transport, base_url="http://ne.test")


MakeGraphApp = Callable[..., GraphAppHarness]


@pytest.fixture
def make_graph_app(tmp_path: Path) -> Iterator[MakeGraphApp]:
    apps: list[Any] = []

    def _make(*, graph_store: Any, chunks: tuple[Any, ...] = ()) -> GraphAppHarness:
        reset_config_hash_for_testing()
        seams, verifier, _perms_url, _store = _seams(
            tmp_path,
            key=uuid.uuid4().hex,
            chunks=chunks,
            brain_graph_settings=BrainGraphSettings(enabled=True),
            graph_store=graph_store,
        )
        app = create_app(_api_settings(), seams=seams)
        apps.append(app)
        # Seed u-ada → g-child ⊂ g-eng directly on the composed perm store.
        state = get_state(app)
        state.perm_store.upsert_principals(
            [SyncedPrincipal(principal_id="u-ada", kind=PrincipalKind.USER)],
            source="test",
            now=datetime.now(UTC),
        )
        state.perm_store.set_member_groups(
            "u-ada", ["g-child"], source="test", now=datetime.now(UTC)
        )
        state.perm_store.set_member_groups(
            "g-child", ["g-eng"], source="test", now=datetime.now(UTC)
        )
        return GraphAppHarness(app=app, verifier=verifier)

    yield _make
    for app in apps:
        for engine in get_state(app).engines:
            engine.dispose()


def _assert_no_acl_key(node: Any) -> None:
    if isinstance(node, dict):
        assert "acl" not in node, f"acl key leaked in {node!r}"
        for v in node.values():
            _assert_no_acl_key(v)
    elif isinstance(node, list):
        for v in node:
            _assert_no_acl_key(v)


def test_enforcement_routers_are_mounted_on_the_authenticated_api(
    make_graph_app: MakeGraphApp,
) -> None:
    h = make_graph_app(graph_store=InMemoryGraphStore())
    state = get_state(h.app)
    assert state.source_authority_reader is not None
    assert state.source_authority_evaluator is not None
    assert state.source_authority_evaluator._authority is state.source_authority_reader
    assert cast(Any, state.current_retriever)._evaluator is state.source_authority_evaluator
    assert state.source_guard is not None
    assert not hasattr(state, "source_authority_writer")
    api_mount = next(
        route for route in h.app.routes if isinstance(route, Mount) and route.path == "/api"
    )

    paths = set(cast(Any, api_mount.app)._app.openapi()["paths"])
    assert {
        "/v1/answer-ledger",
        "/v1/brain/search",
        "/v1/documents",
        "/v1/graph/entities",
        "/v1/ontology/model",
        "/v1/steward/proposals",
        "/v1/workflows",
        "/v1/approvals/pending",
    } <= paths


def test_brain_search_surfaces_trimmed_graph_paths_on_wire(make_graph_app: MakeGraphApp) -> None:
    graph_store = InMemoryGraphStore()
    _seed_c7_graph(graph_store)
    h = make_graph_app(
        graph_store=graph_store,
        chunks=(_source_free_chunk("c1", acl=["g-eng"], text="a"),),
    )

    async def _t() -> None:
        async with h.client() as client:
            r = await client.post(
                "/api/v1/brain/search",
                json={"query": "a", "top_k": 10},
                headers=h.bearer("u-ada", groups=["g-child"]),
            )
            assert r.status_code == 200
            body = r.json()
            assert set(body) == {"chunks", "citations", "graph_paths", "provisional_source_count"}
            assert body["graph_paths"], "expected a visible graph path on the wire"
            for path in body["graph_paths"]:
                assert set(path) == {"hops"}
                for hop in path["hops"]:
                    assert set(hop) == {
                        "subject",
                        "edge_kind",
                        "obj",
                        "source_system",
                        "source_ref",
                        "assurance",
                    }
                    assert set(hop["subject"]) == {
                        "entity_class",
                        "entity_key",
                        "display",
                        "assurance",
                        "source_ref",
                    }
                    assert set(hop["obj"]) == {
                        "entity_class",
                        "entity_key",
                        "display",
                        "assurance",
                        "source_ref",
                    }
                    # _seed_c7_graph uses make_node/make_edge (source_system="fixture"),
                    # so every element reads assurance="source" (NE-T2X-4 F2: nothing
                    # backbone-derived can ever surface as extracted).
                    assert hop["subject"]["assurance"] == "source"
                    assert hop["obj"]["assurance"] == "source"
                    assert hop["assurance"] == "source"
            # A path with zero provisional (extracted/unknown-label) elements
            # contributes zero to the count.
            assert body["provisional_source_count"] == 0
            # The recursive no-acl-key proof now walks graph paths too.
            _assert_no_acl_key(body)
            # Trimmed: never a hop naming the invisible node x or the unreachable b.
            named = {
                ref["entity_key"]
                for path in body["graph_paths"]
                for hop in path["hops"]
                for ref in (hop["subject"], hop["obj"])
            }
            assert "x" not in named and "b" not in named

    run(_t())


def test_graph_store_failure_maps_to_502_class_name_only(make_graph_app: MakeGraphApp) -> None:
    h = make_graph_app(
        graph_store=_ExplodingGraphStore(), chunks=(chunk("c1", acl=["g-eng"], text="a"),)
    )

    async def _t() -> None:
        async with h.client() as client:
            r = await client.post(
                "/api/v1/brain/search",
                json={"query": "a", "top_k": 10},
                headers=h.bearer("u-ada", groups=["g-child"]),
            )
            assert r.status_code == 502
            assert r.json() == {"detail": "GraphStoreError"}
            assert "postgresql" not in r.text  # no DSN oracle

    run(_t())
