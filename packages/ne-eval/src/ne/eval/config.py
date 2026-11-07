# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Layered ne-eval settings (mirrors ne-gateway/ne-audit config conventions).

Validation is fail-loud at construction: an unknown metric name, ``top_k < 1``,
or a duplicate threshold metric raises :class:`EvalConfigError` (NOT a bare
``ValueError`` — pydantic v2 propagates non-``ValueError`` exceptions from an
after-validator unwrapped, which is the loud behavior we want).

The ``judge_family`` default documents the no-egress CI judge: an open-weight
model served on the ``PRIVATE_VLLM`` lane via the gateway task route
``eval-judge`` (SPEC §3.3 menu — Qwen/GLM). The concrete serving pin lands with
NE-013/NE-025; only the config seam ships here.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import SettingsConfigDict

from ne.contracts.config import NeSettings
from ne.eval.errors import EvalConfigError
from ne.eval.metrics import KNOWN_METRICS
from ne.eval.thresholds import DEFAULT_THRESHOLDS, ThresholdSpec


def _default_thresholds() -> list[ThresholdSpec]:
    return list(DEFAULT_THRESHOLDS)


def _default_metrics() -> list[str]:
    return ["recall_at_10", "faithfulness", "context_precision", "context_recall"]


class EvalSettings(NeSettings):
    """Layered configuration for an eval run."""

    model_config = SettingsConfigDict(
        env_prefix="NE_EVAL_",
        env_nested_delimiter="__",
        extra="forbid",
        toml_file=Path("ne.toml"),
    )

    judge_plugin: str = "gateway"
    judge_task: str = "eval-judge"
    judge_model_id: str = ""
    judge_family: str = ""
    evaluated_family: str = ""
    top_k: int = 10
    golden_set: Path | None = None
    calibration_dir: Path = Path("calibration")
    max_judge_error_rate: float = 0.2
    thresholds: list[ThresholdSpec] = Field(default_factory=_default_thresholds)
    metrics: list[str] = Field(default_factory=_default_metrics)

    @model_validator(mode="after")
    def _validate_after(self) -> EvalSettings:
        # Raise EvalConfigError directly: pydantic v2 propagates non-ValueError
        # exceptions from after-validators unwrapped (do NOT raise ValueError).
        for metric in self.metrics:
            if metric not in KNOWN_METRICS:
                raise EvalConfigError(
                    f"unknown metric {metric!r}; known metrics: {list(KNOWN_METRICS)}"
                )
        if self.top_k < 1:
            raise EvalConfigError(f"top_k must be >= 1; got {self.top_k}")
        seen: set[str] = set()
        for spec in self.thresholds:
            if spec.metric in seen:
                raise EvalConfigError(f"duplicate threshold metric {spec.metric!r}")
            seen.add(spec.metric)
        return self
