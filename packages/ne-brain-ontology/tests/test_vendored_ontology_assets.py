# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Permanent guardian for the vendored BFO/CCO ontology assets (NE-085-spike).

Precedent: packages/ne-brain/tests/test_vendored_bm25_assets.py. These tests
assert the vendored corpus stays structurally intact — sha256 + exact triple
counts match the manifest, no symlinks, no unmanifested files, and the whole
corpus parses fully offline — so the pinned artifacts can never silently rot or
be truncated.
"""

from __future__ import annotations

import hashlib

from rdflib import Graph

from ne.brain_ontology.ontology import ASSETS_DIR, load_manifest

_MANIFEST = load_manifest()


def test_manifest_hashes_match_files() -> None:
    for source in _MANIFEST.sources.values():
        for entry in source.files:
            path = ASSETS_DIR / entry.local_path
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            assert digest == entry.sha256, f"sha256 drift: {entry.local_path}"


def test_no_symlinks_under_assets() -> None:
    assert not any(p.is_symlink() for p in ASSETS_DIR.rglob("*"))


def test_no_unmanifested_files() -> None:
    manifested = {
        (ASSETS_DIR / entry.local_path).resolve()
        for source in _MANIFEST.sources.values()
        for entry in source.files
    }
    on_disk = {p.resolve() for p in ASSETS_DIR.rglob("*.ttl")}
    assert on_disk == manifested, (
        f"unmanifested: {on_disk - manifested}; missing: {manifested - on_disk}"
    )
    # Every manifest entry points at a real file (the other direction).
    for path in manifested:
        assert path.is_file()


def test_corpus_loads_offline_with_exact_triple_counts(no_network: None) -> None:
    for source in _MANIFEST.sources.values():
        for entry in source.files:
            path = ASSETS_DIR / entry.local_path
            graph = Graph()
            graph.parse(data=path.read_bytes(), format="turtle")
            assert len(graph) == entry.triples, (
                f"triple-count drift for {entry.local_path}: "
                f"manifest={entry.triples} actual={len(graph)}"
            )
