# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The LinkML→SHACL artifact (NE-085a): operational.shapes.ttl.

Asserts the committed shapes parse as Turtle and materialize real constraints on
the current operational entity classes. ``test_generated_freshness`` separately
proves deterministic source parity for this and every other generated artifact.
"""

from __future__ import annotations

from pathlib import Path

from rdflib import RDF, Graph, Namespace

from ne.schema import codegen

_PKG = Path(__file__).resolve().parents[1]
_GENERATED = _PKG / "src" / "ne" / "schema" / "generated"
_ARTIFACT = _GENERATED / "operational.shapes.ttl"

SH = Namespace("http://www.w3.org/ns/shacl#")
NEOP = Namespace("https://neutron.enterprise/schema/operational/")


def test_shacl_artifact_enrolled_in_registry() -> None:
    assert "operational.shapes.ttl" in codegen.ARTIFACTS


def test_artifact_parses_as_turtle() -> None:
    graph = Graph()
    graph.parse(str(_ARTIFACT), format="turtle")
    assert len(graph) > 0


def test_has_nodeshape_with_targetclass_on_entity() -> None:
    graph = Graph()
    graph.parse(str(_ARTIFACT), format="turtle")
    # ≥1 sh:NodeShape whose sh:targetClass is an operational entity class.
    assert NEOP.Person in set(graph.objects(None, SH.targetClass))
    assert (NEOP.Person, RDF.type, SH.NodeShape) in graph


def test_has_shapes_for_exact_meridian_entity_set() -> None:
    graph = Graph()
    graph.parse(str(_ARTIFACT), format="turtle")
    targets = set(graph.objects(None, SH.targetClass))
    assert {
        NEOP.Customer,
        NEOP.AutoLoan,
        NEOP.PaymentSchedule,
        NEOP.ServicingNote,
    } <= targets


def test_meridian_relationship_shapes_preserve_targets_and_requiredness() -> None:
    graph = Graph()
    graph.parse(str(_ARTIFACT), format="turtle")

    auto_loan_shapes = set(graph.subjects(SH.path, NEOP.has_auto_loan))
    payment_shapes = set(graph.subjects(SH.path, NEOP.has_payment_schedule))
    note_shapes = set(graph.subjects(SH.path, NEOP.has_servicing_note))

    assert len(auto_loan_shapes) == 1
    assert {graph.value(shape, SH["class"]) for shape in auto_loan_shapes} == {NEOP.AutoLoan}
    assert not any(graph.value(shape, SH.minCount) is not None for shape in auto_loan_shapes)

    assert len(payment_shapes) == 1
    assert {graph.value(shape, SH["class"]) for shape in payment_shapes} == {NEOP.PaymentSchedule}
    assert all(graph.value(shape, SH.minCount) is not None for shape in payment_shapes)

    assert len(note_shapes) == 2
    assert {graph.value(shape, SH["class"]) for shape in note_shapes} == {NEOP.ServicingNote}
    assert sum(graph.value(shape, SH.minCount) is not None for shape in note_shapes) == 1


def test_has_materialized_constraint() -> None:
    graph = Graph()
    graph.parse(str(_ARTIFACT), format="turtle")
    # ≥1 materialized constraint: a required-slot (minCount) OR a datatype.
    assert any(graph.triples((None, SH.minCount, None)))
    assert any(graph.triples((None, SH.datatype, None)))
