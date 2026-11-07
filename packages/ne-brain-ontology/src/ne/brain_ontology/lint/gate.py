# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Governed full-corpus BFO-conformance gate (NE-085b).

Promotes the lifted :mod:`~ne.brain_ontology.lint.validate_bfo` static lint from
"runs on bare BFO" (085a) to a GOVERNED gate over the WHOLE loaded corpus —
BFO-2020 + CCO-2.1 + the self-authored operational-alignment ontology — plus any
caller-supplied ``extra`` graph (the operational shapes / a planted violation).

The lifted rules encode ABI's CCO-2.0-era expectations; run against CCO-2.1 they
surface ERROR categories that are RULE-vs-CORPUS skew, not operational-ontology
defects (R1/R4). Each such category is TRIAGED in :data:`TRIAGED_ERROR_ALLOWLIST`
with a written one-line justification recording whether the rule or the corpus is
wrong — never a blanket skip. Any ERROR whose category is NOT triaged fails the
gate (:func:`run_corpus_conformance` returns it in ``errors``); WARNINGs are
informational (the 085a precedent). :mod:`ne.brain_ontology.lint.__main__` is the
nightly/CI invocation surface (exit 0 green / 1 untriaged).
"""

from __future__ import annotations

from rdflib import Graph

from ..shapes import load_corpus_graph
from .validate_bfo import LintReport, run_bfo_lint

__all__ = ["TRIAGED_ERROR_ALLOWLIST", "category_of", "run_corpus_conformance"]

#: category name -> one-line rule-vs-corpus justification. Keyed by the bare
#: category (the ``[CATEGORY]`` token without brackets). An ERROR whose category
#: is absent here fails the gate. Every entry below was confirmed against the
#: vendored CCO-2.1 pin: in each case the CORPUS (CUBRC-published, authoritative)
#: is correct and the LIFTED ABI-2.0-era RULE is stale/over-strict.
TRIAGED_ERROR_ALLOWLIST: dict[str, str] = {
    "BUCKET_MAPPING": (
        "RULE STALE: the ABI rule demands every non-BFO class trace to a BFO "
        "'7 Buckets' root via rdfs:subClassOf. CCO-2.1 anchors many mid-level "
        "classes (measurement/frequency/… subclasses) through BFO's own hierarchy "
        "or equivalent-class axioms the flat rule does not follow. The corpus is "
        "authoritative; not a defect of the neop:* alignment (which DOES trace up)."
    ),
    "INHERES_IN": (
        "RULE STALE: the ABI rule requires every role/quality/disposition subclass "
        "to carry an explicit bfo:inheres-in / bearer-of owl:Restriction. CCO-2.1 "
        "anchors most of these at the BFO level rather than per-class. The rule is "
        "stricter than CCO-2.1 modelling practice; the corpus is authoritative."
    ),
    "WRONG_INVERSE": (
        "CORPUS/UPSTREAM: a single CCO-2.1 BFO-relation inverse pair "
        "('is cause of' / 'caused by') uses occurrent domains/ranges the rule's "
        "exact domain=range(inverse) check rejects. This is upstream BFO/CCO "
        "relation modelling, outside NE-085b's mayTouch fence — not ours to fix."
    ),
}


def category_of(error: str) -> str:
    """Extract the bare ``CATEGORY`` from an ``"[CATEGORY] subject: message"`` error."""
    return error.split("]", 1)[0].lstrip("[")


def run_corpus_conformance(extra: Graph | None = None) -> LintReport:
    """Lint the FULL loaded corpus (+``extra``); return UNTRIAGED errors + warnings.

    ``errors`` in the returned report are ONLY the untriaged ones (triaged
    categories are filtered out), so an empty ``errors`` means the gate is green.
    All warnings are preserved (informational). Offline (C2): the corpus is the
    vendored package-data bytes via :func:`load_corpus_graph`; ``extra`` is an
    already-parsed in-memory graph.
    """
    graph = load_corpus_graph()
    if extra is not None:
        graph += extra
    report = run_bfo_lint(graph)
    untriaged = tuple(e for e in report.errors if category_of(e) not in TRIAGED_ERROR_ALLOWLIST)
    return LintReport(errors=untriaged, warnings=report.warnings)
