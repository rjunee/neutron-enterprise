# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Bounded, frozen configuration for the bootstrap profiler + inference core.

Every threshold that governs profiling or inference lives here, not scattered
as magic numbers through the pipeline — profiling bounds and inference
thresholds. Threshold VALUES are asserted only by
tests, never restated as normative prose elsewhere in this package
([[written-rules-disarm-review]]).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class BootstrapConfig(BaseModel):
    """Tunable bounds for sampling, profiling, and candidate-model inference.

    All library functions in this package accept ``config: BootstrapConfig``
    with :data:`DEFAULT_CONFIG` as the default value.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # §3.3 — bounded sampling.
    rows_per_table: int = 200
    exemplars_k: int = 5
    exemplar_max_chars: int = 80

    # §4.1 — deterministic type/pattern inference.
    type_vote_threshold: float = 0.95
    pattern_threshold: float = 0.95

    # §4.1 — FK edge containment.
    containment_threshold: float = 0.95

    # §4.1 — enum detection.
    enum_max_distinct: int = 24
    enum_max_distinct_ratio: float = 0.1

    # §3.3 — deterministic PII value-shape detection.
    pii_value_shape_threshold: float = 0.5

    # §4.1 — seed-class anchoring.
    anchor_min_matching_attrs: int = 2
    anchor_min_overlap_ratio: float = 0.5


DEFAULT_CONFIG = BootstrapConfig()
"""Module-level singleton default, reused as the default value across every
public function's ``config`` parameter (avoids constructing a new
:class:`BootstrapConfig` in every function signature — ruff B008)."""
