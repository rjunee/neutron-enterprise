# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-085b scope item 1: offline corpus+alignment startup wiring (sync_ontology_corpus)."""

from __future__ import annotations

import asyncio

from ne.brain_ontology import OntologySettings, build_oxigraph_store
from ne.brain_ontology.ontology import load_manifest
from ne.brain_ontology.shapes import load_compiled_shapes

_ALIGNMENT_GRAPH = "urn:ne:ontology:ne:operational-alignment"


def test_sync_loads_bfo_cco_alignment_then_double_sync_is_noop(no_network: None) -> None:
    async def scenario() -> None:
        store = build_oxigraph_store(OntologySettings(include_ontology_graph=True))
        report = await store.sync_ontology_corpus()
        # 1 BFO + 11 CCO + 1 NE alignment.
        assert report.loaded == 13
        assert report.replaced == 0
        assert report.skipped == 0
        # The alignment graph is reachable through the steward sparql over GRAPH ?g.
        rows = await store.sparql("SELECT DISTINCT ?g WHERE { GRAPH ?g { ?s ?p ?o } }")
        graphs = {row["g"] for row in rows}
        assert _ALIGNMENT_GRAPH in graphs
        assert any(g.startswith("urn:ne:ontology:bfo:") for g in graphs)
        assert sum(g.startswith("urn:ne:ontology:cco:") for g in graphs) == 11
        # A second sync is a zero-write no-op (idempotent).
        report2 = await store.sync_ontology_corpus()
        assert report2.loaded == 0
        assert report2.replaced == 0
        assert report2.skipped == 13

    asyncio.run(scenario())


def test_alignment_classes_typed_under_cco(no_network: None) -> None:
    async def scenario() -> None:
        store = build_oxigraph_store(OntologySettings(include_ontology_graph=True))
        await store.sync_ontology_corpus()
        # neop:Account subclasses the CCO Organization class extracted from the corpus.
        rows = await store.sparql(
            "ASK { <https://neutron.enterprise/schema/operational/Account> "
            "<http://www.w3.org/2000/01/rdf-schema#subClassOf> "
            "<https://www.commoncoreontologies.org/ont00001180> }"
        )
        assert rows == [{"ask": True}]

    asyncio.run(scenario())


def test_ontology_version_surfaces_pins_and_shapes_hash(no_network: None) -> None:
    store = build_oxigraph_store(OntologySettings())
    version = store.ontology_version()
    manifest = load_manifest()
    assert set(version.sources) == {"bfo", "cco", "ne"}
    # Each source surfaces pin_commit when present, else pin (bfo has no separate
    # pin_commit; cco does; the self-authored alignment carries only its pin).
    for name, source in manifest.sources.items():
        assert version.sources[name] == (source.pin_commit or source.pin)
    assert version.sources["cco"] == manifest.sources["cco"].pin_commit
    assert version.sources["ne"] == manifest.sources["ne"].pin
    assert version.file_sha256["ne/operational-alignment.ttl"]
    assert version.shapes_hash == load_compiled_shapes().shapes_hash


def test_alignment_manifest_guardian_holds(no_network: None) -> None:
    # The [ne] source is picked up by the manifest iteration + its sha256/triples
    # guard (the permanent vendored-assets guardian covers it — this asserts the
    # new source is present and integral).
    from ne.brain_ontology.ontology import ASSETS_DIR, iter_corpus, verify_assets

    manifest = load_manifest()
    verify_assets(manifest)  # raises on sha256/existence drift
    entry = manifest.sources["ne"].files[0]
    assert entry.local_path == "ne/operational-alignment.ttl"
    from rdflib import Graph

    graph = Graph()
    graph.parse(data=(ASSETS_DIR / entry.local_path).read_bytes(), format="turtle")
    assert len(graph) == entry.triples
    # The alignment graph IRI is inside the sync writer's containment namespace.
    iris = {iri for iri, _p, _b in iter_corpus(manifest)}
    assert _ALIGNMENT_GRAPH in iris
