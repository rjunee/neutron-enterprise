# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-FIX-SYNC-INVOKERS (P2-20) — the `ne permissions sync` CLI contract.

The non-pull path (oidc-claims default) is a clean exit-0 no-op that NEVER composes
the platform; a configured fixture pull source runs run_once over an injected
sqlite/stub platform and prints the counts (principals land in the store); a source
that fails at run time exits 1 naming only the exception class (C12).
"""

from __future__ import annotations

import importlib
import json
import uuid
from pathlib import Path

import pytest
from click.testing import CliRunner

permissions_cli = importlib.import_module("ne.cli.permissions")
permissions = permissions_cli.permissions

_FIXTURE = {
    "name": "fixture",
    "snapshots": [
        {
            "is_full": True,
            "principals": [
                {"principal_id": "u-ada", "kind": "user", "display_name": "Ada"},
                {"principal_id": "g-eng", "kind": "group"},
            ],
            "edges": [{"member_id": "u-ada", "group_id": "g-eng"}],
            "replaced_groups": ["g-eng"],
            "replaced_members": [],
            "removed_principal_ids": [],
            "state": {},
        }
    ],
}


def _sqlite_platform(tmp_path: Path, perms):  # type: ignore[no-untyped-def]
    """Compose a REAL governed platform over sqlite + stub edges (self-contained)."""
    from ne.api.platform import AppSeams, build_platform
    from ne.api.settings import ApiSettings
    from ne.audit.config import AuditSettings
    from ne.contracts import reset_config_hash_for_testing
    from ne.contracts.plugins import PluginRegistry
    from ne.contracts.types import Lane
    from ne.gateway.config import GatewaySettings, ModelDef
    from ne.identity.config import IdentitySettings, OidcConfig
    from ne.runtime.config import RuntimeSettings
    from ne.worker.testing import (
        ISSUER,
        RegoFaithfulPdpFactory,
        StubChunkStore,
        StubEmbedder,
        StubSparseEncoder,
    )

    reset_config_hash_for_testing()
    registry = PluginRegistry()
    registry.register("policy_decision_point", "opa", RegoFaithfulPdpFactory)
    key = uuid.uuid4().hex
    seams = AppSeams(
        identity_settings=IdentitySettings(
            verifier="fake",
            oidc=OidcConfig(issuer=ISSUER, audience="ne-cli"),
            principal_claims=("email", "department"),
        ),
        runtime_settings=RuntimeSettings(session_database_url=f"sqlite:///{tmp_path}/rt-{key}.db"),
        audit_settings=AuditSettings(database_url=f"sqlite:///{tmp_path}/au-{key}.db"),
        permissions_settings=perms,
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
        embedder=StubEmbedder(),
        sparse_encoder=StubSparseEncoder(),
        chunk_store=StubChunkStore(),
        plugin_registry=registry,
    )
    return build_platform(
        ApiSettings(
            qdrant_url="http://qdrant.test", qdrant_collection="t", tei_url="http://tei.test"
        ),
        seams,
    )


def _fixture_perms(tmp_path: Path, fixture_path: str):  # type: ignore[no-untyped-def]
    from ne.permissions.config import PermissionsSettings

    return PermissionsSettings(
        database_url=f"sqlite:///{tmp_path}/pm-{uuid.uuid4().hex}.db",
        mode="fixture",
        fixture_path=fixture_path,
    )


# --- (t1) oidc-claims default ⇒ clean no-op, platform NEVER composed ---------


def test_sync_non_pull_mode_is_clean_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NE_PERMISSIONS_MODE", raising=False)

    def _boom() -> object:
        raise AssertionError("platform must not be composed")

    monkeypatch.setattr(permissions_cli, "_build_platform", _boom)
    result = CliRunner().invoke(permissions, ["sync"])
    assert result.exit_code == 0, result.output
    assert "nothing to pull" in result.output


# --- (t2) fixture pull source ⇒ run_once, counts printed, store populated ----


def test_sync_fixture_source_runs_and_populates_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fixture_path = tmp_path / "principals.json"
    fixture_path.write_text(json.dumps(_FIXTURE), encoding="utf-8")
    monkeypatch.setenv("NE_PERMISSIONS_MODE", "fixture")
    monkeypatch.setenv("NE_PERMISSIONS_FIXTURE_PATH", str(fixture_path))

    perms = _fixture_perms(tmp_path, str(fixture_path))
    platform = _sqlite_platform(tmp_path, perms)
    monkeypatch.setattr(permissions_cli, "_build_platform", lambda: platform)

    result = CliRunner().invoke(permissions, ["sync"])
    assert result.exit_code == 0, result.output
    assert "principals_upserted=2" in result.output
    assert platform.perm_store.is_known("u-ada")


# --- (t3) a source failing at run time ⇒ exit 1, class-name-only error -------


def test_sync_failing_source_exits_one_class_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    missing = str(tmp_path / "does-not-exist.json")
    monkeypatch.setenv("NE_PERMISSIONS_MODE", "fixture")
    monkeypatch.setenv("NE_PERMISSIONS_FIXTURE_PATH", missing)

    perms = _fixture_perms(tmp_path, missing)
    platform = _sqlite_platform(tmp_path, perms)
    monkeypatch.setattr(permissions_cli, "_build_platform", lambda: platform)

    result = CliRunner().invoke(permissions, ["sync"])
    assert result.exit_code == 1
    assert "FileNotFoundError" in result.output
    # C12: no path/payload leaked — only the class name.
    assert missing not in result.output
