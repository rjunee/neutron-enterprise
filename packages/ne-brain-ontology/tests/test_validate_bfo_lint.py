# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Lifted BFO lint runs on the vendored BFO (SPEC-row mandate) + non-vacuity proof."""

from __future__ import annotations

from rdflib import OWL, RDF, Graph, Namespace, URIRef

from ne.brain_ontology.lint.validate_bfo import run_bfo_lint

from .conftest import BFO_TTL

# BFO-2020 at the vendored pin produces ZERO ERROR-severity lint issues under the
# lifted rule set (the WARNING-severity annotation/naming notes are informational,
# not gating). No triaged allowlist is required. If a future BFO re-pin introduces
# an ERROR, add its category here with a one-line rule-vs-corpus justification
# (e.g. "rule encodes a CCO-2.0-era expectation") — never a silent skip.
_TRIAGED_ERROR_ALLOWLIST: frozenset[str] = frozenset()


def _category(error: str) -> str:
    # errors are formatted "[CATEGORY] subject: message".
    return error.split("]", 1)[0].lstrip("[")


def test_lint_runs_on_vendored_bfo() -> None:
    graph = Graph()
    graph.parse(data=BFO_TTL.read_bytes(), format="turtle")
    report = run_bfo_lint(graph)
    unexpected = [e for e in report.errors if _category(e) not in _TRIAGED_ERROR_ALLOWLIST]
    assert unexpected == [], f"untriaged BFO lint errors: {unexpected}"


def test_lint_flags_planted_violation() -> None:
    # A property declared owl:inverseOf itself is an INVERSE_OF_ITSELF ERROR —
    # proves the lint is not vacuously green.
    ex = Namespace("http://example.org/")
    graph = Graph()
    graph.add((ex.selfInverse, RDF.type, OWL.ObjectProperty))
    graph.add((ex.selfInverse, OWL.inverseOf, ex.selfInverse))
    report = run_bfo_lint(graph)
    categories = {_category(e) for e in report.errors}
    assert "INVERSE_OF_ITSELF" in categories, report.errors


def test_planted_violation_subject_is_the_property() -> None:
    ex = Namespace("http://example.org/")
    graph = Graph()
    prop: URIRef = ex.selfInverse
    graph.add((prop, RDF.type, OWL.ObjectProperty))
    graph.add((prop, OWL.inverseOf, prop))
    report = run_bfo_lint(graph)
    assert any("INVERSE_OF_ITSELF" in e and "selfInverse" in e for e in report.errors)
