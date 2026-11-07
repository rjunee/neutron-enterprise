# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Splink entity resolution — statistical candidates, review-gated, never auto-merge.

Splink (and its GPL igraph closure) runs OUT-OF-PROCESS in a hash-pinned sidecar
venv (:class:`~ne.brain_graph.er._splink.SplinkSidecar`); the platform never
imports splink/igraph/duckdb. The :class:`~ne.brain_graph.er.job.SplinkResolutionJob`
exports attribute frames (a non-principal batch read over ``scan_nodes``), scores
pairs in the sidecar, and its SOLE write surface is the NE-065 proposal store
(``ENTITY_MERGE_CANDIDATE`` on the ``ENTITY_RESOLUTION`` lane). Merges are applied
ONLY through the NE-066 gate (:func:`~ne.brain_graph.er.merge.apply_merge`), are
non-destructive (a ``SAME_AS`` edge under INTERSECTION ACLs, source_system 'ne:er'),
and reversible (:func:`~ne.brain_graph.er.merge.unmerge`).
"""

from __future__ import annotations

from ne.brain_graph.er.export import export_frames
from ne.brain_graph.er.job import SplinkResolutionJob
from ne.brain_graph.er.merge import (
    ER_SOURCE_SYSTEM,
    SAME_AS_EDGE_KIND,
    MergeGateError,
    apply_merge,
    unmerge,
)

__all__ = [
    "ER_SOURCE_SYSTEM",
    "SAME_AS_EDGE_KIND",
    "MergeGateError",
    "SplinkResolutionJob",
    "apply_merge",
    "export_frames",
    "unmerge",
]
