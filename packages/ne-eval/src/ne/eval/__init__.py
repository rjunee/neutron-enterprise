# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ne-eval — the evaluation harness (native RAGAS-methodology metrics, SPEC §11).

The RAGAS *library* is excluded (heavy-ML closure trips the C4 gate); its metric
*algorithms* are reimplemented here over the platform's own pluggable judge.
"""

from __future__ import annotations

__version__ = "0.1.0"

from ne.eval import errors
from ne.eval.calibration import (
    CalibrationArtifact,
    CalibrationItem,
    check_calibration,
    cohen_kappa,
    load_artifacts,
    run_calibration,
    save_artifact,
)
from ne.eval.config import EvalSettings
from ne.eval.golden import (
    GoldenItem,
    GoldenSet,
    add_items,
    filter_by_tag,
    golden_set_hash,
    load_golden_set,
    save_golden_set,
)
from ne.eval.judge import (
    GatewayJudge,
    Judge,
    enforce_cross_family,
    out_model_for,
)
from ne.eval.metrics import (
    KNOWN_METRICS,
    METRIC_RUBRICS,
    ItemScore,
    context_precision,
    context_recall,
    faithfulness,
    is_judged_metric,
    mean_score,
    recall_at_k,
)
from ne.eval.promptfoo import (
    PROMPTFOO_ENV,
    PROMPTFOO_VERSION,
    build_promptfoo_config,
    check_promptfoo_config,
    write_promptfoo_config,
)
from ne.eval.rubrics import Rubric, get_rubric, load_rubrics, render
from ne.eval.store import (
    EvalRunRecord,
    EvalRunStore,
    ensure_eval_schema,
)
from ne.eval.suite import EvalTarget, RagEvalSuite
from ne.eval.thresholds import (
    DEFAULT_THRESHOLDS,
    MetricStatus,
    ThresholdSpec,
    evaluate_thresholds,
    gate_passed,
)

__all__ = [
    "DEFAULT_THRESHOLDS",
    "KNOWN_METRICS",
    "METRIC_RUBRICS",
    "PROMPTFOO_ENV",
    "PROMPTFOO_VERSION",
    "CalibrationArtifact",
    "CalibrationItem",
    "EvalRunRecord",
    "EvalRunStore",
    "EvalSettings",
    "EvalTarget",
    "GatewayJudge",
    "GoldenItem",
    "GoldenSet",
    "ItemScore",
    "Judge",
    "MetricStatus",
    "RagEvalSuite",
    "Rubric",
    "ThresholdSpec",
    "__version__",
    "add_items",
    "build_promptfoo_config",
    "check_calibration",
    "check_promptfoo_config",
    "cohen_kappa",
    "context_precision",
    "context_recall",
    "enforce_cross_family",
    "ensure_eval_schema",
    "errors",
    "evaluate_thresholds",
    "faithfulness",
    "filter_by_tag",
    "gate_passed",
    "get_rubric",
    "golden_set_hash",
    "is_judged_metric",
    "load_artifacts",
    "load_golden_set",
    "load_rubrics",
    "mean_score",
    "out_model_for",
    "recall_at_k",
    "render",
    "run_calibration",
    "save_artifact",
    "save_golden_set",
    "write_promptfoo_config",
]
