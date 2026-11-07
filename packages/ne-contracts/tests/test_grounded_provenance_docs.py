# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-GA-G5: docs-regression pin for the reconciled G4 P2 GroundedProvenance docstrings.

Docstring-only change (no field/behavior change, no version bump): the
``fallback``/``assist_used`` field docs now match the MERGED G4 runtime
semantics exactly (``ne.runtime.grounding`` §4/``run_grounding_gate``) —
``assist_used`` means the assist's output was ADOPTED (not merely that it
ran), and ``fallback`` combined with ``assist_used`` is the clean 4-way
encoding. This test pins the reconciled wording so it cannot silently regress
back to the ambiguous pre-G5 phrasing.
"""

from __future__ import annotations

from ne.contracts.runtime import GroundedProvenance


def test_grounded_provenance_docstring_pins_four_way_encoding() -> None:
    doc = GroundedProvenance.__doc__
    assert doc is not None
    assert "ADOPTED" in doc
    assert "assist_used=False" in doc
    assert "assist_used=True" in doc
    assert "deterministic verified-table fallback" in doc
