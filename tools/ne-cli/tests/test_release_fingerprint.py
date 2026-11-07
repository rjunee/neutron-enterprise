# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Tests for the deterministic platform release fingerprint (`ne release fingerprint`)."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

import ne.cli.fingerprint as fingerprint_module
from ne.cli import main
from ne.cli.fingerprint import build_manifest, compute_fingerprint

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def _reset_config_hash():
    """Reset the process-global frozen config_hash before AND after the test, so
    no cross-test frozen-state leaks (the composition-root freeze is a
    process-global singleton)."""
    from ne.contracts.config import reset_config_hash_for_testing

    reset_config_hash_for_testing()
    try:
        yield
    finally:
        reset_config_hash_for_testing()


def test_fingerprint_is_deterministic_across_runs() -> None:
    manifest_a = build_manifest(REPO_ROOT, "v0.0.0-test")
    fp_a = compute_fingerprint(manifest_a)
    manifest_b = build_manifest(REPO_ROOT, "v0.0.0-test")
    fp_b = compute_fingerprint(manifest_b)
    assert manifest_a == manifest_b
    assert fp_a == fp_b
    assert fp_a.startswith("sha256:")


def test_fingerprint_config_hash_matches_composition_root_freeze(_reset_config_hash) -> None:
    from ne.runtime.factory import freeze_platform_config

    frozen = freeze_platform_config()
    computed = fingerprint_module.compute_default_platform_config_hash()
    assert frozen == computed


def test_fingerprint_env_invariant(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    baseline = fingerprint_module.compute_default_platform_config_hash()

    toml_path = tmp_path / "override.toml"
    # session_database_url is a real, direct RuntimeSettings field — overriding
    # it via a selected ne.toml must NOT change the release config-hash, because
    # the release hash is defined over the DEFAULT composed configuration, not
    # whatever env/cwd-file happens to be lying around the calling process.
    toml_path.write_text(
        'session_database_url = "sqlite:///should-not-leak.db"\n', encoding="utf-8"
    )
    monkeypatch.setenv("NE_CONFIG_FILE", str(toml_path))

    overridden = fingerprint_module.compute_default_platform_config_hash()
    assert overridden == baseline


def test_fingerprint_requires_tag_when_head_untagged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fingerprint_module, "_resolve_tag", lambda root: None)
    result = CliRunner().invoke(main, ["release", "fingerprint", "--allow-dirty"])
    assert result.exit_code != 0
    assert "not tagged" in result.output


def test_fingerprint_refuses_dirty_tree_without_allow_dirty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fingerprint_module, "_worktree_dirty", lambda root: True)
    result = CliRunner().invoke(main, ["release", "fingerprint", "--tag", "v0.0.0-test"])
    assert result.exit_code != 0
    assert "dirty" in result.output.lower()

    result_ok = CliRunner().invoke(
        main, ["release", "fingerprint", "--tag", "v0.0.0-test", "--allow-dirty"]
    )
    assert result_ok.exit_code == 0
