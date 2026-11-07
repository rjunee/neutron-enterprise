# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""P2-22: the vendored-corpus integrity gate (``verify_assets``) runs on the
RUNTIME load path (``sync_ontology_corpus``), not just in tests.

A byte-tampered vendored TTL must make ``sync_ontology_corpus`` raise ``sha256
mismatch`` and load ZERO graphs into the schema store — fail-closed BEFORE any
TTL reaches the graphs (C11 byte-matched posture, enforced at runtime).
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from ne.brain_ontology.config import OntologySettings
from ne.brain_ontology.gateway import build_oxigraph_store
from ne.brain_ontology.ontology import ASSETS_DIR, load_manifest


def test_corrupted_vendored_ttl_fails_closed_before_load(
    no_network: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A full copy of the vendored corpus (incl. MANIFEST.toml) we can tamper.
    ontologies = tmp_path / "ontologies"
    shutil.copytree(ASSETS_DIR, ontologies)
    tampered = ontologies / "ne" / "operational-alignment.ttl"
    tampered.write_bytes(tampered.read_bytes() + b"\n# tampered\n")

    manifest = load_manifest(ontologies / "MANIFEST.toml")
    monkeypatch.setattr("ne.brain_ontology.gateway.load_manifest", lambda: manifest)

    store = build_oxigraph_store(OntologySettings())
    with pytest.raises(ValueError, match="sha256 mismatch"):
        asyncio.run(store.sync_ontology_corpus())

    # Fail-closed: NOTHING loaded (the tampered corpus never reached the graphs).
    loaded = asyncio.run(store.sparql("SELECT DISTINCT ?g WHERE { GRAPH ?g { ?s ?p ?o } }"))
    assert loaded == []
