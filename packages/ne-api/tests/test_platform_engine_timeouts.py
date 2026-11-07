# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""P2-16 at the ne-api composition root: the runtime + audit engines are built
through the shared timeout helper, so neither ships with unbounded blocking
connect defaults. Captured at the ne.audit.engine ``create_engine`` seam.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from ne.api.platform import AppSeams, build_platform
from ne.api.settings import ApiSettings
from ne.audit.config import AuditSettings
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
    RegoFaithfulPdpFactory,
    ScriptedGateway,
    StaticEmbedder,
    StaticSparseEncoder,
)


def test_platform_runtime_and_audit_engines_have_timeout_connect_args(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reset_config_hash_for_testing()
    runtime_url = f"sqlite:///{tmp_path}/runtime.db"
    audit_url = f"sqlite:///{tmp_path}/audit.db"

    # Capture every create_engine that routes through the shared timeout helper.
    captured: dict[str, dict[str, Any]] = {}
    real = sa.create_engine

    def fake(url: str, **kwargs: object) -> sa.Engine:
        captured[url] = dict(kwargs)
        return real(url, **kwargs)

    monkeypatch.setattr("ne.audit.engine.sa.create_engine", fake)

    registry = PluginRegistry()
    registry.register("token_verifier", "fake", lambda oidc: FakeTokenVerifier())
    registry.register("policy_decision_point", "opa", RegoFaithfulPdpFactory)

    seams = AppSeams(
        identity_settings=IdentitySettings(
            verifier="fake",
            oidc=OidcConfig(issuer=ISSUER, audience="ne-api"),
            principal_claims=("email", "department"),
        ),
        runtime_settings=RuntimeSettings(session_database_url=runtime_url),
        audit_settings=AuditSettings(database_url=audit_url),
        permissions_settings=PermissionsSettings(database_url=f"sqlite:///{tmp_path}/perms.db"),
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
        plugin_registry=registry,
    )
    platform = build_platform(
        ApiSettings(
            qdrant_url="http://qdrant.test",
            qdrant_collection="t",
            tei_url="http://tei.test",
        ),
        seams,
    )
    try:
        # Both the runtime session engine and the audit read/write engine were built
        # with the sqlite busy-timeout connect_args (Postgres would carry connect_timeout
        # + statement/lock timeouts) — no unbounded-blocking default at the root.
        assert captured[runtime_url]["connect_args"] == {"timeout": 10}
        assert captured[audit_url]["connect_args"] == {"timeout": 10}
    finally:
        for engine in platform.engines:
            engine.dispose()
        reset_config_hash_for_testing()
