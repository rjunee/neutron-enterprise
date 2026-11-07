# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Ontology lint (NE-085-spike).

Lifts ABI's rdflib-only static BFO conformance lint. The ``ne ontology lint``
CLI verb is wired (NE-ONTOLOGY-LINT-CLI) as a documented alias of ``ne ontology
conformance``; ``python -m ne.brain_ontology.lint`` remains the bare-gate
exit-0/1 surface. A nightly full-graph workflow was evaluated and declined: the
vendored corpus and triage allowlist change only via commits, and every commit
already runs the gate in CI.
"""
