# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-087 (O-4) — the composition-root ReBAC wiring proof (FIX-1 + FIX-2).

Proves at the ne-api composition root what a prior pass MISSED: enabling ReBAC
actually ACTIVATES the resolver end-to-end (the C7 union runs on both the search
and retrieve paths), the config_hash stays byte-identical when disabled (085c
conditional stamp), and BOTH process roots close the retained OpenFGA client on
shutdown (the unclosed-session leak). The governed spine is REAL; only the OpenFGA
wire is the in-memory fake injected through ``AppSeams.rebac_client``.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import sqlalchemy as sa

from ne.api.platform import AppSeams, Platform, build_platform
from ne.api.settings import ApiSettings
from ne.audit.config import AuditSettings
from ne.brain.trimming import PrincipalAclFilter
from ne.contracts import reset_config_hash_for_testing
from ne.contracts.plugins import PluginRegistry
from ne.contracts.types import Lane, Principal
from ne.gateway.config import GatewaySettings, ModelDef
from ne.govern.opa import OpaClient
from ne.govern.pdp import GovernedPdp
from ne.identity.config import IdentitySettings, OidcConfig
from ne.permissions.config import PermissionsSettings
from ne.permissions.models import PrincipalKind, SyncedPrincipal
from ne.permissions.store import PrincipalStore
from ne.permissions_rebac.config import RebacSettings
from ne.permissions_rebac.share import ShareService
from ne.permissions_rebac.testing.fakes import (
    FakeKnownPrincipals,
    FakeReadProbe,
    InMemoryOpenFga,
)
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
    run,
)

_DECISION_PATH = "/v1/data/ne/govern/decision"


def _enabled_settings() -> RebacSettings:
    return RebacSettings(enabled=True, store_id="s", authorization_model_id="m")


