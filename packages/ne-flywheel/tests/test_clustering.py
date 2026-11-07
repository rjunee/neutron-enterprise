# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Deterministic clustering tests for the NE-065 curation agent."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ne.flywheel.clustering import cluster_signals, correction_candidates
from ne.flywheel.store import StoredCorrectionSignal
from ne.schema import CorrectionKind

_NOW = datetime(2026, 7, 1, tzinfo=UTC)


def _sig(
    signal_id: int,
    kind: CorrectionKind,
    subject_ref: str,
    *,
    event_id: str | None = None,
    provenance: dict[str, str] | None = None,
) -> StoredCorrectionSignal:
    payload: dict[str, Any] = {"provenance": dict(provenance or {})}
    return StoredCorrectionSignal(
        id=signal_id,
        event_id=event_id or f"ev-{signal_id}",
        signal_kind=kind.value,
        surface="console",
        principal_id="user:ada@acme.com",
        session_id="sess-1",
        turn_id=None,
        message_id=None,
        subject_ref=subject_ref,
        value=None,
        comment=None,
        actor="user:ada@acme.com",
        config_hash="deadbeef",
        kpi_tag=None,
        trace_id=None,
        occurred_at=_NOW,
        recorded_at=_NOW,
        payload=payload,
    )


def test_signals_sharing_subject_ref_form_one_cluster() -> None:
    signals = [
        _sig(1, CorrectionKind.thumbs_down, "doc-1"),
        _sig(2, CorrectionKind.thumbs_down, "doc-1"),
        _sig(3, CorrectionKind.edit_distance, "doc-1"),
    ]
    clusters = cluster_signals(signals)
    assert len(clusters) == 1
    assert clusters[0].signal_ids == (1, 2, 3)
    assert clusters[0].refs == ("doc-1",)
    assert clusters[0].kinds == ("edit_distance", "thumbs_down")


def test_wrong_source_answer_ref_joins_answer_cluster() -> None:
    # A wrong_source whose provenance answer_ref == another signal's subject_ref
    # unions transitively into one cluster.
    signals = [
        _sig(1, CorrectionKind.thumbs_down, "ans-9"),
        _sig(
            2,
            CorrectionKind.wrong_source,
            "cite-3",
            provenance={"answer_ref": "ans-9"},
        ),
    ]
    clusters = cluster_signals(signals)
    assert len(clusters) == 1
    assert clusters[0].signal_ids == (1, 2)
    assert set(clusters[0].refs) == {"ans-9", "cite-3"}


def test_thumbs_up_excluded_from_correction_candidates() -> None:
    signals = [
        _sig(1, CorrectionKind.thumbs_up, "doc-1"),
        _sig(2, CorrectionKind.thumbs_down, "doc-2"),
    ]
    candidates = correction_candidates(signals)
    assert [s.id for s in candidates] == [2]
    # thumbs_up never becomes a cluster.
    clusters = cluster_signals(correction_candidates(signals))
    assert len(clusters) == 1
    assert clusters[0].signal_ids == (2,)


def test_clustering_is_deterministic_across_input_order() -> None:
    signals = [
        _sig(1, CorrectionKind.thumbs_down, "doc-1"),
        _sig(2, CorrectionKind.edit_distance, "doc-1"),
        _sig(3, CorrectionKind.thumbs_down, "doc-2"),
        _sig(
            4,
            CorrectionKind.wrong_source,
            "cite-1",
            provenance={"answer_ref": "doc-2"},
        ),
    ]
    forward = cluster_signals(signals)
    reversed_clusters = cluster_signals(list(reversed(signals)))
    shuffled = cluster_signals([signals[2], signals[0], signals[3], signals[1]])

    def _shape(clusters: object) -> object:
        return [
            (c.cluster_id, c.signal_ids, c.event_ids, c.kinds, c.refs)
            for c in clusters  # type: ignore[attr-defined]
        ]

    assert _shape(forward) == _shape(reversed_clusters) == _shape(shuffled)


def test_disjoint_refs_form_singleton_clusters() -> None:
    signals = [
        _sig(1, CorrectionKind.thumbs_down, "doc-1"),
        _sig(2, CorrectionKind.thumbs_down, "doc-2"),
        _sig(3, CorrectionKind.thumbs_down, "doc-3"),
    ]
    clusters = cluster_signals(signals)
    assert [c.signal_ids for c in clusters] == [(1,), (2,), (3,)]


def test_cluster_id_is_content_addressed() -> None:
    a = [
        _sig(1, CorrectionKind.thumbs_down, "doc-1", event_id="ev-a"),
        _sig(2, CorrectionKind.thumbs_down, "doc-1", event_id="ev-b"),
    ]
    # Same event-id SET, different store ids/order => identical cluster_id.
    b = [
        _sig(5, CorrectionKind.thumbs_down, "doc-1", event_id="ev-b"),
        _sig(9, CorrectionKind.thumbs_down, "doc-1", event_id="ev-a"),
    ]
    (ca,) = cluster_signals(a)
    (cb,) = cluster_signals(b)
    assert ca.cluster_id == cb.cluster_id
