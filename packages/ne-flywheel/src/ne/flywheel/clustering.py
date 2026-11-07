# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Deterministic signal clustering for the NE-065 curation agent.

Correction signals that share a normalized reference (the ``subject_ref`` they
target, plus the kind-specific provenance refs a ``wrong_source``/``re_query``
signal carries) are grouped by a union-find over those shared refs. The result
is a set of :class:`SignalCluster` objects the curation engine routes into fix
lanes.

Determinism contract (CI never depends on live-LLM nondeterminism): given the
SAME input SET of signals in ANY order, this module returns IDENTICAL clusters
— identical membership, identical ordering, and identical content-addressed
``cluster_id`` values. Union-find components are order-independent by
construction; every collection is explicitly sorted by the append-order store
id (or, for the id itself, over the sorted event-id set). No LLM, no clock, no
randomness participates in a clustering decision.

Positive feedback (``thumbs_up``) is NOT a correction and is filtered out by
:func:`correction_candidates` before clustering — the flywheel only proposes
fixes for things that went wrong.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict

from ne.flywheel.store import StoredCorrectionSignal
from ne.schema import CorrectionKind

#: Provenance keys (written by NE-064 ``_kind_specific_provenance``) that carry an
#: ADDITIONAL reference the cluster should union on, beyond ``subject_ref``:
#: a ``wrong_source`` signal's ``answer_ref`` (the answer the bad citation sat on)
#: and a ``re_query`` signal's ``requery_ref`` (the follow-up query id).
_PROVENANCE_REF_KEYS = ("answer_ref", "requery_ref")


def correction_candidates(
    signals: Sequence[StoredCorrectionSignal],
) -> tuple[StoredCorrectionSignal, ...]:
    """Drop positive feedback; return only the signals that represent a correction.

    ``thumbs_up`` is explicit positive feedback — it is not something to fix, so
    it never becomes a cluster or a proposal. Every other kind (thumbs_down,
    edit_distance, wrong_source, re_query, approval_rejection) is a correction
    candidate. Input order is preserved; the caller clusters deterministically.
    """
    return tuple(s for s in signals if s.signal_kind != CorrectionKind.thumbs_up.value)


def _refs_for(signal: StoredCorrectionSignal) -> frozenset[str]:
    """Return the normalized set of refs a signal is clustered on.

    Always includes ``subject_ref``; additionally includes the kind-specific
    provenance refs (``answer_ref`` for wrong_source, ``requery_ref`` for
    re_query) read from ``signal.payload['provenance']``. Each ref is
    ``.strip()``-normalized (case preserved — refs are opaque ids) and empties
    are dropped.
    """
    refs: set[str] = set()
    subject = signal.subject_ref.strip()
    if subject:
        refs.add(subject)
    provenance: Any = signal.payload.get("provenance")
    if isinstance(provenance, Mapping):
        for key in _PROVENANCE_REF_KEYS:
            raw = provenance.get(key)
            if isinstance(raw, str):
                normalized = raw.strip()
                if normalized:
                    refs.add(normalized)
    return frozenset(refs)


class SignalCluster(BaseModel):
    """A deterministically-formed group of correction signals sharing a ref.

    ``cluster_id`` is content-addressed: the sha256 over the newline-joined
    SORTED event ids, so the same member set yields the same id regardless of
    the order the signals were fed in. Every tuple field is sorted (members by
    store id ASC; ``kinds``/``refs`` as sorted-unique) for a stable identity.
    """

    model_config = ConfigDict(frozen=True)

    cluster_id: str
    signal_ids: tuple[int, ...]
    event_ids: tuple[str, ...]
    kinds: tuple[str, ...]
    refs: tuple[str, ...]
    signals: tuple[StoredCorrectionSignal, ...]


def _cluster_id_for(event_ids: Sequence[str]) -> str:
    """Content-addressed cluster id: sha256 over the newline-joined SORTED event ids."""
    joined = "\n".join(sorted(event_ids))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def cluster_signals(
    signals: Sequence[StoredCorrectionSignal],
) -> tuple[SignalCluster, ...]:
    """Union-find cluster ``signals`` by shared normalized ref (deterministic).

    Two signals join the same cluster iff their :func:`_refs_for` sets
    intersect (directly or transitively). The result is independent of input
    order: signals are processed by store id ASC, members within a cluster are
    sorted by store id ASC, and clusters are ordered by their minimum store id
    ASC. See the module docstring for the full determinism contract.
    """
    ordered = sorted(signals, key=lambda s: s.id)
    parent: dict[int, int] = {s.id: s.id for s in ordered}

    def find(x: int) -> int:
        root = x
        while parent[root] != root:
            root = parent[root]
        # Path-compress for stability/perf; does not affect the partition.
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        # Deterministic root: always attach the larger id under the smaller.
        lo, hi = (ra, rb) if ra < rb else (rb, ra)
        parent[hi] = lo

    # Bucket refs -> the signal ids carrying them, then union each bucket. Bucketing
    # (rather than a running first-seen map) keeps the partition order-independent.
    ref_to_ids: dict[str, list[int]] = {}
    for signal in ordered:
        for ref in _refs_for(signal):
            ref_to_ids.setdefault(ref, []).append(signal.id)
    for ids in ref_to_ids.values():
        first = ids[0]
        for other in ids[1:]:
            union(first, other)

    by_root: dict[int, list[StoredCorrectionSignal]] = {}
    for signal in ordered:
        by_root.setdefault(find(signal.id), []).append(signal)

    clusters: list[SignalCluster] = []
    for members in by_root.values():
        members.sort(key=lambda s: s.id)
        event_ids = tuple(s.event_id for s in members)
        all_refs: set[str] = set()
        for s in members:
            all_refs |= _refs_for(s)
        clusters.append(
            SignalCluster(
                cluster_id=_cluster_id_for(event_ids),
                signal_ids=tuple(s.id for s in members),
                event_ids=event_ids,
                kinds=tuple(sorted({s.signal_kind for s in members})),
                refs=tuple(sorted(all_refs)),
                signals=tuple(members),
            )
        )
    clusters.sort(key=lambda c: c.signal_ids[0])
    return tuple(clusters)


__all__ = [
    "SignalCluster",
    "cluster_signals",
    "correction_candidates",
]
