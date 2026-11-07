# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Threshold specs with tolerance-band PASS/WARN/FAIL semantics (SPEC §5 NE-024).

A :class:`ThresholdSpec` binds a metric to a threshold plus a tolerance band. The
band absorbs judge nondeterminism: a value just under the threshold WARNs (the
gate still passes) rather than flapping the CI gate red on noise.

Band semantics (DECIDED):

* ``value >= threshold``                         -> ``PASS``
* ``threshold - tolerance <= value < threshold`` -> ``WARN`` (gate passes)
* ``value < threshold - tolerance``              -> ``FAIL``
* metric absent from the computed metrics        -> ``FAIL`` (fail-closed)
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, model_validator


class MetricStatus(StrEnum):
    """The gate status of a single metric against its threshold spec."""

    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


class ThresholdSpec(BaseModel):
    """A metric threshold with a symmetric-below tolerance band."""

    model_config = ConfigDict(frozen=True)

    metric: str
    threshold: float
    tolerance: float = 0.0

    @model_validator(mode="after")
    def _validate(self) -> ThresholdSpec:
        if not (0.0 <= self.tolerance <= self.threshold):
            raise ValueError(
                f"tolerance must satisfy 0 <= tolerance <= threshold; "
                f"got tolerance={self.tolerance}, threshold={self.threshold}"
            )
        return self


DEFAULT_THRESHOLDS: tuple[ThresholdSpec, ...] = (
    ThresholdSpec(metric="recall_at_10", threshold=0.85, tolerance=0.02),
    ThresholdSpec(metric="faithfulness", threshold=0.80, tolerance=0.03),
    ThresholdSpec(metric="context_precision", threshold=0.70, tolerance=0.03),
    ThresholdSpec(metric="context_recall", threshold=0.80, tolerance=0.03),
)


def evaluate_thresholds(
    metrics: Mapping[str, float], specs: Sequence[ThresholdSpec]
) -> dict[str, MetricStatus]:
    """Return ``{metric: MetricStatus}`` applying tolerance-band semantics."""
    statuses: dict[str, MetricStatus] = {}
    for spec in specs:
        if spec.metric not in metrics:
            statuses[spec.metric] = MetricStatus.FAIL
            continue
        value = metrics[spec.metric]
        if value >= spec.threshold:
            statuses[spec.metric] = MetricStatus.PASS
        elif value >= spec.threshold - spec.tolerance:
            statuses[spec.metric] = MetricStatus.WARN
        else:
            statuses[spec.metric] = MetricStatus.FAIL
    return statuses


def gate_passed(statuses: Mapping[str, MetricStatus]) -> bool:
    """The gate passes iff no metric is ``FAIL`` (``WARN`` still passes)."""
    return all(status is not MetricStatus.FAIL for status in statuses.values())
