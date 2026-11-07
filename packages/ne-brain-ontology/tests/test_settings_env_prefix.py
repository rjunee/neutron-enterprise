# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""OntologySettings env_prefix: NE_ONTOLOGY_ binds, bare NE_ does not, hash/TOML parity."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic_settings import SettingsConfigDict

from ne.brain_ontology.config import OntologySettings
from ne.contracts.config import compute_platform_config_hash


@pytest.fixture(autouse=True)
def _scrub_ne_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop ambient NE_* config so a developer's env (e.g. a live-park NE_FUSEKI_URL)
    never leaks into these tests."""
    for name in [k for k in os.environ if k.startswith("NE_")]:
        monkeypatch.delenv(name)


def test_defaults_unchanged() -> None:
    s = OntologySettings()
    assert s.backend == "oxigraph"
    assert s.fuseki_url is None
    assert s.verified_answers_enabled is False
    assert s.verified_max_rows == 500


def test_new_prefix_binds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NE_ONTOLOGY_BACKEND", "fuseki")
    monkeypatch.setenv("NE_ONTOLOGY_FUSEKI_URL", "http://fuseki:3030/ne")
    monkeypatch.setenv("NE_ONTOLOGY_VERIFIED_MAX_ROWS", "42")
    s = OntologySettings()
    assert s.backend == "fuseki"
    assert s.fuseki_url == "http://fuseki:3030/ne"
    assert s.verified_max_rows == 42


def test_old_bare_env_no_longer_binds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NE_BACKEND", "fuseki")
    monkeypatch.setenv("NE_FUSEKI_URL", "http://x:3030/ne")
    monkeypatch.setenv("NE_VERIFIED_ANSWERS_ENABLED", "true")
    s = OntologySettings()
    assert s.backend == "oxigraph"
    assert s.fuseki_url is None
    assert s.verified_answers_enabled is False


def test_env_prefix_not_in_config_hash_preimage() -> None:
    """Default config_hash is byte-identical across the env_prefix change (the
    freeze governance proof — env_prefix never enters the model_dump preimage)."""

    class _OldBarePrefix(OntologySettings):
        model_config = SettingsConfigDict(
            env_prefix="NE_",
            env_nested_delimiter="__",
            extra="forbid",
            toml_file=Path("ne.toml"),
        )

    assert compute_platform_config_hash(
        {"ne-brain-ontology": OntologySettings()}
    ) == compute_platform_config_hash({"ne-brain-ontology": _OldBarePrefix()})


def test_toml_keys_unaffected_by_env_prefix(tmp_path: Path) -> None:
    """TOML keys stay bare field names — env_prefix is not load-bearing for TOML."""
    cfg = tmp_path / "ne.toml"
    cfg.write_text('backend = "fuseki"\nfuseki_url = "http://localhost:3030/ne"\n')
    s = OntologySettings.from_config_file(cfg)
    assert s.backend == "fuseki"
    assert s.fuseki_url == "http://localhost:3030/ne"
