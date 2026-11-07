# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Threshold tolerance-band PASS/WARN/FAIL tests at the boundaries."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ne.eval.thresholds import (
    DEFAULT_THRESHOLDS,
    MetricStatus,
    ThresholdSpec,
    evaluate_thresholds,
    gate_passed,
)

_SPEC = (ThresholdSpec(metric="m", threshold=0.80, tolerance=0.05),)


def test_value_at_threshold_is_pass() -> None:
    assert evaluate_thresholds({"m": 0.80}, _SPEC)["m"] is MetricStatus.PASS


def test_value_at_band_floor_is_warn() -> None:
    assert evaluate_thresholds({"m": 0.75}, _SPEC)["m"] is MetricStatus.WARN


def test_just_below_band_is_fail() -> None:
    assert evaluate_thresholds({"m": 0.7499}, _SPEC)["m"] is MetricStatus.FAIL


def test_within_band_is_warn() -> None:
    assert evaluate_thresholds({"m": 0.78}, _SPEC)["m"] is MetricStatus.WARN


def test_missing_metric_is_fail() -> None:
    assert evaluate_thresholds({}, _SPEC)["m"] is MetricStatus.FAIL


def test_default_thresholds_match_spec() -> None:
    by_metric = {s.metric: s.threshold for s in DEFAULT_THRESHOLDS}
    assert by_metric == {
        "recall_at_10": 0.85,
        "faithfulness": 0.80,
        "context_precision": 0.70,
        "context_recall": 0.80,
    }


def test_gate_passed_with_warns_true() -> None:
    statuses = {"a": MetricStatus.PASS, "b": MetricStatus.WARN}
    assert gate_passed(statuses) is True


def test_gate_passed_with_fail_false() -> None:
    statuses = {"a": MetricStatus.PASS, "b": MetricStatus.FAIL}
    assert gate_passed(statuses) is False


def test_tolerance_validation() -> None:
    with pytest.raises(ValidationError):
        ThresholdSpec(metric="m", threshold=0.8, tolerance=-0.1)
    with pytest.raises(ValidationError):
        ThresholdSpec(metric="m", threshold=0.8, tolerance=0.9)
