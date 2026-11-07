# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Layered pydantic-settings precedence: init > env (NE_/__) > TOML > defaults."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel, Field, ValidationError

from ne.contracts import ConfigFileNotFoundError, NeSettings


class BrainSettings(BaseModel):
    vector_store: str = "default-store"


class DemoSettings(NeSettings):
    brain: BrainSettings = Field(default_factory=BrainSettings)


def _write_toml(tmp_path: Path, value: str) -> None:
    (tmp_path / "ne.toml").write_text(f'[brain]\nvector_store = "{value}"\n', encoding="utf-8")


def test_defaults_when_nothing_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    settings = DemoSettings()
    assert settings.brain.vector_store == "default-store"


def test_toml_overrides_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_toml(tmp_path, "toml-store")
    settings = DemoSettings()
    assert settings.brain.vector_store == "toml-store"


def test_env_overrides_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_toml(tmp_path, "toml-store")
    monkeypatch.setenv("NE_BRAIN__VECTOR_STORE", "env-store")
    settings = DemoSettings()
    assert settings.brain.vector_store == "env-store"


def test_init_overrides_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_toml(tmp_path, "toml-store")
    monkeypatch.setenv("NE_BRAIN__VECTOR_STORE", "env-store")
    settings = DemoSettings(brain=BrainSettings(vector_store="init-store"))
    assert settings.brain.vector_store == "init-store"


def test_full_precedence_chain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Every layer disagrees; the winner proves the full ordering in one shot.
    monkeypatch.chdir(tmp_path)
    _write_toml(tmp_path, "toml-store")
    monkeypatch.setenv("NE_BRAIN__VECTOR_STORE", "env-store")
    assert DemoSettings().brain.vector_store == "env-store"  # env > toml
    assert (
        DemoSettings(brain=BrainSettings(vector_store="init-store")).brain.vector_store
        == "init-store"  # init > env
    )


def test_extra_forbid_rejects_unknown_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    # model_validate still enforces extra="forbid"; it also sidesteps the typed
    # __init__ signature so the deliberately-unknown key is expressible.
    with pytest.raises(ValidationError):
        DemoSettings.model_validate({"unknown_setting": "boom"})


def _write_named_toml(path: Path, value: str) -> None:
    path.write_text(f'[brain]\nvector_store = "{value}"\n', encoding="utf-8")


def test_from_config_file_loads_selected_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_toml(tmp_path, "decoy-cwd-store")  # decoy ne.toml
    selected = tmp_path / "customer-a.toml"
    _write_named_toml(selected, "customer-a-store")
    settings = DemoSettings.from_config_file(selected)
    assert settings.brain.vector_store == "customer-a-store"


def test_from_config_file_env_still_overrides_file_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_toml(tmp_path, "decoy-cwd-store")
    selected = tmp_path / "customer-a.toml"
    _write_named_toml(selected, "customer-a-store")
    monkeypatch.setenv("NE_BRAIN__VECTOR_STORE", "env-store")
    # env beats the selected file...
    assert DemoSettings.from_config_file(selected).brain.vector_store == "env-store"
    # ...and an init kwarg beats env — full layering preserved under selection.
    assert (
        DemoSettings.from_config_file(
            selected, brain=BrainSettings(vector_store="init-store")
        ).brain.vector_store
        == "init-store"
    )


def test_from_config_file_missing_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ConfigFileNotFoundError):
        DemoSettings.from_config_file(tmp_path / "does-not-exist.toml")


def test_from_config_file_selection_does_not_leak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_toml(tmp_path, "cwd-store")
    selected = tmp_path / "customer-a.toml"
    _write_named_toml(selected, "customer-a-store")
    assert DemoSettings.from_config_file(selected).brain.vector_store == "customer-a-store"
    # After the scoped call, a plain construction reads the cwd ne.toml again.
    assert DemoSettings().brain.vector_store == "cwd-store"


def test_ne_config_file_env_var_selects_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_toml(tmp_path, "decoy-cwd-store")
    selected = tmp_path / "deployment.toml"
    _write_named_toml(selected, "deployment-store")
    monkeypatch.setenv("NE_CONFIG_FILE", str(selected))
    assert DemoSettings().brain.vector_store == "deployment-store"


def test_ne_config_file_missing_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NE_CONFIG_FILE", str(tmp_path / "dangling.toml"))
    with pytest.raises(ConfigFileNotFoundError):
        DemoSettings()


def test_explicit_config_file_beats_ne_config_file_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    env_file = tmp_path / "env.toml"
    _write_named_toml(env_file, "env-file-store")
    explicit_file = tmp_path / "explicit.toml"
    _write_named_toml(explicit_file, "explicit-file-store")
    monkeypatch.setenv("NE_CONFIG_FILE", str(env_file))
    assert DemoSettings.from_config_file(explicit_file).brain.vector_store == "explicit-file-store"
