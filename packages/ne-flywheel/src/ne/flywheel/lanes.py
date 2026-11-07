# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The fix lanes and the deterministic cluster -> lane routing.

Every :class:`~ne.flywheel.clustering.SignalCluster` routes to EXACTLY one of the
four ROUTED :class:`FixLane` members. (A fifth, JOB-EMITTED lane —
``ENTITY_RESOLUTION`` — is stamped by the ne-brain-graph Splink job and is never
returned by :func:`route_cluster`; see the ``FixLane`` docstring.) The routing is
a documented, first-match precedence — no LLM,
no scoring, no randomness — so CI asserts it exactly and Argus reviews a
DECISION, not an ambiguity. NE-066's rejection feedback can retune the policy
later; this unit freezes a defensible v1.

Lane semantics (SPEC §5 four fix lanes):

* **policy**    — a governance / guardrail / ACL issue. A denied approval
  (``approval_rejection``) is the canonical signal: the fix is an OPA
  policy-diff proposal, not a content or retrieval change.
* **schema**    — a data-contract / extraction gap. Surfaced by a ref whose
  scheme names a schema/field/record/extraction/contract object: the produced
  answer is only wrong because the underlying contract is wrong.
* **retrieval** — the right document exists but was not retrieved / ranked
  (``wrong_source`` — a bad citation implies the good source was missed; or
  ``re_query`` — the user had to re-ask). The fix is a retrieval-config review.
* **content**   — the produced draft/answer content itself is wrong
  (``thumbs_down`` / ``edit_distance``). The fix is a source-doc change.

Precedence (STRICTEST governance concern first, first match wins):
``policy > schema > retrieval > content``. A cluster mixing kinds is routed by
the highest-precedence concern it contains — a data-contract gap dominates a
retrieval symptom, and a governance denial dominates everything. Because
``content`` is the catch-all fourth branch, every cluster routes to exactly one
lane by construction.
"""

from __future__ import annotations

import enum

from ne.flywheel.clustering import SignalCluster
from ne.schema import CorrectionKind


class FixLane(enum.StrEnum):
    """The fix lanes a proposal can carry.

    The first four (CONTENT / RETRIEVAL / SCHEMA / POLICY) are the ROUTED lanes:
    :func:`route_cluster` maps every :class:`SignalCluster` to exactly one of
    them by construction (CONTENT is the catch-all fourth branch). ENTITY_RESOLUTION
    is a fifth, JOB-EMITTED lane: it is stamped by the ne-brain-graph Splink ER
    job on its entity-merge-candidate proposals and is NEVER returned by
    ``route_cluster`` (the router's totality argument covers the four routed lanes
    only; the ER lane rides the same NE-066 gate via the diff renderer's
    ``_KNOWN_LANES`` allowlist, which derives from this enum so the lane is
    auto-trusted). ENTITY_RESOLUTION now carries TWO job-emitted proposal kinds:
    ``entity_merge_candidate`` (the Splink ER job) and ``graph_extraction_candidate``
    (the NE-T2X LLM-extraction job, NE-T2X-2, future). Both are NEVER returned by
    ``route_cluster`` and both ride the same NE-066 gate + the same
    ``_KNOWN_LANES`` auto-trust — no new lane was needed for extraction to reuse
    the ER lane's review posture.

    ``ONTOLOGY`` is a SIXTH, STEWARD/API-EMITTED lane (NE-ONTOLOGY-FLYWHEEL): it
    is stamped on steward-authored ``verified_answer_candidate`` proposals by the
    ne-api curation seam and is NEVER returned by ``route_cluster`` (exactly like
    ``ENTITY_RESOLUTION``). It rides the same NE-066 gate + the diff renderer's
    ``_KNOWN_LANES`` allowlist (derived from this enum, so the lane is auto-trusted).

    ``SKILLS`` is a SEVENTH, JOB-EMITTED lane (NE-SKILL-1b): it is stamped on
    ``skill_candidate`` / ``skill_update_candidate`` proposals by the ne-skills
    ``SkillForge`` scheduled job and is NEVER returned by ``route_cluster``
    (exactly like ``ENTITY_RESOLUTION`` and ``ONTOLOGY``). It rides the same
    NE-066 gate + the diff renderer's ``_KNOWN_LANES`` allowlist (derived from
    this enum, so the lane is auto-trusted).
    """

    CONTENT = "content"
    RETRIEVAL = "retrieval"
    SCHEMA = "schema"
    POLICY = "policy"
    ENTITY_RESOLUTION = "entity_resolution"
    ONTOLOGY = "ontology"
    SKILLS = "skills"


class ProposalPriority(enum.StrEnum):
    """Relative urgency stamped on a proposal for steward triage."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


