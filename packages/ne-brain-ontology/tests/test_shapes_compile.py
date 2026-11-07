# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Compiled-shapes loading + the per-write/global scope split (NE-085a)."""

from __future__ import annotations

from rdflib import Graph, Namespace

from ne.brain_ontology.shapes import CompiledShapes, load_compiled_shapes

SH = Namespace("http://www.w3.org/ns/shacl#")


def test_compiled_shapes_load_and_parse() -> None:
    compiled = load_compiled_shapes()
    assert isinstance(compiled, CompiledShapes)
    per_write = compiled.per_write_graph()
    global_graph = compiled.global_graph()
    # Generated operational shapes present in per-write.
    assert any(per_write.triples((None, SH.targetClass, None)))
    # The seed global shape parses and carries an sh:sparql constraint.
    assert any(global_graph.triples((None, SH.sparql, None)))


def test_shapes_hash_stable_across_calls() -> None:
    assert load_compiled_shapes().shapes_hash == load_compiled_shapes().shapes_hash


def test_r2_meta_check_per_write_shapes_have_no_sparql() -> None:
    # R2 (brain-tier3-spec §5.5): a per-write-scoped shape graph MUST be delta-safe
    # — zero sh:sparql predicates (global/non-local constraints belong in the
    # global lane, evaluated only in the nightly full-graph job).
    per_write = load_compiled_shapes().per_write_graph()
    assert not any(per_write.triples((None, SH.sparql, None)))


def test_planting_sparql_in_per_write_fails_the_meta_check() -> None:
    # Prove the meta-check bites: a per-write graph with an sh:sparql predicate
    # would fail the R2 assertion above.
    poisoned = Graph()
    poisoned.parse(
        data="""
        @prefix sh: <http://www.w3.org/ns/shacl#> .
        <urn:s> a sh:NodeShape ; sh:sparql [ sh:select "SELECT $this WHERE {}" ] .
        """,
        format="turtle",
    )
    assert any(poisoned.triples((None, SH.sparql, None)))


def test_global_shape_excluded_from_per_write_validator() -> None:
    compiled = load_compiled_shapes()
    per_write = compiled.per_write_graph()
    global_graph = compiled.global_graph()
    # The unique-label global shape node is NOT in the per-write graph.
    global_shapes = set(global_graph.subjects(None, SH.NodeShape))
    per_write_shapes = set(per_write.subjects(None, SH.NodeShape))
    assert global_shapes  # there is at least one global shape
    assert global_shapes.isdisjoint(per_write_shapes)