def _allow_share_pdp(sink: Any) -> GovernedPdp:
    """A REAL GovernedPdp (audits ``pdp.decide``) over a transport that ALLOWs
    ``rebac.share`` — the rego-faithful transport denies unclassified actions."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == _DECISION_PATH
        return httpx.Response(
            200,
            json={
                "result": {
                    "effect": "allow",
                    "tier": 0,
                    "reason": "rebac.share allowed (test)",
                    "obligations": {},
                    "policy_version": "1.0.0",
                }
            },
        )

    client = httpx.AsyncClient(base_url="http://opa.test", transport=httpx.MockTransport(handler))
    return GovernedPdp(OpaClient("http://opa.test", client=client), sink)


def _seed_known(store: PrincipalStore, *pids: str) -> None:
    store.upsert_principals(
        [SyncedPrincipal(principal_id=p, kind=PrincipalKind.USER) for p in pids],
        source="test",
        now=datetime.now(UTC),
    )


@dataclass
class PlatformHarness:
    platform: Platform
    audit_url: str
    chunk_store: InMemoryFilteredChunkStore

    def audit_actions(self) -> list[str]:
        engine = sa.create_engine(self.audit_url)
        try:
            with engine.connect() as conn:
                rows = conn.execute(sa.text("SELECT action FROM ne_audit_log")).fetchall()
        finally:
            engine.dispose()
        return [r[0] for r in rows]


AppBuilder = Callable[..., PlatformHarness]


@pytest.fixture
def make_platform(tmp_path: Path) -> Iterator[AppBuilder]:
    engines: list[sa.Engine] = []

    def _make(
        *,
        rebac_settings: RebacSettings | None = None,
        rebac_client: Any | None = None,
        chunks: tuple[Any, ...] = (),
        url_key: str | None = None,
    ) -> PlatformHarness:
        reset_config_hash_for_testing()
        registry = PluginRegistry()
        verifier = FakeTokenVerifier()
        registry.register("token_verifier", "fake", lambda oidc: verifier)
        registry.register("policy_decision_point", "opa", RegoFaithfulPdpFactory)

        # A stable url_key reuses the same DB URLs across builds so a config_hash
        # comparison isolates the ONLY difference — whether ReBAC is enabled.
        key = url_key or uuid.uuid4().hex
        runtime_url = f"sqlite:///{tmp_path}/runtime-{key}.db"
        audit_url = f"sqlite:///{tmp_path}/audit-{key}.db"
        perms_url = f"sqlite:///{tmp_path}/perms-{key}.db"

        store = InMemoryFilteredChunkStore(chunks)
        seams = AppSeams(
            identity_settings=IdentitySettings(
                verifier="fake",
                oidc=OidcConfig(issuer=ISSUER, audience="ne-api"),
                principal_claims=("email", "department"),
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
            rebac_settings=rebac_settings,
            rebac_client=rebac_client,
        )
        platform = build_platform(
            ApiSettings(
                qdrant_url="http://qdrant.test",
                qdrant_collection="t",
                tei_url="http://tei.test",
            ),
            seams,
        )
        engines.extend(platform.engines)
        return PlatformHarness(platform=platform, audit_url=audit_url, chunk_store=store)

    yield _make
    for engine in engines:
        engine.dispose()


# --- (1) default-off: no client, plain source-only filter -------------------


def test_default_off_no_client_and_source_only_filter(make_platform: AppBuilder) -> None:
    h = make_platform()
    assert h.platform.rebac_client is None
    # The retriever is the RetrievalPipeline (rerank wrap); the acl filter lives on
    # the ONE base HybridRetriever it delegates to.
    acl = h.platform.retriever._retriever._acl_filters  # type: ignore[attr-defined]
    assert isinstance(acl, PrincipalAclFilter)
    # No shared_docs resolver → the plain SYNC {"acl": [...]} shape (resolver-absent).
    out = acl(Principal(id="jane"))
    assert out == {"acl": ["jane"]}


# --- (2) default-off config_hash byte-identity ------------------------------


def test_default_off_config_hash_byte_identical(make_platform: AppBuilder) -> None:
    default = make_platform(url_key="ident")
    disabled = make_platform(rebac_settings=RebacSettings(enabled=False), url_key="ident")
    assert disabled.platform.config_hash == default.platform.config_hash
    enabled = make_platform(
        rebac_settings=_enabled_settings(),
        rebac_client=InMemoryOpenFga(),
        url_key="ident",
    )
    assert enabled.platform.config_hash != default.platform.config_hash


# --- (3) enabled: governed share grants search + retrieve, audited ----------


def _corpus() -> tuple[Any, ...]:
    # jane sees D-src by label; the OWNER's D-shared/D-other are label-hidden from
    # her. A governed share of D-shared must make it visible on BOTH paths.
    return (
        chunk("c-src", acl=["jane"], document_id="D-src"),
        chunk("c-shared", acl=["owner-label"], document_id="D-shared"),
        chunk("c-other", acl=["owner-label"], document_id="D-other"),
    )


def _share(harness: PlatformHarness, fake: InMemoryOpenFga, document_id: str) -> None:
    service = ShareService(
        fake,
        FakeKnownPrincipals({"jane"}),
        FakeReadProbe({("owner", document_id)}),
        _allow_share_pdp(harness.platform.sink),
        harness.platform.sink,
        settings=_enabled_settings(),
    )
    run(service.share(document_id, "user:jane", sharer=Principal(id="owner")))


def test_enabled_share_grants_search_and_retrieve_audited(make_platform: AppBuilder) -> None:
    fake = InMemoryOpenFga()
    h = make_platform(rebac_settings=_enabled_settings(), rebac_client=fake, chunks=_corpus())
    _seed_known(h.platform.perm_store, "jane", "colleague")

    _share(h, fake, "D-shared")

    jane = Principal(id="jane")
    colleague = Principal(id="colleague")

    search = run(h.platform.retriever.search("q", principal=jane, top_k=50))
    seen = {sc.chunk.chunk_id for sc in search.chunks}
    assert "c-src" in seen  # her own label doc
    assert "c-shared" in seen  # the governed share
    assert "c-other" not in seen  # per-object grant, no widening

    # retrieve-by-id: jane sees the shared doc AND her own, not the unshared one.
    ret = run(h.platform.retriever.retrieve(["c-shared", "c-src", "c-other"], principal=jane))
    assert {sc.chunk.chunk_id for sc in ret.chunks} == {"c-shared", "c-src"}

    # A non-shared same-store colleague stays denied on both paths.
    col_search = run(h.platform.retriever.search("q", principal=colleague, top_k=50))
    assert {sc.chunk.chunk_id for sc in col_search.chunks} == set()
    col_ret = run(h.platform.retriever.retrieve(["c-shared"], principal=colleague))
    assert col_ret.chunks == ()

    actions = h.audit_actions()
    assert "rebac.share.created" in actions
    assert "pdp.decide" in actions


# --- (4) enabled but zero shares collapses to source-only -------------------


def test_enabled_no_shares_collapses_to_source_only(make_platform: AppBuilder) -> None:
    fake = InMemoryOpenFga()  # no tuples written
    h = make_platform(rebac_settings=_enabled_settings(), rebac_client=fake, chunks=_corpus())
    _seed_known(h.platform.perm_store, "jane")
    jane = Principal(id="jane")
    search = run(h.platform.retriever.search("q", principal=jane, top_k=50))
    # Identical to disabled: only her label doc, the $any wrapper collapses away.
    assert {sc.chunk.chunk_id for sc in search.chunks} == {"c-src"}


# --- (5) FIX-2: lifespan closes the retained client -------------------------


def test_lifespan_closes_rebac_client(make_app: Any) -> None:
    fake = InMemoryOpenFga()
    h = make_app(rebac_settings=_enabled_settings(), rebac_client=fake)

    async def scenario() -> None:
        assert fake.closed is False
        async with h.app.router.lifespan_context(h.app):
            assert fake.closed is False
        assert fake.closed is True  # closed on shutdown, before engine disposal

    run(scenario())


def test_lifespan_default_off_noop(make_app: Any) -> None:
    h = make_app()  # ReBAC disabled → no client retained

    async def scenario() -> None:
        async with h.app.router.lifespan_context(h.app):
            pass  # clean enter/exit; nothing to close

    run(scenario())


# --- (6) real-factory path: build constructs + retains a closable client ----


def test_enabled_without_injected_client_constructs_real_client_and_close(
    make_platform: AppBuilder,
) -> None:
    from ne.permissions_rebac._client import OpenFgaHttpClient

    # No injected seam client → build_platform constructs the real one. No server
    # contact happens at construction (the SDK client is lazy).
    h = make_platform(rebac_settings=_enabled_settings())
    assert isinstance(h.platform.rebac_client, OpenFgaHttpClient)
    run(h.platform.rebac_client.close())  # closes the owned session, no server needed
