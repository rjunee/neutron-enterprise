# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""015A0-AUTHORITY-M365: the credential-free M365 descriptor the API may hold.

The worker registers authority from the FULL connector configuration; the API
must report the same configured contract hash and epoch while never mounting a
credential. These are the two halves of that: the projection carries no
credential VALUE, and the composed root exposes it on the Platform without it
reaching request state or bringing a Writer with it.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import uuid
from collections.abc import Callable, Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from ne.api.platform import AppSeams, Platform, build_platform
from ne.api.settings import ApiSettings
from ne.api.source_authority_descriptors import (
    M365_CREDENTIAL_FIELD_NAMES,
    M365SourceAuthorityDescriptor,
    descriptor_carries_no_credential,
    project_m365_source_authority,
)
from ne.audit.config import AuditSettings
from ne.connect.m365.config import M365ConnectorSettings
from ne.connect.m365.contract import m365_contract_identity, m365_contract_sla_seconds
from ne.contracts import reset_config_hash_for_testing
from ne.contracts.plugins import PluginRegistry
from ne.contracts.source_authority import CurrentSourceContractHead, SourceReconciliationScope
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

_TENANT = "11111111-1111-1111-1111-111111111111"
_SECRET = "the-only-real-secret-value"
_PEM = "/etc/neutron/secrets/m365-signing-key.pem"


def _m365_settings(**overrides: Any) -> M365ConnectorSettings:
    values: dict[str, Any] = {
        "tenant_id": _TENANT,
        "client_id": "22222222-2222-2222-2222-222222222222",
        "auth_method": "secret",
        "client_secret": _SECRET,
        "site_ids": ("site-eng",),
        "owner": "data-eng@contoso.test",
    }
    values.update(overrides)
    return M365ConnectorSettings(**values)


def _head(settings: M365ConnectorSettings, *, epoch: int = 1) -> CurrentSourceContractHead:
    return CurrentSourceContractHead(
        connector_scope=SourceReconciliationScope(
            path=("m365-sharepoint", "deployment-alpha", "conn-1", _TENANT)
        ),
        source_generation=1,
        contract_epoch=epoch,
        source_contract=m365_contract_identity(settings),
        head_revision="chead-abc",
    )


def _descriptor(**overrides: Any) -> M365SourceAuthorityDescriptor:
    settings = _m365_settings(**overrides.pop("settings", {}))
    return project_m365_source_authority(
        settings,
        head=_head(settings),
        freshness_sla_seconds=m365_contract_sla_seconds(settings),
        resolved=overrides.pop("resolved", False),
    )


# -- the projection ----------------------------------------------------------


def test_the_descriptor_field_set_is_exactly_the_credential_free_projection() -> None:
    assert set(M365SourceAuthorityDescriptor.model_fields) == {
        "source_kind",
        "source_name",
        "connector_instance_id",
        "source_generation",
        "contract_epoch",
        "contract_identity_hash",
        "descriptor_schema_version",
        "descriptor_hash",
        "head_revision",
        "freshness_sla_seconds",
        "resolved",
    }


def test_no_field_name_of_the_descriptor_is_a_credential_field() -> None:
    assert set(M365SourceAuthorityDescriptor.model_fields) & M365_CREDENTIAL_FIELD_NAMES == set()


@pytest.mark.parametrize(
    "auth",
    [
        pytest.param(
            {
                "auth_method": "certificate",
                "certificate_pem_path": _PEM,
                "private_key_pem_path": _PEM,
                "client_secret": "",
            },
            id="certificate",
        ),
        pytest.param({"auth_method": "secret", "client_secret": _SECRET}, id="secret"),
    ],
)
def test_no_credential_value_reaches_the_projected_descriptor(auth: dict[str, Any]) -> None:
    """BOTH auth modes: a mode-specific credential is still a credential.

    Parametrized because the two modes populate DIFFERENT settings fields — a
    certificate-mode fixture leaves ``client_secret`` empty, so it cannot detect
    a secret smuggled into the projection.
    """
    settings = _m365_settings(**auth)
    descriptor = project_m365_source_authority(
        settings,
        head=_head(settings),
        freshness_sla_seconds=m365_contract_sla_seconds(settings),
        resolved=False,
    )
    rendered = descriptor.model_dump_json()
    for value in (_SECRET, _PEM, settings.client_id):
        if value:
            assert value not in rendered, value
    assert descriptor_carries_no_credential(descriptor, settings) is True


