# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Tests for SLSA-v1 provenance generation (`ne release provenance`)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from ne.cli.release import build_provenance

_ENV = {
    "GITHUB_SERVER_URL": "https://github.com",
    "GITHUB_REPOSITORY": "qv/neutron-enterprise",
    "GITHUB_REF": "refs/tags/v0.1.0",
    "GITHUB_SHA": "0123456789abcdef0123456789abcdef01234567",
    "GITHUB_WORKFLOW_REF": "qv/neutron-enterprise/.github/workflows/release.yml@refs/tags/v0.1.0",
    "GITHUB_RUN_ID": "12345",
    "GITHUB_RUN_ATTEMPT": "1",
}


def test_valid_slsa_v1_statement(tmp_path: Path) -> None:
    art = tmp_path / "ne_cli-0.1.0-py3-none-any.whl"
    art.write_bytes(b"fake wheel bytes")
    expected = hashlib.sha256(b"fake wheel bytes").hexdigest()

    stmt = build_provenance([art], _ENV)

    assert stmt["_type"] == "https://in-toto.io/Statement/v1"
    assert stmt["predicateType"] == "https://slsa.dev/provenance/v1"
    assert stmt["subject"][0]["name"] == art.name
    assert stmt["subject"][0]["digest"]["sha256"] == expected
    builder = stmt["predicate"]["runDetails"]["builder"]["id"]
    assert "neutron-enterprise" in builder and builder.startswith("https://github.com/")
    ext = stmt["predicate"]["buildDefinition"]["externalParameters"]
    assert ext["ref"] == "refs/tags/v0.1.0"
    dep = stmt["predicate"]["buildDefinition"]["resolvedDependencies"][0]
    assert dep["digest"]["gitCommit"] == _ENV["GITHUB_SHA"]


def test_missing_github_env_fails(tmp_path: Path) -> None:
    art = tmp_path / "x.whl"
    art.write_bytes(b"x")
    incomplete = dict(_ENV)
    del incomplete["GITHUB_SHA"]
    with pytest.raises(ValueError, match="GITHUB_SHA"):
        build_provenance([art], incomplete)
