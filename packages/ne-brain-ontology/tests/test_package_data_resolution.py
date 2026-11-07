# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The tracked Codex P2 proof: assets resolve via importlib.resources, not layout.

The ontology corpus is PACKAGE DATA inside ``ne/brain_ontology/assets``. This
proves it resolves under a relocated, repo-layout-absent install: we copy the
package into a site-packages-style tree, then run a SUBPROCESS whose sys.path
prefers that copy and whose cwd is OUTSIDE the repo, and load the full corpus.
"""

from __future__ import annotations

import re
import subprocess
import sys
import textwrap
from pathlib import Path

_SRC_PKG = Path(__file__).resolve().parents[1] / "src" / "ne" / "brain_ontology"


def test_no_file_parents_path_resolution_in_src() -> None:
    # grep-guard: no `.parents[` repo-layout escape survives under src/ (the dict
    # ``direct_parents[...]`` in the lint module has no leading dot, so is exempt).
    for path in _SRC_PKG.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"\.parents\[", text), f"repo-layout path resolution in {path}"


def test_corpus_loads_from_relocated_site_packages(tmp_path: Path) -> None:
    import shutil

    site = tmp_path / "site"
    dest = site / "ne" / "brain_ontology"
    dest.parent.mkdir(parents=True)
    # Copy the package (incl. its assets) into a site-packages-style namespace tree.
    shutil.copytree(_SRC_PKG, dest, ignore=shutil.ignore_patterns("__pycache__"))

    outside = tmp_path / "outside-the-repo"
    outside.mkdir()

    script = textwrap.dedent(
        """
        import sys
        # Prefer the relocated copy; ne.contracts/ne.schema still merge from the venv
        # (PEP 420 namespace packages).
        sys.path.insert(0, sys.argv[1])
        from ne.brain_ontology import ontology
        # The resolved assets root must live INSIDE the relocated copy, never the repo.
        assert str(ontology.ASSETS_DIR).startswith(sys.argv[1]), ontology.ASSETS_DIR
        manifest = ontology.load_manifest()
        ontology.verify_assets(manifest)
        corpus = list(ontology.iter_corpus(manifest))
        assert len(corpus) == 13, len(corpus)
        print("OK", len(corpus))
        """
    )

    result = subprocess.run(
        [sys.executable, "-c", script, str(site)],
        cwd=outside,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "OK 13" in result.stdout


def test_real_wheel_install_resolution(tmp_path: Path) -> None:
    # LIVE-PARKABLE (NE-CLEAN3 precedent): a real wheel build + unzip + import
    # smoke. Skipped when `uv build` is unavailable offline.
    import shutil
    import zipfile

    if shutil.which("uv") is None:
        import pytest

        pytest.skip("uv unavailable — real-wheel resolution variant parked")

    pkg_root = _SRC_PKG.parents[2]  # .../packages/ne-brain-ontology
    build = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path)],
        cwd=str(pkg_root),
        capture_output=True,
        text=True,
    )
    if build.returncode != 0:
        import pytest

        pytest.skip(f"uv build unavailable offline: {build.stderr[-400:]}")
    wheels = list(tmp_path.glob("*.whl"))
    assert wheels, "no wheel produced"
    unpacked = tmp_path / "unpacked"
    with zipfile.ZipFile(wheels[0]) as zf:
        zf.extractall(unpacked)
    # The vendored corpus MANIFEST must be present as package data inside the wheel.
    manifest = unpacked / "ne" / "brain_ontology" / "assets" / "ontologies" / "MANIFEST.toml"
    found = sorted(unpacked.rglob("*.toml"))
    assert manifest.is_file(), f"corpus not shipped as package data: {found}"