def test_the_credential_guard_detects_a_smuggled_value_under_any_field_name() -> None:
    """The guard compares VALUES, so renaming a field does not evade it."""
    settings = _m365_settings()
    smuggled = _descriptor().model_copy(update={"head_revision": _SECRET})
    assert descriptor_carries_no_credential(smuggled, settings) is False


def test_the_descriptor_reports_the_exact_configured_contract_hash() -> None:
    settings = _m365_settings()
    descriptor = _descriptor()
    expected = m365_contract_identity(settings)
    assert descriptor.contract_identity_hash == expected.identity_hash
    assert descriptor.descriptor_hash == expected.descriptor_hash
    assert descriptor.configured_contract_hash == expected.identity_hash


def test_a_changed_connector_configuration_changes_the_reported_hash() -> None:
    baseline = _descriptor().contract_identity_hash
    changed = _descriptor(settings={"owner": "other-team@contoso.test"}).contract_identity_hash
    assert changed != baseline


def test_a_client_secret_rotation_does_not_change_the_reported_hash() -> None:
    baseline = _descriptor().contract_identity_hash
    rotated = _descriptor(settings={"client_secret": "rotated-not-real"}).contract_identity_hash
    assert rotated == baseline


def test_the_connector_instance_id_is_taken_from_the_heads_own_scope() -> None:
    assert _descriptor().connector_instance_id == "conn-1"


def test_a_scope_without_a_connector_instance_component_cannot_be_projected() -> None:
    settings = _m365_settings()
    truncated = CurrentSourceContractHead(
        connector_scope=SourceReconciliationScope(path=("m365-sharepoint", "deployment-alpha")),
        source_generation=1,
        contract_epoch=1,
        source_contract=m365_contract_identity(settings),
        head_revision="chead-abc",
    )
    with pytest.raises(ValueError):
        project_m365_source_authority(
            settings,
            head=truncated,
            freshness_sla_seconds=Decimal(3600),
            resolved=False,
        )


def test_the_descriptor_is_frozen_and_forbids_extra_fields() -> None:
    from pydantic import ValidationError

    descriptor = _descriptor()
    with pytest.raises(ValidationError):
        descriptor.contract_epoch = 9  # type: ignore[misc]
    with pytest.raises(ValidationError):
        M365SourceAuthorityDescriptor(
            **{**descriptor.model_dump(), "client_secret": _SECRET}  # type: ignore[arg-type]
        )


def test_an_unresolved_epoch_is_reported_as_unresolved() -> None:
    assert _descriptor(resolved=False).resolved is False
    assert _descriptor(resolved=True).resolved is True


# -- composition -------------------------------------------------------------


AppBuilder = Callable[..., Platform]


@pytest.fixture
def make_platform(tmp_path: Path) -> Iterator[AppBuilder]:
    engines: list[Any] = []

    def _make(*, url_key: str | None = None) -> Platform:
        reset_config_hash_for_testing()
        registry = PluginRegistry()
        registry.register("token_verifier", "fake", lambda oidc: FakeTokenVerifier())
        registry.register("policy_decision_point", "opa", RegoFaithfulPdpFactory)
        key = url_key or uuid.uuid4().hex
        seams = AppSeams(
            identity_settings=IdentitySettings(
                verifier="fake",
                oidc=OidcConfig(issuer=ISSUER, audience="ne-api"),
                principal_claims=("email", "department"),
            ),
            runtime_settings=RuntimeSettings(
                session_database_url=f"sqlite:///{tmp_path}/runtime-{key}.db"
            ),
            audit_settings=AuditSettings(database_url=f"sqlite:///{tmp_path}/audit-{key}.db"),
            permissions_settings=PermissionsSettings(
                database_url=f"sqlite:///{tmp_path}/perms-{key}.db"
            ),
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
            chunk_store=InMemoryFilteredChunkStore(()),
            plugin_registry=registry,
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


def _m365_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **overrides: str) -> None:
    env = {
        "NE_CONNECT_SYNC_SOURCES": '["m365"]',
        "NE_CONNECT_SYNC_STATE_PATH": str(tmp_path / "state.db"),
        "NE_CONNECT_SYNC_QUARANTINE_PATH": str(tmp_path / "q.db"),
        "NE_M365_TENANT_ID": _TENANT,
        "NE_M365_CLIENT_ID": "22222222-2222-2222-2222-222222222222",
        "NE_M365_AUTH_METHOD": "secret",
        "NE_M365_CLIENT_SECRET": _SECRET,
        "NE_M365_SITE_IDS": '["site-eng"]',
        "NE_M365_OWNER": "data-eng@contoso.test",
        "NE_M365_AUTHORITY_ENABLED": "true",
        "NE_M365_AUTHORITY_SWEEP_CADENCE_SECONDS": "120",
        "NE_M365_AUTHORITY_SWEEP_BATCH_SIZE": "50",
        "NE_M365_AUTHORITY_SWEEP_MAX_BATCH_DURATION_SECONDS": "60",
        "NE_M365_AUTHORITY_SWEEP_MAX_POINTS_PER_SCOPE": "500",
    }
    env.update(overrides)
    for name, value in env.items():
        monkeypatch.setenv(name, value)


