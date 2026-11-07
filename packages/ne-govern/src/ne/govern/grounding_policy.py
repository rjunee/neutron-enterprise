# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Strictness-tier policy for the grounding gate (NE-HGATE-1): the PURE,
post-hoc mapping from a tier-agnostic :class:`~ne.govern.grounding.GroundingVerdict`
to a withhold/advisory decision (design §2.5/§3.1).

**The load-bearing law (design §0-law-5 / §2.1): facts are verified at EVERY
tier.** The six fact classes — ``numeral-ungrounded``, ``numeral-unparseable``,
``iri-ungrounded``, ``citation-out-of-range``, ``phrase-recombination`` (which
fabricates *entity identity* from grounded fragments), and
``relation-ungrounded`` (NE-GROUND-REL: a row-bound prose token whose asserted
relation no fetched row supports — the semantic row binding that closes the
``"Alice is closed."`` recombination) — plus ``vocab-ungrounded-factual`` (an
ungrounded non-lexicon token inside a fact-bearing sentence, where a
fabricated entity does its damage) withhold UNCONDITIONALLY at every tier.
The strictness knob ONLY ever moves non-factual vocabulary:
``vocab-extended`` (relational/quantified phrasing) and
``vocab-ungrounded`` (an ungrounded token in a no-fact sentence) are enforced at
STRICT and advisory at STANDARD. The :func:`ENFORCEMENT`-matrix guardian test
(iterating EVERY :class:`StrictnessTier`) makes tiering a fact class a CI
failure. STANDARD's declared residuals (accepted posture costs, all pinned by
test): prompt echo — a question that embeds its own answer is affirmable from
the question's words (A-P2-6; NE-GROUND-REL narrowed A-P2-6 from "free
relational phrasing is model-trusted" to this: entity/value relations are now
row-bound by ``relation-ungrounded``, with the swapped-compound bag-of-tokens
fixed point disclosed in SPEC); digit-free existence/zero claims pass
advisory-only (A-P2-7); the number-word/corpus-name collision (A-P2-8).

**Advisory means recorded, never dropped.** An advisory finding is carried in
:attr:`StrictnessDecision.advisory` (class+count, C12-shaped, no text) so the
flywheel/steward sees it as a curation signal — it simply does not withhold.

C3 purity: this module is stdlib-only + imports FROM ``ne.govern.grounding``
ONLY (no cycle: grounding.py DEFINES :class:`GateProfile`/the profiles and can
never import this module — its own AST guardian allowlists only stdlib roots).
:class:`GateProfile`, :data:`PROFILE_NG1_FROZEN`, :data:`PROFILE_NG2` are
re-exported here so the SPEC-named public surface
``from ne.govern.grounding_policy import GateProfile, PROFILE_NG1_FROZEN,
PROFILE_NG2`` works. No I/O, no clock, no randomness — a positive-allowlist AST
guardian test enforces the import discipline.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from ne.govern.grounding import (
    PROFILE_NG1_FROZEN,
    PROFILE_NG2,
    FindingReason,
    GateProfile,
    GroundingFinding,
    GroundingVerdict,
)

__all__ = [
    "ENFORCEMENT",
    "PROFILE_NG1_FROZEN",
    "PROFILE_NG2",
    "GateProfile",
    "StrictnessDecision",
    "StrictnessTier",
    "apply_strictness",
]


class StrictnessTier(StrEnum):
    """Deployment posture (design §2.2). Not caller-selectable — a per-request
    tier would be a self-service assurance downgrade (B-P0-1); it lives only on
    deployment config, threaded as frozen plain data."""

    STRICT = "strict"
    STANDARD = "standard"


#: Every FindingReason class (the ng-2 vocabulary; ng1 emits a subset).
_ALL_CLASSES: frozenset[FindingReason] = frozenset(
    {
        "citation-out-of-range",
        "iri-ungrounded",
        "numeral-ungrounded",
        "numeral-unparseable",
        "phrase-recombination",
        "relation-ungrounded",
        "vocab-extended",
        "vocab-ungrounded",
        "vocab-ungrounded-factual",
    }
)

#: The classes that withhold at EVERY tier: the six fact classes + the
#: fact-sentence vocab class. Exactly ``_ALL_CLASSES`` minus the two purely
#: non-factual vocabulary classes the knob is allowed to move. This is the ONE
#: place the "facts always verify" law is encoded; the guardian test pins it.
_FACT_CLASSES: frozenset[FindingReason] = _ALL_CLASSES - {"vocab-ungrounded", "vocab-extended"}

#: The §2.5 finding-class x tier enforcement matrix (immutable). STRICT enforces
#: everything; STANDARD enforces exactly the fact classes (vocab-extended /
#: vocab-ungrounded become advisory). Iterated by the guardian test over EVERY
#: StrictnessTier member so a future tier that drops a fact class fails CI.
ENFORCEMENT: Mapping[StrictnessTier, frozenset[FindingReason]] = MappingProxyType(
    {
        StrictnessTier.STRICT: _ALL_CLASSES,
        StrictnessTier.STANDARD: _FACT_CLASSES,
    }
)


@dataclass(frozen=True, slots=True)
class StrictnessDecision:
    """The post-hoc policy result for one verdict at one tier. ``enforced`` +
    ``advisory`` partition ``verdict.findings`` in order; ``withhold`` iff any
    finding was enforced. C12-shaped throughout (findings are class+count)."""

    withhold: bool
    enforced: tuple[GroundingFinding, ...]
    advisory: tuple[GroundingFinding, ...]


def apply_strictness(verdict: GroundingVerdict, tier: StrictnessTier) -> StrictnessDecision:
    """Partition ``verdict.findings`` into enforced/advisory by the §2.5
    :data:`ENFORCEMENT` matrix for ``tier``, preserving order. ``withhold`` iff
    at least one finding is enforced. Pure: the gate stays tier-agnostic
    (:func:`ne.govern.grounding.check_grounding` computed every class); this is
    the only place a tier is consulted."""
    enforced_classes = ENFORCEMENT[tier]
    enforced: list[GroundingFinding] = []
    advisory: list[GroundingFinding] = []
    for finding in verdict.findings:
        if finding.reason in enforced_classes:
            enforced.append(finding)
        else:
            advisory.append(finding)
    return StrictnessDecision(
        withhold=bool(enforced),
        enforced=tuple(enforced),
        advisory=tuple(advisory),
    )