#: Ref schemes (the lowercased text before the first ``:`` in a ref) that mark a
#: data-contract / extraction object — their presence routes a cluster to SCHEMA.
_SCHEMA_REF_SCHEMES = frozenset({"schema", "field", "record", "extraction", "contract"})
#: Signal kinds that indicate the right source existed but was not surfaced.
_RETRIEVAL_KINDS = frozenset({CorrectionKind.wrong_source.value, CorrectionKind.re_query.value})


def _ref_scheme(ref: str) -> str | None:
    """Return the lowercased scheme (text before the first ``:``), or ``None``.

    A ref without a ``:`` has no scheme (returns ``None``) — a bare id never
    routes to the schema lane by accident.
    """
    if ":" not in ref:
        return None
    return ref.split(":", 1)[0].strip().lower()


def route_cluster(cluster: SignalCluster) -> FixLane:
    """Route ``cluster`` to EXACTLY one lane via the documented precedence.

    First match wins, strictest concern first:

    1. **POLICY** if any member kind is ``approval_rejection`` (a denied
       approval is a governance/guardrail/ACL issue -> OPA policy-diff).
    2. **SCHEMA** if any ref scheme is one of schema/field/record/extraction/
       contract (a data-contract/extraction gap dominates retrieval symptoms).
    3. **RETRIEVAL** if any member kind is ``wrong_source`` or ``re_query``
       (the right doc exists but was not retrieved/ranked).
    4. **CONTENT** otherwise (thumbs_down / edit_distance -> the produced
       content itself is wrong).
    """
    kinds = set(cluster.kinds)
    if CorrectionKind.approval_rejection.value in kinds:
        return FixLane.POLICY
    if any(_ref_scheme(ref) in _SCHEMA_REF_SCHEMES for ref in cluster.refs):
        return FixLane.SCHEMA
    if kinds & _RETRIEVAL_KINDS:
        return FixLane.RETRIEVAL
    return FixLane.CONTENT


def confidence_for(cluster: SignalCluster) -> float:
    """Confidence in the proposal: more corroborating signals => higher, capped 0.9.

    ``round(min(0.9, 0.4 + 0.1 * n), 2)`` where ``n`` is the member count — a
    lone signal is 0.5, and corroboration saturates at 0.9 (never certainty; a
    steward still approves at NE-066).
    """
    n = len(cluster.signal_ids)
    return round(min(0.9, 0.4 + 0.1 * n), 2)


def priority_for(cluster: SignalCluster, lane: FixLane) -> ProposalPriority:
    """Priority for steward triage: governance and well-corroborated clusters rank up.

    HIGH if the lane is POLICY (a governance denial) or the cluster has >= 3
    signals; MEDIUM at exactly 2 signals; LOW for a lone signal.
    """
    n = len(cluster.signal_ids)
    if lane is FixLane.POLICY or n >= 3:
        return ProposalPriority.HIGH
    if n == 2:
        return ProposalPriority.MEDIUM
    return ProposalPriority.LOW


__all__ = [
    "FixLane",
    "ProposalPriority",
    "confidence_for",
    "priority_for",
    "route_cluster",
]
