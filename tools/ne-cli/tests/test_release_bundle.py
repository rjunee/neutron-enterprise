# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Tests for the deterministic release bundle builder (`ne release bundle`)."""

from __future__ import annotations

import hashlib
import json
import tarfile
from pathlib import Path

from click.testing import CliRunner

from ne.cli import main
from ne.cli.bundle import build_bundle
from ne.cli.fingerprint import build_manifest, compute_fingerprint
from ne.cli.licenses import Policy, default_policy_path
from ne.cli.sbom import build_sbom

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_bundle_bytes_reproducible(tmp_path: Path) -> None:
    out_a = tmp_path / "a"
    out_b = tmp_path / "b"
    tar_a, _manifest_a, fp_a = build_bundle(REPO_ROOT, "v0.0.0-test", out_a)
    tar_b, _manifest_b, fp_b = build_bundle(REPO_ROOT, "v0.0.0-test", out_b)

    sha_a = hashlib.sha256(tar_a.read_bytes()).hexdigest()
    sha_b = hashlib.sha256(tar_b.read_bytes()).hexdigest()
    assert sha_a == sha_b, "same tree must produce byte-identical bundle tar.gz"
    assert fp_a == fp_b


def test_bundle_contains_manifest_and_all_member_sboms(tmp_path: Path) -> None:
    out = tmp_path / "out"
    tar_path, manifest, _fp = build_bundle(REPO_ROOT, "v0.0.0-test", out)

    prefix = "neutron-platform-v0.0.0-test"
    with tarfile.open(tar_path, mode="r:gz") as tar:
        names = sorted(tar.getnames())
        expected = sorted(
            [f"{prefix}/manifest.json"] + [f"{prefix}/{m['sbom']}" for m in manifest["members"]]
        )
        assert names == expected

        manifest_member = tar.extractfile(f"{prefix}/manifest.json")
        assert manifest_member is not None
        in_tar_manifest = json.loads(manifest_member.read())
        assert in_tar_manifest == manifest

        for member in manifest["members"]:
            fh = tar.extractfile(f"{prefix}/{member['sbom']}")
            assert fh is not None
            content = fh.read()
            assert hashlib.sha256(content).hexdigest() == member["sha256"]


def test_bundle_fingerprint_matches_release_fingerprint_command(tmp_path: Path) -> None:
    manifest = build_manifest(REPO_ROOT, "v0.0.0-test")
    expected_fp = compute_fingerprint(manifest)

    out = tmp_path / "out"
    _tar_path, _manifest, fp = build_bundle(REPO_ROOT, "v0.0.0-test", out)
    assert fp == expected_fp

    result = CliRunner().invoke(
        main, ["release", "fingerprint", "--tag", "v0.0.0-test", "--allow-dirty"]
    )
    assert result.exit_code == 0
    printed = json.loads(result.output)
    assert printed["fingerprint"] == fp


def test_bundle_sbom_serial_deterministic() -> None:
    lock_path = REPO_ROOT / "uv.lock"
    policy = Policy.load(default_policy_path(REPO_ROOT))

    doc_a = build_sbom(lock_path, policy, "ne-cli", deterministic_serial=True)
    doc_b = build_sbom(lock_path, policy, "ne-cli", deterministic_serial=True)
    assert doc_a["serialNumber"] == doc_b["serialNumber"]

    other = build_sbom(lock_path, policy, "ne-contracts", deterministic_serial=True)
    assert other["serialNumber"] != doc_a["serialNumber"]

    default_a = build_sbom(lock_path, policy, "ne-cli")
    default_b = build_sbom(lock_path, policy, "ne-cli")
    assert default_a["serialNumber"] != default_b["serialNumber"]
    assert default_a["serialNumber"] != doc_a["serialNumber"]
