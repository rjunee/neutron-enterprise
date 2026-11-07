# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Deterministic lane-routing + confidence/priority tests (NE-065)."""

from __future__ import annotations

import hashlib
import itertools
from collections.abc import Sequence

from ne.flywheel.clustering import SignalCluster
from ne.flywheel.lanes import (
    FixLane,
    ProposalPriority,
    confidence_for,
    priority_for,
    route_cluster,
)
from ne.schema import CorrectionKind

_CORRECTION_KINDS = [
    CorrectionKind.thumbs_down.value,
    CorrectionKind.edit_distance.value,
    CorrectionKind.wrong_source.value,
    CorrectionKind.re_query.value,
    CorrectionKind.approval_rejection.value,
]


def _cluster(
    *,
    kinds: Sequence[str],
    refs: Sequence[str] = ("doc-1",),
    n_signals: int = 1,
) -> SignalCluster:
    event_ids = tuple(f"ev-{i}" for i in range(n_signals))
    cluster_id = hashlib.sha256("\n".join(sorted(event_ids)).encode()).hexdigest()
    return SignalCluster(
        cluster_id=cluster_id,
        signal_ids=tuple(range(1, n_signals + 1)),
        event_ids=event_ids,
        kinds=tuple(sorted(set(kinds))),
        refs=tuple(sorted(set(refs))),
        signals=(),
    )


def test_approval_rejection_routes_policy() -> None:
    cluster = _cluster(kinds=[CorrectionKind.approval_rejection.value])
    assert route_cluster(cluster) is FixLane.POLICY


def test_wrong_source_routes_retrieval() -> None:
    cluster = _cluster(kinds=[CorrectionKind.wrong_source.value])
    assert route_cluster(cluster) is FixLane.RETRIEVAL


def test_re_query_routes_retrieval() -> None:
    cluster = _cluster(kinds=[CorrectionKind.re_query.value])
    assert route_cluster(cluster) is FixLane.RETRIEVAL


def test_schema_ref_scheme_routes_schema() -> None:
    cluster = _cluster(
        kinds=[CorrectionKind.wrong_source.value],
        refs=["schema:crm.contact.email"],
    )
    assert route_cluster(cluster) is FixLane.SCHEMA


def test_thumbs_down_and_edit_distance_route_content() -> None:
    assert route_cluster(_cluster(kinds=[CorrectionKind.thumbs_down.value])) is FixLane.CONTENT
    assert route_cluster(_cluster(kinds=[CorrectionKind.edit_distance.value])) is FixLane.CONTENT


def test_precedence_policy_beats_schema_beats_retrieval_beats_content() -> None:
    # policy dominates everything (even a schema-scheme ref + retrieval kind).
    assert (
        route_cluster(
            _cluster(
                kinds=[
                    CorrectionKind.approval_rejection.value,
                    CorrectionKind.wrong_source.value,
                    CorrectionKind.thumbs_down.value,
                ],
                refs=["schema:x", "doc-1"],
            )
        )
        is FixLane.POLICY
    )
    # schema beats retrieval (no policy kind present).
    assert (
        route_cluster(
            _cluster(
                kinds=[
                    CorrectionKind.wrong_source.value,
                    CorrectionKind.thumbs_down.value,
                ],
                refs=["field:x", "doc-1"],
            )
        )
        is FixLane.SCHEMA
    )
    # retrieval beats content (no policy/schema).
    assert (
        route_cluster(
            _cluster(
                kinds=[
                    CorrectionKind.re_query.value,
                    CorrectionKind.thumbs_down.value,
                ],
                refs=["doc-1"],
            )
        )
        is FixLane.RETRIEVAL
    )


def test_every_kind_combination_routes_exactly_one_lane() -> None:
    for r in range(1, len(_CORRECTION_KINDS) + 1):
        for combo in itertools.combinations(_CORRECTION_KINDS, r):
            lane = route_cluster(_cluster(kinds=list(combo)))
            assert isinstance(lane, FixLane)
            # StrEnum membership => exactly one of the four.
            assert lane in {
                FixLane.CONTENT,
                FixLane.RETRIEVAL,
                FixLane.SCHEMA,
                FixLane.POLICY,
            }


def test_confidence_and_priority_formulas() -> None:
    content_kind = [CorrectionKind.thumbs_down.value]
    one = _cluster(kinds=content_kind, n_signals=1)
    two = _cluster(kinds=content_kind, n_signals=2)
    three = _cluster(kinds=content_kind, n_signals=3)
    six = _cluster(kinds=content_kind, n_signals=6)

    assert confidence_for(one) == 0.5
    assert confidence_for(two) == 0.6
    assert confidence_for(three) == 0.7
    assert confidence_for(six) == 0.9  # capped

    assert priority_for(one, FixLane.CONTENT) is ProposalPriority.LOW
    assert priority_for(two, FixLane.CONTENT) is ProposalPriority.MEDIUM
    assert priority_for(three, FixLane.CONTENT) is ProposalPriority.HIGH

    # A POLICY singleton is HIGH despite only one signal.
    policy_one = _cluster(kinds=[CorrectionKind.approval_rejection.value], n_signals=1)
    assert priority_for(policy_one, FixLane.POLICY) is ProposalPriority.HIGH


def test_entity_resolution_lane_is_job_emitted_never_routed() -> None:
    # ENTITY_RESOLUTION (ne-brain-graph Splink job) and ONTOLOGY (NE-ONTOLOGY-FLYWHEEL
    # steward-authored verified-answer templates) are job/API-emitted FixLane members,
    # NEVER returned by route_cluster: exhaustively over every kind combo.
    assert FixLane.ENTITY_RESOLUTION.value == "entity_resolution"
    assert FixLane.ONTOLOGY.value == "ontology"
    assert FixLane.SKILLS.value == "skills"
    assert len(list(FixLane)) == 7
    for r in range(1, len(_CORRECTION_KINDS) + 1):
        for combo in itertools.combinations(_CORRECTION_KINDS, r):
            routed = route_cluster(_cluster(kinds=list(combo)))
            assert routed is not FixLane.ENTITY_RESOLUTION
            assert routed is not FixLane.ONTOLOGY
            assert routed is not FixLane.SKILLS
