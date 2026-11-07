# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""EvalSettings default + validation tests."""

from __future__ import annotations

import pytest

from ne.eval.config import EvalSettings
from ne.eval.errors import EvalConfigError
from ne.eval.thresholds import DEFAULT_THRESHOLDS, ThresholdSpec


def test_defaults() -> None:
    settings = EvalSettings()
    assert settings.top_k == 10
    assert settings.max_judge_error_rate == 0.2
    assert tuple(settings.thresholds) == DEFAULT_THRESHOLDS
    assert settings.metrics == [
        "recall_at_10",
        "faithfulness",
        "context_precision",
        "context_recall",
    ]


def test_env_override_top_k(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NE_EVAL_TOP_K", "5")
    assert EvalSettings().top_k == 5


def test_unknown_metric_raises() -> None:
    with pytest.raises(EvalConfigError, match="unknown metric"):
        EvalSettings(metrics=["not_a_metric"])


def test_duplicate_threshold_metric_raises() -> None:
    with pytest.raises(EvalConfigError, match="duplicate threshold"):
        EvalSettings(
            thresholds=[
                ThresholdSpec(metric="recall_at_10", threshold=0.8),
                ThresholdSpec(metric="recall_at_10", threshold=0.7),
            ]
        )


def test_top_k_below_one_raises() -> None:
    with pytest.raises(EvalConfigError, match="top_k"):
        EvalSettings(top_k=0)


def test_extra_forbidden() -> None:
    with pytest.raises(ValueError):
        EvalSettings(unknown_field=123)  # type: ignore[call-arg]