def test_a_root_without_m365_composes_no_descriptor(make_platform: AppBuilder) -> None:
    assert make_platform().m365_source_authority is None


def test_a_configured_root_composes_the_credential_free_descriptor(
    make_platform: AppBuilder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _m365_env(monkeypatch, tmp_path)
    platform = make_platform()
    descriptor = platform.m365_source_authority
    assert descriptor is not None
    assert descriptor.contract_epoch == 1
    assert descriptor.resolved is False
    assert descriptor.freshness_sla_seconds == Decimal(3600)
    assert _SECRET not in descriptor.model_dump_json()


def test_the_composed_descriptor_reports_the_producers_configured_hash(
    make_platform: AppBuilder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """API and worker agree because there is ONE factory, not two that match."""
    _m365_env(monkeypatch, tmp_path)
    platform = make_platform()
    descriptor = platform.m365_source_authority
    assert descriptor is not None
    producer = m365_contract_identity(M365ConnectorSettings())  # type: ignore[call-arg]
    assert descriptor.contract_identity_hash == producer.identity_hash


def test_an_m365_source_without_authority_enabled_composes_no_descriptor(
    make_platform: AppBuilder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _m365_env(monkeypatch, tmp_path, NE_M365_AUTHORITY_ENABLED="false")
    assert make_platform().m365_source_authority is None


def test_a_misconfigured_m365_connector_does_not_fail_the_whole_composition(
    make_platform: AppBuilder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _m365_env(monkeypatch, tmp_path)
    monkeypatch.delenv("NE_M365_TENANT_ID")
    platform = make_platform()
    assert platform.m365_source_authority is None
    assert platform.config_hash


def test_the_api_composition_root_never_builds_the_m365_authority_producer() -> None:
    """``_compose_m365_source_authority`` may not name a producer type."""
    from ne.api import platform as platform_module

    source = inspect.getsource(platform_module._compose_m365_source_authority)
    for forbidden in (
        "M365AuthorityAdmission",
        "SqlAlchemyCurrentSourceAuthorityWriter",
        "ImmutableKnowledgeArtifactStore",
        "build_ingest_stack",
    ):
        assert forbidden not in source, forbidden


def test_no_field_of_request_state_carries_the_m365_descriptor(
    make_platform: AppBuilder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from ne.api.deps import ApiState

    _m365_env(monkeypatch, tmp_path)
    platform = make_platform()
    assert platform.m365_source_authority is not None
    annotations = {
        field.name: str(field.type)
        for field in dataclasses.fields(ApiState)  # type: ignore[arg-type]
    }
    assert not [
        name
        for name, annotation in annotations.items()
        if "M365SourceAuthorityDescriptor" in annotation
        or "CurrentSourceAuthorityWriter" in annotation
    ]


def test_the_descriptor_module_imports_no_producer_or_engine_symbol() -> None:
    """A projector that could reach a Writer or an engine is producer capability."""
    from ne.api import source_authority_descriptors as module

    tree = ast.parse(inspect.getsource(module))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert not imported & {
        "CurrentSourceAuthorityWriter",
        "SqlAlchemyCurrentSourceAuthorityWriter",
        "ImmutableKnowledgeArtifactWrite",
        "Engine",
    }
