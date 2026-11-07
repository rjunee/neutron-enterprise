# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Nightly/CI invocation surface for the BFO conformance gate (NE-085b).

``python -m ne.brain_ontology.lint`` runs :func:`run_corpus_conformance` over the
full loaded corpus, prints the report, and exits 0 (green) / 1 (untriaged
ERRORs). This replaces the out-of-mayTouch ``ne ontology lint`` CLI verb in-unit
(flagged as an orchestrator rider). Offline (C2) — no arguments, no network.
"""

from __future__ import annotations

import sys

from .gate import TRIAGED_ERROR_ALLOWLIST, run_corpus_conformance


def main() -> int:
    report = run_corpus_conformance()
    for warning in report.warnings:
        print(f"WARNING {warning}")
    if TRIAGED_ERROR_ALLOWLIST:
        print(f"triaged ERROR categories (allowlisted): {sorted(TRIAGED_ERROR_ALLOWLIST)}")
    if report.errors:
        print(f"GATE FAILED: {len(report.errors)} untriaged BFO-conformance ERROR(s):")
        for error in report.errors:
            print(f"  ERROR {error}")
        return 1
    print("GATE PASSED: zero untriaged BFO-conformance errors over the full corpus.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
