# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Permanent guardian for the vendored Qdrant/bm25 FastEmbed assets (NE-CKC9).

The BM25 model assets are vendored in-repo (HF-cache layout) so Checkpoint-C
loads them fully offline — immune to GH Actions cache eviction and HuggingFace
429s. These tests assert the vendored tree stays structurally intact and that a
REAL ``SparseTextEmbedding('Qdrant/bm25')`` load + embed succeeds with
``HF_HUB_OFFLINE=1`` and zero network path, so the assets can never silently rot.
"""

from __future__ import annotations

import shutil
from pathlib import Path

_MODEL_DIR = Path(__file__).resolve().parents[1] / "assets" / "fastembed" / "models--Qdrant--bm25"


def test_vendored_tree_resolves_ref_to_snapshot() -> None:
    ref = (_MODEL_DIR / "refs" / "main").read_text(encoding="utf-8").strip()
    snap = _MODEL_DIR / "snapshots" / ref
    assert (snap / "config.json").is_file()
    assert (snap / "english.txt").is_file()


def test_vendored_tree_has_no_symlinks() -> None:
    assert not any(p.is_symlink() for p in _MODEL_DIR.rglob("*"))


def test_bm25_loads_offline_from_vendored_assets(tmp_path, monkeypatch) -> None:
    shutil.copytree(_MODEL_DIR, tmp_path / "cache" / "models--Qdrant--bm25")
    monkeypatch.setenv("FASTEMBED_CACHE_PATH", str(tmp_path / "cache"))
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    from fastembed import SparseTextEmbedding

    emb = list(SparseTextEmbedding("Qdrant/bm25").embed(["neutron enterprise offline bm25"]))
    assert emb and len(emb[0].indices) > 0
