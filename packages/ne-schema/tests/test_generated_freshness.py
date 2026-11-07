# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The CI staleness gate: committed artifacts are byte-reproducible from the
LinkML source, generation is deterministic, and drift is detected.

These tests build/reuse the isolated hash-pinned codegen venv (SPEC §2 C4/C11).
To keep the suite fast the fake-repo-root case symlinks the already-built real
codegen venv instead of installing a second copy.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from ne.schema import codegen

_PKG = Path(__file__).resolve().parents[1]


def _repo_root() -> Path:
    # tests/ -> ne-schema -> packages -> <repo root>
    return _PKG.parents[1]


def test_committed_tree_is_fresh() -> None:
    assert codegen.check_freshness(_repo_root()) == []


def test_generation_is_deterministic(tmp_path: Path) -> None:
    root = _repo_root()
    out_a = tmp_path / "a"
    out_b = tmp_path / "b"
    codegen.generate(root, out_a)
    codegen.generate(root, out_b)
    for name in codegen.ARTIFACTS:
        assert (out_a / name).read_bytes() == (out_b / name).read_bytes()


def test_stale_artifact_is_detected(tmp_path: Path) -> None:
    real_pkg = _PKG
    fake_pkg = tmp_path / "packages" / "ne-schema"
    fake_pkg.parent.mkdir(parents=True)

    # Copy the package sources, but not the (heavy, non-relocatable) venv or caches.
    shutil.copytree(
        real_pkg,
        fake_pkg,
        ignore=shutil.ignore_patterns(".codegen-venv", "__pycache__"),
    )
    # Reuse the already-built real codegen venv so we don't reinstall 88 wheels.
    (fake_pkg / ".codegen-venv").symlink_to(real_pkg / ".codegen-venv")

    # Minimal workspace marker so root-relative resolution has a valid root.
    (tmp_path / "pyproject.toml").write_text(
        "[tool.uv.workspace]\nmembers = ['packages/*']\n", encoding="utf-8"
    )

    # Baseline: the copied tree is fresh.
    assert codegen.check_freshness(tmp_path) == []

    # Perturb a committed artifact -> it must be reported stale.
    operational = fake_pkg / "src" / "ne" / "schema" / "generated" / "operational.py"
    operational.write_text(
        operational.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8"
    )
    assert "operational.py" in codegen.check_freshness(tmp_path)

    # The staleness gate covers the SECOND schema's artifacts too: edit an events
    # artifact and delete another — both must be reported stale.
    generated = fake_pkg / "src" / "ne" / "schema" / "generated"
    events_py = generated / "events.py"
    events_py.write_text(events_py.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8")
    (generated / "_events_manifest.py").unlink()
    stale = codegen.check_freshness(tmp_path)
    assert "events.py" in stale
    assert "_events_manifest.py" in stale


def test_artifacts_cover_both_schemas() -> None:
    # The registry-derived ARTIFACTS tuple must enumerate all committed files,
    # including the additive Tier-2 entity-graph contracts (NE-081,
    # operational_graph.py — enrolled via SchemaSpec.graph_filename).
    assert set(codegen.ARTIFACTS) == {
        "operational.py",
        "operational.schema.json",
        "operational_graph.py",
        "operational.shapes.ttl",
        "_manifest.py",
        "events.py",
        "events.schema.json",
        "_events_manifest.py",
    }
