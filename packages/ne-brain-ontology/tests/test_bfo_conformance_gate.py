# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-085b scope item 2: the governed full-corpus BFO-conformance gate."""

from __future__ import annotations

import subprocess
import sys

from rdflib import OWL, RDF, Graph, Namespace

from ne.brain_ontology.lint.gate import (
    TRIAGED_ERROR_ALLOWLIST,
    category_of,
    run_corpus_conformance,
)


def test_full_corpus_gate_is_green_with_every_error_category_justified(no_network: None) -> None:
    report = run_corpus_conformance()
    # Zero UNTRIAGED error categories — the gate is green over BFO+CCO+alignment.
    assert report.errors == (), f"untriaged BFO-conformance errors: {report.errors}"
    # R4 discipline: every allowlisted category carries a written, non-empty
    # rule-vs-corpus justification (visible triage, never a silent skip).
    assert TRIAGED_ERROR_ALLOWLIST, "the allowlist must be explicit, not empty"
    for category, justification in TRIAGED_ERROR_ALLOWLIST.items():
        assert category == category.upper()
        assert len(justification) > 30, category


def test_planted_inverse_of_itself_violation_turns_the_gate_red(no_network: None) -> None:
    # NON-VACUITY: an owl:inverseOf-itself property (a category NOT naturally
    # present in the vendored corpus, so NOT allowlisted) surfaces as an untriaged
    # ERROR — proving the gate is not vacuously green.
    ex = Namespace("http://example.org/planted#")
    extra = Graph()
    extra.add((ex.selfInverse, RDF.type, OWL.ObjectProperty))
    extra.add((ex.selfInverse, OWL.inverseOf, ex.selfInverse))
    report = run_corpus_conformance(extra)
    categories = {category_of(e) for e in report.errors}
    assert "INVERSE_OF_ITSELF" in categories, report.errors
    assert "INVERSE_OF_ITSELF" not in TRIAGED_ERROR_ALLOWLIST


def test_python_m_lint_exits_zero_on_clean_corpus(no_network: None) -> None:
    # The nightly/CI invocation surface exits 0 green on the clean corpus.
    result = subprocess.run(
        [sys.executable, "-m", "ne.brain_ontology.lint"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "GATE PASSED" in result.stdout
