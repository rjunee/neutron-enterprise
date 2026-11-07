# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-INGEST (P1-4) — the ne-api composition-root connector-sync wiring proof.

Proves the default-off byte-identity (no NE_CONNECT_SYNC_* ⇒ zero connect tool
policies, unchanged config_hash), the conditional per-source ToolArgPolicy merge +
extra-module stamp when a source is configured, and that the composed ChunkStore is
now exposed on the Platform. The governed spine is REAL; only the network edges are
faked (the conftest pattern).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from ne.api.connect_sync import build_connect_sync_request_bindings
from ne.api.platform import AppSeams, Platform, build_platform
from ne.api.settings import ApiSettings
from ne.audit.config import AuditSettings
from ne.brain_ontology.config import OntologySettings
from ne.brain_ontology.verified.templates import load_template_registry
from ne.connect.runner import ConnectSyncSettings
from ne.contracts import reset_config_hash_for_testing
from ne.contracts.plugins import PluginRegistry
from ne.contracts.types import Lane
from ne.gateway.config import GatewaySettings, ModelDef
from ne.identity.config import IdentitySettings, OidcConfig
from ne.permissions.config import PermissionsSettings
from ne.runtime.config import RuntimeSettings

from .conftest import (
    ISSUER,
    FakeTokenVerifier,
    InMemoryFilteredChunkStore,
    RegoFaithfulPdpFactory,
    ScriptedGateway,
    StaticEmbedder,
    StaticSparseEncoder,
)

_CONNECT_PREFIX = "write.internal.connect."


def _connect_tools(platform: Platform) -> list[str]:
    return [t for t in platform.govern_settings.guardrails.tools if t.startswith(_CONNECT_PREFIX)]


AppBuilder = Callable[..., Platform]


@pytest.fixture
def make_platform(tmp_path: Path) -> Iterator[AppBuilder]:
    engines: list[Any] = []

    def _make(
        *,
        url_key: str | None = None,
        chunk_store: Any | None = None,
        ontology_settings: OntologySettings | None = None,
    ) -> Platform:
        reset_config_hash_for_testing()
        registry = PluginRegistry()
        registry.register("token_verifier", "fake", lambda oidc: FakeTokenVerifier())
        registry.register("policy_decision_point", "opa", RegoFaithfulPdpFactory)

        key = url_key or uuid.uuid4().hex
        runtime_url = f"sqlite:///{tmp_path}/runtime-{key}.db"
        audit_url = f"sqlite:///{tmp_path}/audit-{key}.db"
        perms_url = f"sqlite:///{tmp_path}/perms-{key}.db"

        store = chunk_store if chunk_store is not None else InMemoryFilteredChunkStore(())
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
            plugin_registry=registry,
            ontology_settings=ontology_settings,
        )
        platform = build_platform(
            ApiSettings(
                qdrant_url="http://qdrant.test", qdrant_collection="t", tei_url="http://tei.test"
            ),
            seams,
        )
        engines.extend(platform.engines)
        return platform

    yield _make
    for engine in engines:
        engine.dispose()


# --- (t1) default-off: no connect policies, byte-identical config_hash ------


def test_default_off_no_connect_policies_and_stable_hash(make_platform: AppBuilder) -> None:
    first = make_platform(url_key="ident")
    assert first.connect_sync_settings.sources == ()
    assert _connect_tools(first) == []
    second = make_platform(url_key="ident")
    assert second.config_hash == first.config_hash  # default-off byte-identical


# --- (t2) configured: policy merged pre-freeze + conditional stamp changes hash --


