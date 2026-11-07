# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""PermissionsSettings: defaults, env override, mode validation, secret masking."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from ne.contracts.config import compute_config_hash
from ne.permissions.config import PermissionsSettings


def test_defaults() -> None:
    s = PermissionsSettings()
    assert s.mode == "oidc-claims"
    assert s.sync_interval_seconds == 900
    assert s.groups_claim == "groups"
    assert s.max_expansion_depth == 32
    assert s.mapping_path is None
    assert "graph.microsoft.com" in s.graph_allowed_hosts
    assert "graph.microsoft.us" in s.graph_allowed_hosts
    assert "login.microsoftonline.com" in s.login_allowed_hosts


def test_env_override_sync_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NE_PERMISSIONS_SYNC_INTERVAL_SECONDS", "300")
    assert PermissionsSettings().sync_interval_seconds == 300


def test_reads_ne_permissions_database_url_and_ignores_bare(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NE_PERMISSIONS_DATABASE_URL", "postgresql+psycopg://ne@db:5432/perms")
    monkeypatch.setenv("NE_DATABASE_URL", "sqlite:///wrong.db")
    assert PermissionsSettings().database_url == "postgresql+psycopg://ne@db:5432/perms"


def test_bare_ne_database_url_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NE_DATABASE_URL", "sqlite:///wrong.db")
    assert PermissionsSettings().database_url == "sqlite:///ne_permissions.db"


def test_sync_interval_floor() -> None:
    with pytest.raises(ValidationError):
        PermissionsSettings(sync_interval_seconds=0)


def test_entra_mode_requires_tenant_and_client() -> None:
    with pytest.raises(ValidationError):
        PermissionsSettings(mode="entra")
    with pytest.raises(ValidationError):
        PermissionsSettings(mode="entra", entra_tenant_id="t")  # missing client id
    # Both present → valid.
    s = PermissionsSettings(mode="entra", entra_tenant_id="t", entra_client_id="c")
    assert s.mode == "entra"


def test_fixture_mode_requires_path() -> None:
    with pytest.raises(ValidationError):
        PermissionsSettings(mode="fixture")
    assert PermissionsSettings(mode="fixture", fixture_path="/tmp/x.json").mode == "fixture"


def test_mapping_path_composes_with_every_mode(tmp_path: Path) -> None:
    mapping_path = tmp_path / "mapping.json"
    settings = (
        PermissionsSettings(mode="oidc-claims", mapping_path=mapping_path),
        PermissionsSettings(
            mode="fixture", fixture_path="/tmp/principals.json", mapping_path=mapping_path
        ),
        PermissionsSettings(
            mode="entra",
            entra_tenant_id="tenant",
            entra_client_id="client",
            mapping_path=mapping_path,
        ),
    )

    assert [item.mode for item in settings] == ["oidc-claims", "fixture", "entra"]
    assert all(item.mapping_path == mapping_path for item in settings)


def test_mapping_path_reads_ne_permissions_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NE_PERMISSIONS_MAPPING_PATH", "/tmp/mapping.json")

    assert PermissionsSettings().mapping_path == Path("/tmp/mapping.json")


def test_client_secret_is_secretstr_and_masked_in_hash() -> None:
    a = PermissionsSettings(entra_client_secret=SecretStr("hunter2"))
    b = PermissionsSettings(entra_client_secret=SecretStr("different-secret"))
    # Secret material never enters the config_hash preimage → same hash.
    assert compute_config_hash(a) == compute_config_hash(b)
    # A non-secret change DOES move the hash.
    c = PermissionsSettings(entra_client_secret=SecretStr("hunter2"), sync_interval_seconds=300)
    assert compute_config_hash(a) != compute_config_hash(c)
    # Repr/dump masks the secret.
    assert "hunter2" not in str(a.model_dump(mode="json"))
