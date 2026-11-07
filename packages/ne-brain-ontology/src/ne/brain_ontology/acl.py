# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Per-ACL-label named-graph partitioning for the RDF lift (C7, NE-085b).

Every lifted triple is placed into ONE named graph per ACL label its Tier-2
source carries (``urn:ne:graph:acl:<quoted-label>``). A principal-scoped read
then unions ONLY the graphs for the labels the principal holds — so a lifted
triple is trimmed identically to its T2 source, and the constructed
:class:`~ne.brain_ontology.store.protocols.DatasetSpec` is NEVER "all graphs".

``graphs_for_labels`` raises on an empty label set (fail-closed — it mirrors
``ne.brain.trimming``'s non-empty invariant WITHOUT importing it, C3): an empty
dataset must never widen to the full union. Only TRUSTED code constructs a
:class:`DatasetSpec`; agents never receive one.
"""

from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import quote

from .store.protocols import DatasetSpec

__all__ = ["ACL_GRAPH_PREFIX", "graph_iri_for_label", "graphs_for_labels"]

#: Namespace prefix for the per-ACL-label named graphs the lift writes into.
ACL_GRAPH_PREFIX = "urn:ne:graph:acl:"


def graph_iri_for_label(label: str) -> str:
    """Deterministic, collision-free named-graph IRI for one ACL label.

    ``urllib.parse.quote(label, safe='')`` percent-encodes every reserved and
    delimiter character, so two distinct labels can never collide on one IRI.
    """
    return f"{ACL_GRAPH_PREFIX}{quote(label, safe='')}"


def graphs_for_labels(labels: Iterable[str]) -> DatasetSpec:
    """The read :class:`DatasetSpec` unioning the graphs for ``labels``.

    Raises ``ValueError`` on an empty label set (fail-closed): the constructed
    dataset must never be the full union. The graph list is sorted for a
    deterministic query.
    """
    graphs = tuple(sorted(graph_iri_for_label(label) for label in labels))
    if not graphs:
        raise ValueError(
            "graphs_for_labels refuses an empty label set: a lifted-triple read "
            "dataset must never widen to all graphs (fail-closed C7 trim)"
        )
    return DatasetSpec(default_graphs=graphs)
