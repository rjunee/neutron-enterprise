# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""promptfoo Python provider that runs the ne-eval suite and returns its metrics.

promptfoo calls :func:`call_api` per test row; it returns ``{"output": <json>}``
where the JSON carries ``metrics`` + ``passed``. The committed ``promptfooconfig.json``
asserts each metric against ``threshold - tolerance`` (see ``ne.eval.promptfoo``).

Modes (``NE_EVAL_GATE_MODE`` env, default ``offline``):

* ``offline`` — fixture-first: a deterministic scripted suite over a ``ScriptedJudge``
  with runtime-generated calibration artifacts, so the gate wiring is exercisable
  end-to-end (all four SPEC metrics emitted, green) with NO gateway/retrieval stack
  (the policy for this unit; plan §0.6/§1.4). It emits EVERY metric the committed
  ``promptfooconfig.json`` asserts, so the default ``run_gate.sh`` invocation passes.
* ``live`` — build the real suite from ``EvalSettings`` + the plugin registry
  (``eval_judge`` / ``eval_target``). NE-025 wires the live retrieval pipeline
  target; v1 raises a clear error pointing there.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from typing import Any


def _offline_output() -> dict[str, Any]:
    from ne.eval.calibration import CalibrationArtifact, CalibrationItem, save_artifact
    from ne.eval.golden import GoldenSet
    from ne.eval.judge import (
        PrecisionOut,
        RecallClassificationItem,
        RecallClassificationsOut,
        StatementsOut,
        VerdictItem,
        VerdictsOut,
    )
    from ne.eval.metrics import METRIC_RUBRICS
    from ne.eval.rubrics import get_rubric
    from ne.eval.suite import RagEvalSuite
    from ne.eval.testing import (
        ScriptedJudge,
        ScriptedTarget,
        make_golden_item,
        make_retrieval_result,
    )

    metrics = ("recall_at_10", "faithfulness", "context_precision", "context_recall")
    statement = "the answer lives in docA"

    # A ScriptedJudge whose canned outputs make every judged metric score 1.0.
    judge = ScriptedJudge(
        {
            "faithfulness_statements": StatementsOut(statements=(statement,)),
            "faithfulness_verdicts": VerdictsOut(
                verdicts=(VerdictItem(statement=statement, verdict=1),)
            ),
            "context_precision": PrecisionOut(verdict=1),
            "context_recall": RecallClassificationsOut(
                classifications=(RecallClassificationItem(statement=statement, attributed=1),)
            ),
        }
    )

    question = "What is the offline gate smoke question?"
    golden = GoldenSet(
        items=(
            make_golden_item(
                "gate-smoke",
                question,
                reference_answer="The answer lives in docA.",
                relevant_document_ids=("docA",),
            ),
        )
    )
    retrieval = make_retrieval_result([("c1", "docA", "the answer lives here", 0.9)])
    target = ScriptedTarget({question: retrieval}, {question: "The answer lives in docA."})

    with tempfile.TemporaryDirectory() as calibration_dir:
        # Generate a matching calibration artifact for every judged rubric so the
        # fail-closed calibration check passes (rubric_hash bound to content_hash).
        rubric_ids = sorted({rid for m in metrics for rid in METRIC_RUBRICS.get(m, ())})
        for rubric_id in rubric_ids:
            rubric = get_rubric(rubric_id)
            artifact = CalibrationArtifact(
                judge_model_id=judge.judge_model_id,
                judge_family=judge.judge_family,
                rubric_id=rubric.id,
                rubric_version=rubric.version,
                rubric_hash=rubric.content_hash,
                labels_hash="offline-gate",
                items=(CalibrationItem(item_id="gate-smoke", human_verdict=1, judge_verdict=1),),
                accuracy=1.0,
                cohen_kappa=1.0,
                created_at=datetime.now(UTC),
            )
            save_artifact(artifact, calibration_dir)

        suite = RagEvalSuite(
            golden=golden,
            target=target,
            judge=judge,
            metrics=metrics,
            evaluated_family="claude",
            calibration_dir=calibration_dir,
        )
        result = suite.run_sync(config_hash="offline-gate")
    return {"metrics": dict(result.metrics), "passed": result.passed}


def _live_output() -> dict[str, Any]:
    raise NotImplementedError(
        "live gate mode requires the NE-025 retrieval-pipeline eval target and a "
        "deployed gateway; set NE_EVAL_GATE_MODE=offline for the fixture-first gate"
    )


def call_api(prompt: str, options: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """promptfoo provider entrypoint. Returns ``{"output": json_string}``."""
    mode = os.environ.get("NE_EVAL_GATE_MODE", "offline")
    output = _offline_output() if mode == "offline" else _live_output()
    return {"output": json.dumps(output)}
