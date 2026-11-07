# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-069R: uploaded standalone release SBOMs are consistent with the bundle.

`.github/workflows/release.yml` builds the standalone SBOMs it uploads via
`ne release sbom --all --deterministic`, so each is BYTE-IDENTICAL to the copy
inside the release bundle and its sha256 matches the manifest-recorded digest.
These tests prove that consistency by construction (option (a) of the brief):
one deterministic SBOM artifact set that both the manifest and the uploaded
assets reference, so digest verification is consistent end-to-end.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tarfile
from pathlib import Path, PurePosixPath

from click.testing import CliRunner

from ne.cli import main
from ne.cli.bundle import build_bundle
from ne.cli.fingerprint import build_manifest

REPO_ROOT = Path(__file__).resolve().parents[3]

_UUID4_URN = re.compile(
    r"^urn:uuid:[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_UUID5_URN = re.compile(
    r"^urn:uuid:[0-9a-f]{8}-[0-9a-f]{4}-5[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)

_TAG = "v0.0.0-test"


def _run_sbom_cli(out: Path, *, deterministic: bool) -> None:
    """Invoke `ne release sbom --all [--deterministic] --out OUT` from REPO_ROOT.

    Uses the os.chdir(REPO_ROOT) try/finally pattern (the CLI resolves the repo
    root and uv.lock from cwd); never asserts on cwd after the guard.
    """
    args = ["release", "sbom", "--all"]
    if deterministic:
        args.append("--deterministic")
    args += ["--out", str(out)]
    cwd = Path.cwd()
    try:
        os.chdir(REPO_ROOT)
        result = CliRunner().invoke(main, args)
    finally:
        os.chdir(cwd)
    assert result.exit_code == 0, result.output


def test_deterministic_cli_sboms_match_manifest_digests(tmp_path: Path) -> None:
    """THE acceptance test: uploaded-SBOM digest == manifest-recorded digest,
    per component, with exact file-set equality."""
    out = tmp_path / "sbom"
    _run_sbom_cli(out, deterministic=True)

    manifest = build_manifest(REPO_ROOT, _TAG)
    expected_names = {PurePosixPath(m["sbom"]).name for m in manifest["members"]}
    actual_files = {p.name: p for p in out.glob("*.cdx.json")}

    assert set(actual_files) == expected_names
    for member in manifest["members"]:
        name = PurePosixPath(member["sbom"]).name
        digest = hashlib.sha256(actual_files[name].read_bytes()).hexdigest()
        assert digest == member["sha256"], name


def test_deterministic_cli_sboms_match_bundle_sbom_bytes(tmp_path: Path) -> None:
    """Option (a): the uploaded standalone SBOM set IS the bundle's SBOM set,
    byte-for-byte."""
    out = tmp_path / "sbom"
    _run_sbom_cli(out, deterministic=True)

    bundle_dir = tmp_path / "bundle"
    tar_path, manifest, _fp = build_bundle(REPO_ROOT, _TAG, bundle_dir)

    prefix = f"neutron-platform-{_TAG}"
    cli_files = {p.name: p for p in out.glob("*.cdx.json")}
    with tarfile.open(tar_path, mode="r:gz") as tar:
        for member in manifest["members"]:
            fh = tar.extractfile(f"{prefix}/{member['sbom']}")
            assert fh is not None
            tar_bytes = fh.read()
            name = PurePosixPath(member["sbom"]).name
            assert tar_bytes == cli_files[name].read_bytes(), name


def test_deterministic_cli_output_byte_reproducible(tmp_path: Path) -> None:
    """Determinism regression: same inputs -> identical SBOM bytes -> identical
    digest across repeated runs."""
    out_a = tmp_path / "a"
    out_b = tmp_path / "b"
    _run_sbom_cli(out_a, deterministic=True)
    _run_sbom_cli(out_b, deterministic=True)

    files_a = {p.name: p for p in out_a.glob("*.cdx.json")}
    files_b = {p.name: p for p in out_b.glob("*.cdx.json")}
    assert set(files_a) == set(files_b)
    assert files_a, "expected at least one SBOM"
    for name, path_a in files_a.items():
        bytes_a = path_a.read_bytes()
        bytes_b = files_b[name].read_bytes()
        assert bytes_a == bytes_b, name
        assert hashlib.sha256(bytes_a).hexdigest() == hashlib.sha256(bytes_b).hexdigest()


def test_deterministic_serial_is_uuid5_and_default_stays_uuid4(tmp_path: Path) -> None:
    """No random serial in the verified set: --deterministic serials are uuid5
    and stable; the default (no flag) stays a fresh uuid4 that differs per run."""
    det_a = tmp_path / "det_a"
    det_b = tmp_path / "det_b"
    _run_sbom_cli(det_a, deterministic=True)
    _run_sbom_cli(det_b, deterministic=True)

    det_serials_a = _serials(det_a)
    det_serials_b = _serials(det_b)
    assert det_serials_a, "expected at least one SBOM"
    for name, serial in det_serials_a.items():
        assert _UUID5_URN.match(serial), (name, serial)
        assert det_serials_b[name] == serial, name

    def_a = tmp_path / "def_a"
    def_b = tmp_path / "def_b"
    _run_sbom_cli(def_a, deterministic=False)
    _run_sbom_cli(def_b, deterministic=False)

    def_serials_a = _serials(def_a)
    def_serials_b = _serials(def_b)
    for name, serial in def_serials_a.items():
        assert _UUID4_URN.match(serial), (name, serial)
        assert def_serials_b[name] != serial, name


def _serials(out: Path) -> dict[str, str]:
    serials: dict[str, str] = {}
    for path in out.glob("*.cdx.json"):
        doc = json.loads(path.read_text(encoding="utf-8"))
        serials[path.name] = doc["serialNumber"]
    return serials
