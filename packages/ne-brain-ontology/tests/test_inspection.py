# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Focused parity tests for the read-only active-model inspection projection."""

from __future__ import annotations

import hashlib
from importlib.resources import files

import pytest

from ne.brain_ontology import inspection
from ne.brain_ontology.inspection import ProjectionIntegrityError
from ne.brain_ontology.shapes import load_compiled_shapes
from ne.schema.generated import operational
from ne.schema.generated._manifest import LINKML_VERSION, SCHEMA_VERSION, SOURCE_SHA256
from ne.schema.generated.operational_graph import EDGE_DEFS


def test_projection_names_generated_model_and_fingerprints() -> None:
    projected = inspection.build_active_model_projection()
    schema_bytes = files("ne.schema.generated").joinpath("operational.schema.json").read_bytes()

    assert projected.provenance.title == "Neutron Enterprise Operational Schema"
    assert projected.provenance.schema_id == operational.linkml_meta.root["id"]
    assert projected.provenance.schema_version == SCHEMA_VERSION
    assert projected.provenance.linkml_version == LINKML_VERSION
    assert projected.provenance.source_sha256 == SOURCE_SHA256
    assert projected.provenance.json_schema_sha256 == hashlib.sha256(schema_bytes).hexdigest()
    assert projected.provenance.shapes_sha256 == load_compiled_shapes().shapes_hash


def test_projection_discovers_every_generated_concept() -> None:
    projected = inspection.build_active_model_projection()
    expected = {
        name
        for name, value in vars(operational).items()
        if isinstance(value, type)
        and value is not operational.ConfiguredBaseModel
        and issubclass(value, operational.ConfiguredBaseModel)
    }

    assert {concept.name for concept in projected.concepts} == expected
    assert {concept.name for concept in projected.concepts} >= {
        "Account",
        "Person",
        "Document",
        "Ticket",
        "Order",
        "OperationalRecordSet",
    }
    assert projected.concept_count == len(expected)


def test_projection_fields_match_pydantic_requiredness_and_enums() -> None:
    projected = inspection.build_active_model_projection()
    concept_types = {
        name: value
        for name, value in vars(operational).items()
        if isinstance(value, type)
        and value is not operational.ConfiguredBaseModel
        and issubclass(value, operational.ConfiguredBaseModel)
    }

    for concept in projected.concepts:
        model_type = concept_types[concept.name]
        assert {field.name for field in concept.fields} == set(model_type.model_fields)
        for field in concept.fields:
            assert field.required is model_type.model_fields[field.name].is_required()

    document = next(concept for concept in projected.concepts if concept.name == "Document")
    doc_type = next(field for field in document.fields if field.name == "doc_type")
    assert doc_type.required is True
    assert doc_type.enum_values == (
        "contract",
        "invoice",
        "report",
        "policy",
        "presentation",
        "correspondence",
        "other",
    )


def test_projection_relationships_match_edge_defs() -> None:
    projected = inspection.build_active_model_projection()
    projected_edges = {
        (
            relation.kind,
            relation.source_concept,
            relation.field_name,
            relation.target_concept,
            relation.multivalued,
            relation.required,
        )
        for relation in projected.relationships
        if relation.kind != "CONTAINS"
    }
    assert projected_edges == set(EDGE_DEFS)

    record_set = next(
        concept for concept in projected.concepts if concept.name == "OperationalRecordSet"
    )
    contains = [relation for relation in projected.relationships if relation.kind == "CONTAINS"]
    assert {(relation.field_name, relation.target_concept) for relation in contains} == {
        (field.name, field.related_concepts[0]) for field in record_set.fields
    }


def test_projection_rules_match_compiled_shapes_and_hash() -> None:
    projected = inspection.build_active_model_projection()
    compiled = load_compiled_shapes()

    assert projected.rules
    assert {rule.scope for rule in projected.rules} == {"per-write", "global"}
    assert {rule.source for rule in projected.rules} == {"generated", "curated"}
    assert {rule.artifact_sha256 for rule in projected.rules} == {compiled.shapes_hash}
    assert all(rule.label and rule.purpose and rule.condition for rule in projected.rules)
    assert len({rule.rule_id for rule in projected.rules}) == len(projected.rules)


def test_projection_rule_labels_do_not_derive_from_raw_iris() -> None:
    projected = inspection.build_active_model_projection()
    curated = [rule for rule in projected.rules if rule.source == "curated"]

    assert curated
    assert any(rule.label == "Labels are unique" for rule in curated)
    assert all("UniqueLabelShape" not in rule.label for rule in curated)
    assert all("IdentityAssertionShape" not in rule.label for rule in curated)
    assert all("same_as_" not in rule.label for rule in curated)


def test_projection_rejects_unlabelled_curated_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    labels = dict(inspection._CURATED_SHAPE_LABELS)
    labels.pop("urn:ne:shapes:global#UniqueLabelShape")
    monkeypatch.setattr(inspection, "_CURATED_SHAPE_LABELS", labels)

    with pytest.raises(ProjectionIntegrityError, match="presentation metadata"):
        inspection.build_active_model_projection()


def test_projection_order_is_deterministic_and_load_is_cached() -> None:
    first = inspection.build_active_model_projection()
    second = inspection.build_active_model_projection()
    assert first == second
    assert [concept.name for concept in first.concepts] == sorted(
        concept.name for concept in first.concepts
    )
    assert [relation for relation in first.relationships] == sorted(
        first.relationships,
        key=lambda item: (item.source_concept, item.field_name, item.target_concept, item.kind),
    )

    inspection.load_active_model_projection.cache_clear()
    cached = inspection.load_active_model_projection()
    assert inspection.load_active_model_projection() is cached
