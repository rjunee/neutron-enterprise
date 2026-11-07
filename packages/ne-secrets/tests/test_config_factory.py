# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Tests for SecretsSettings defaults/env overrides and create_secret_store()."""

from __future__ import annotations

from pathlib import Path

import pytest

from ne.contracts.plugins import PluginRegistry
from ne.secrets.codecs import PlainJsonCodec, SopsCodec
from ne.secrets.config import FileStoreConfig, SecretsSettings
from ne.secrets.errors import SecretsBackendError
from ne.secrets.factory import create_secret_store
from ne.secrets.file_store import FileSecretStore
from ne.secrets.openbao import OpenBaoSecretStore


def test_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    settings = SecretsSettings()
    assert settings.store == "sops-file"
    assert settings.file.mode == "sops"
    assert settings.openbao.url == "http://localhost:8200"
    assert settings.openbao.token_env_var == "BAO_TOKEN"


def test_env_override_store_and_nested_openbao_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NE_SECRETS_STORE", "openbao")
    monkeypatch.setenv("NE_SECRETS_OPENBAO__MOUNT", "custom-mount")
    settings = SecretsSettings()
    assert settings.store == "openbao"
    assert settings.openbao.mount == "custom-mount"


def test_no_token_field_on_settings() -> None:
    assert "token" not in SecretsSettings.model_fields
    assert "token" not in type(SecretsSettings().openbao).model_fields


def test_create_secret_store_sops_file_plain_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    settings = SecretsSettings(file=FileStoreConfig(path=tmp_path / "s.json", mode="plain"))
    store = create_secret_store(settings)
    assert isinstance(store, FileSecretStore)
    assert isinstance(store._codec, PlainJsonCodec)


def test_create_secret_store_sops_file_sops_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    settings = SecretsSettings(file=FileStoreConfig(path=tmp_path / "s.json", mode="sops"))
    store = create_secret_store(settings)
    assert isinstance(store, FileSecretStore)
    assert isinstance(store._codec, SopsCodec)


def test_sops_timeout_flows_into_constructed_codec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    settings = SecretsSettings(
        file=FileStoreConfig(path=tmp_path / "s.json", mode="sops", sops_timeout_seconds=7.0)
    )
    store = create_secret_store(settings)
    assert isinstance(store, FileSecretStore)
    assert isinstance(store._codec, SopsCodec)
    assert store._codec._timeout == 7.0


def test_create_secret_store_openbao_reads_token_from_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BAO_TOKEN", "test-token-value")
    settings = SecretsSettings(store="openbao")
    store = create_secret_store(settings)
    assert isinstance(store, OpenBaoSecretStore)


def test_create_secret_store_openbao_missing_token_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BAO_TOKEN", raising=False)
    settings = SecretsSettings(store="openbao")
    with pytest.raises(SecretsBackendError) as excinfo:
        create_secret_store(settings)
    assert "BAO_TOKEN" in str(excinfo.value)


def test_create_secret_store_honors_programmatic_registry_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    class Fake:
        def __init__(self) -> None:
            self.marker = "fake"

    registry = PluginRegistry()
    registry.register("secret_store", "sops-file", Fake)
    settings = SecretsSettings()
    store = create_secret_store(settings, registry=registry)
    assert isinstance(store, Fake)
