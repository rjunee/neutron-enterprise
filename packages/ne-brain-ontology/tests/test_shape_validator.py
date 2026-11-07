# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""PyShaclValidator tests (NE-085-spike), incl. the C2 owl:imports no-fetch proof."""

from __future__ import annotations

import asyncio

import pytest

from ne.brain_ontology.shapes import PyShaclValidator

from .conftest import PERSON_SHAPE

_RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
_PERSON = "http://example.org/Person"

_SHAPES = PERSON_SHAPE.read_bytes()


def test_conforming_triples_mapping() -> None:
    validator = PyShaclValidator(_SHAPES)
    report = asyncio.run(
        validator.validate(
            {
                "triples": [
                    ("http://example.org/alice", _RDF_TYPE, _PERSON),
                    ("http://example.org/alice", _LABEL, "Alice"),
                ]
            }
        )
    )
    assert report.conforms is True
    assert report.violations == ()


def test_violating_data_reports_violations() -> None:
    validator = PyShaclValidator(_SHAPES)
    report = asyncio.run(
        validator.validate({"triples": [("http://example.org/bob", _RDF_TYPE, _PERSON)]})
    )
    assert report.conforms is False
    assert report.violations
    for violation in report.violations:
        # "<focusNode> <sourceShape> <message>" — at least three parts, IRIs/shapes only.
        assert len(violation.split(" ", 2)) == 3
        assert "http://example.org/bob" in violation


def test_serialized_turtle_input() -> None:
    validator = PyShaclValidator(_SHAPES)
    turtle = """
    @prefix ex: <http://example.org/> .
    @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
    ex:carol a ex:Person ; rdfs:label "Carol" .
    """
    report = asyncio.run(validator.validate({"data": turtle, "format": "turtle"}))
    assert report.conforms is True


def test_invalid_mapping_raises() -> None:
    validator = PyShaclValidator(_SHAPES)
    with pytest.raises(ValueError, match="exactly one"):
        asyncio.run(validator.validate({}))
    with pytest.raises(ValueError, match="exactly one"):
        asyncio.run(
            validator.validate({"triples": [], "data": "ex:x a ex:Person .", "format": "turtle"})
        )


def test_validator_error_propagates() -> None:
    # A malformed shapes graph must raise, NEVER degrade to conforms=True. The
    # parse error surfaces at construction; either way it propagates as a denial.
    with pytest.raises(Exception):  # noqa: B017 - any parser/validation error is a denial
        validator = PyShaclValidator(b"this is not valid turtle @@@")
        asyncio.run(validator.validate({"triples": [("http://example.org/x", _RDF_TYPE, _PERSON)]}))


def test_owl_imports_never_fetched(no_network: None) -> None:
    # The ont graph declares a remote owl:imports; with do_owl_imports=False and
    # sockets disabled, validate() must still complete offline (C2 leak-path 1).
    ont = b"""
    @prefix owl: <http://www.w3.org/2002/07/owl#> .
    <http://example.org/onto> a owl:Ontology ;
        owl:imports <http://example.org/remote.owl> .
    """
    validator = PyShaclValidator(_SHAPES, ont=ont)
    report = asyncio.run(
        validator.validate(
            {
                "triples": [
                    ("http://example.org/dave", _RDF_TYPE, _PERSON),
                    ("http://example.org/dave", _LABEL, "Dave"),
                ]
            }
        )
    )
    assert report.conforms is True