def test_configured_source_merges_policy_and_changes_hash(
    make_platform: AppBuilder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    default = make_platform(url_key="ident")
    default_hash = default.config_hash

    monkeypatch.setenv("NE_CONNECT_SYNC_SOURCES", '["fixture"]')
    monkeypatch.setenv("NE_CONNECT_SYNC_STATE_PATH", str(tmp_path / "state.db"))
    monkeypatch.setenv("NE_CONNECT_SYNC_QUARANTINE_PATH", str(tmp_path / "q.db"))
    fixture_path = tmp_path / "configured-fixture.json"
    fixture_path.write_text("[]", encoding="utf-8")
    monkeypatch.setenv("NE_CONNECT_FIXTURE_PATH", str(fixture_path))
    monkeypatch.setenv("NE_CONNECT_FIXTURE_OWNER", "configured-test")
    configured = make_platform(url_key="ident")
    assert configured.connect_sync_settings.sources == ("fixture",)
    assert "write.internal.connect.sync_fixture" in configured.govern_settings.guardrails.tools
    # The conditional extra-module stamp + the merged policy both move the hash.
    assert configured.config_hash != default_hash


def test_only_runnable_sources_receive_manual_request_bindings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ne.api import connect_sync

    settings = ConnectSyncSettings(
        sources=("m365", "csv"),
        runnable_sources=("csv",),
        state_path="state.db",
        quarantine_path="quarantine.db",
    )
    monkeypatch.setattr(connect_sync, "configured_source_name", lambda source: source)

    bindings = build_connect_sync_request_bindings(
        settings,
        object(),  # type: ignore[arg-type]
        config_hash="a" * 64,
        principal_provider=lambda: None,
    )

    assert [binding.spec.name for binding in bindings] == [
        "write.internal.connect.request_sync_csv"
    ]


def _configure_m365_sync(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    source_link_host: str,
    client_secret: str,
) -> None:
    monkeypatch.setenv("NE_CONNECT_SYNC_SOURCES", '["m365"]')
    monkeypatch.setenv("NE_CONNECT_SYNC_STATE_PATH", str(tmp_path / "m365-state.db"))
    monkeypatch.setenv("NE_CONNECT_SYNC_QUARANTINE_PATH", str(tmp_path / "m365-q.db"))
    monkeypatch.setenv("NE_M365_TENANT_ID", "tenant-id")
    monkeypatch.setenv("NE_M365_CLIENT_ID", "client-id")
    monkeypatch.setenv("NE_M365_AUTH_METHOD", "secret")
    monkeypatch.setenv("NE_M365_CLIENT_SECRET", client_secret)
    monkeypatch.setenv("NE_M365_DRIVE_IDS", '["drive-1"]')
    monkeypatch.setenv("NE_M365_OWNER", "platform-test")
    monkeypatch.setenv("NE_M365_SOURCE_LINK_ALLOWED_HOSTS", f'["{source_link_host}"]')


def test_connector_source_link_authority_is_frozen_with_secret_masking_and_root_parity(
    make_platform: AppBuilder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure_m365_sync(
        monkeypatch,
        tmp_path,
        source_link_host="tenant.sharepoint.com",
        client_secret="first-secret-value",
    )
    api_root = make_platform(url_key="source-link-hash")

    # ne-api and ne-worker call this same composition root. Rebuilding from the
    # same fully resolved settings is therefore the cross-process parity proxy.
    worker_root = make_platform(url_key="source-link-hash")
    assert worker_root.config_hash == api_root.config_hash

    # SecretStr rotation is deliberately masked from the platform hash.
    monkeypatch.setenv("NE_M365_CLIENT_SECRET", "rotated-secret-value")
    rotated_secret = make_platform(url_key="source-link-hash")
    assert rotated_secret.config_hash == api_root.config_hash

    # The non-secret source-link authority is decision-affecting and must move it.
    monkeypatch.setenv("NE_M365_SOURCE_LINK_ALLOWED_HOSTS", '["other-tenant.sharepoint.com"]')
    changed_authority = make_platform(url_key="source-link-hash")
    assert changed_authority.config_hash != api_root.config_hash


def test_salesforce_source_link_origin_and_authority_are_frozen(
    make_platform: AppBuilder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NE_CONNECT_SYNC_SOURCES", '["salesforce"]')
    monkeypatch.setenv("NE_CONNECT_SYNC_STATE_PATH", str(tmp_path / "sf-state.db"))
    monkeypatch.setenv("NE_CONNECT_SYNC_QUARANTINE_PATH", str(tmp_path / "sf-q.db"))
    monkeypatch.setenv("NE_SALESFORCE_AUTH_METHOD", "client_credentials")
    monkeypatch.setenv("NE_SALESFORCE_CLIENT_ID", "client-id")
    monkeypatch.setenv("NE_SALESFORCE_CLIENT_SECRET", "first-secret-value")
    monkeypatch.setenv("NE_SALESFORCE_INSTANCE_URL", "https://acme.my.salesforce.com")
    monkeypatch.setenv("NE_SALESFORCE_OWNER", "platform-test")
    monkeypatch.setenv("NE_SALESFORCE_OBJECTS", '[{"name":"Account","content_fields":["Name"]}]')

    baseline = make_platform(url_key="salesforce-source-link-hash")

    monkeypatch.setenv("NE_SALESFORCE_CLIENT_SECRET", "rotated-secret-value")
    rotated_secret = make_platform(url_key="salesforce-source-link-hash")
    assert rotated_secret.config_hash == baseline.config_hash

    monkeypatch.setenv("NE_SALESFORCE_SOURCE_LINK_ALLOWED_HOSTS", '["acme.my.salesforce.com"]')
    changed_authority = make_platform(url_key="salesforce-source-link-hash")
    assert changed_authority.config_hash != baseline.config_hash

    monkeypatch.setenv("NE_SALESFORCE_INSTANCE_URL", "https://other.my.salesforce.com")
    monkeypatch.setenv("NE_SALESFORCE_INSTANCE_ALLOWED_HOSTS", '["other.my.salesforce.com"]')
    monkeypatch.setenv("NE_SALESFORCE_SOURCE_LINK_ALLOWED_HOSTS", '["other.my.salesforce.com"]')
    changed_origin = make_platform(url_key="salesforce-source-link-hash")
    assert changed_origin.config_hash != changed_authority.config_hash


def test_configured_source_and_verified_answers_share_boot_hash_projection(
    make_platform: AppBuilder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NE_CONNECT_SYNC_SOURCES", '["fixture"]')
    monkeypatch.setenv("NE_CONNECT_SYNC_STATE_PATH", str(tmp_path / "state.db"))
    monkeypatch.setenv("NE_CONNECT_SYNC_QUARANTINE_PATH", str(tmp_path / "q.db"))
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text("[]", encoding="utf-8")
    monkeypatch.setenv("NE_CONNECT_FIXTURE_PATH", str(fixture_path))
    monkeypatch.setenv("NE_CONNECT_FIXTURE_OWNER", "projection-test")
    templates = tmp_path / "verified"
    templates.mkdir()
    (templates / "accounts.toml").write_text(
        '''template_id = "accounts"
version = 1
description = "List governed accounts."
sparql = """
SELECT ?account WHERE {
  #BINDINGS#
  ?account a <https://neutron.enterprise/schema/operational/Account> .
}
"""

[scope]
graph_classes = ["accounts"]
result_shape = "enumerable"

[answer]
columns = ["account"]
citation_var = "account"
''',
        encoding="utf-8",
    )

    platform = make_platform(
        ontology_settings=OntologySettings(
            verified_answers_enabled=True,
            verified_templates_dir=templates,
            include_ontology_graph=False,
            backend="oxigraph",
        )
    )

    assert platform.verified_curation is not None
    registry = load_template_registry(templates)
    assert platform.verified_curation.project_config_hash(registry) == platform.config_hash


# --- (t3) the composed chunk store is exposed on the Platform ---------------


def test_platform_exposes_composed_chunk_store(make_platform: AppBuilder) -> None:
    store = InMemoryFilteredChunkStore(())
    platform = make_platform(chunk_store=store)
    assert platform.chunk_store is store
