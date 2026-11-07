# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The integration ratchet: RagEvalSuite fully offline over scripted seams.

Real ne-eval code over real SQLite + scripted Judge/EvalTarget (the seams NE-025
fills live). Proves the fail-closed judge policy runs BEFORE any scoring, all
four metrics compute, config_hash is stamped verbatim, and exactly one run is
persisted.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa

from ne.contracts.evalobs import EvalSuite
from ne.eval.calibration import CalibrationArtifact, save_artifact
from ne.eval.errors import CrossFamilyError, EvalConfigError
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
from ne.eval.store import EvalRunStore
from ne.eval.suite import RagEvalSuite
from ne.eval.testing import ScriptedJudge, ScriptedTarget, make_retrieval_result

CONFIG_HASH = "deadbeef" * 8


def green_judge(**kw: str) -> ScriptedJudge:
    return ScriptedJudge(
        {
            "faithfulness_statements": StatementsOut(statements=("s1", "s2")),
            "faithfulness_verdicts": VerdictsOut(
                verdicts=(
                    VerdictItem(statement="s1", verdict=1),
                    VerdictItem(statement="s2", verdict=1),
                )
            ),
            "context_precision": PrecisionOut(verdict=1),
            "context_recall": RecallClassificationsOut(
                classifications=(
                    RecallClassificationItem(statement="s1", attributed=1),
                    RecallClassificationItem(statement="s2", attributed=1),
                )
            ),
        },
        judge_model_id=kw.get("judge_model_id", "qwen3.5-judge"),
        judge_family=kw.get("judge_family", "qwen"),
    )


def target_for(golden: GoldenSet) -> ScriptedTarget:
    retrievals = {}
    answers = {}
    for item in golden.items:
        chunks: list[tuple[str, str, str, float]] = []
        score = 0.9
        for doc in item.relevant_document_ids:
            chunks.append((f"c-{doc}", doc, f"context for {doc}", score))
            score -= 0.1
        chunks.append(("c-filler", "docZ", "filler", 0.05))
        retrievals[item.question] = make_retrieval_result(chunks)
        answers[item.question] = f"answer for {item.id}"
    return ScriptedTarget(retrievals, answers)


def write_calibrations(calibration_dir: Path, judge: ScriptedJudge, metrics: Sequence[str]) -> None:
    rubric_ids = sorted({rid for m in metrics for rid in METRIC_RUBRICS.get(m, ())})
    for rid in rubric_ids:
        rubric = get_rubric(rid)
        save_artifact(
            CalibrationArtifact(
                judge_model_id=judge.judge_model_id,
                judge_family=judge.judge_family,
                rubric_id=rubric.id,
                rubric_version=rubric.version,
                rubric_hash=rubric.content_hash,
                labels_hash="0" * 64,
                items=(),
                accuracy=1.0,
                cohen_kappa=1.0,
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
            ),
            calibration_dir,
        )


ALL_METRICS = ("recall_at_10", "faithfulness", "context_precision", "context_recall")


def test_green_run_all_metrics(sample_golden: GoldenSet, tmp_path: Path, engine: sa.Engine) -> None:
    judge = green_judge()
    write_calibrations(tmp_path, judge, ALL_METRICS)
    store = EvalRunStore(engine)

    suite = RagEvalSuite(
        golden=sample_golden,
        target=target_for(sample_golden),
        judge=judge,
        evaluated_family="claude",
        calibration_dir=str(tmp_path),
        store=store,
        metrics=ALL_METRICS,
    )
    result = suite.run_sync(CONFIG_HASH)
    assert result.config_hash == CONFIG_HASH
    assert set(result.metrics) == set(ALL_METRICS)
    assert result.passed is True
    runs = store.list_runs()
    assert len(runs) == 1
    assert runs[0].config_hash == CONFIG_HASH
    assert runs[0].golden_set_hash
    assert "context_precision" in runs[0].rubric_versions


def test_failing_metric_fails_gate(sample_golden: GoldenSet, tmp_path: Path) -> None:
    judge = ScriptedJudge(
        {
            "faithfulness_statements": StatementsOut(statements=("s1", "s2")),
            "faithfulness_verdicts": VerdictsOut(
                verdicts=(
                    VerdictItem(statement="s1", verdict=0),
                    VerdictItem(statement="s2", verdict=0),
                )
            ),
            "context_precision": PrecisionOut(verdict=1),
            "context_recall": RecallClassificationsOut(
                classifications=(
                    RecallClassificationItem(statement="s1", attributed=1),
                    RecallClassificationItem(statement="s2", attributed=1),
                )
            ),
        },
        judge_model_id="qwen3.5-judge",
        judge_family="qwen",
    )
    write_calibrations(tmp_path, judge, ALL_METRICS)
    suite = RagEvalSuite(
        golden=sample_golden,
        target=target_for(sample_golden),
        judge=judge,
        evaluated_family="claude",
        calibration_dir=str(tmp_path),
        metrics=ALL_METRICS,
    )
    result = suite.run_sync(CONFIG_HASH)
    assert result.metrics["faithfulness"] == 0.0
    assert result.passed is False


def test_high_judge_error_rate_fails(sample_golden: GoldenSet, tmp_path: Path) -> None:
    # faithfulness verdicts count mismatches on EVERY item -> all faithfulness
    # items error (score 0.0). metrics green? No — but error-rate alone must fail.
    judge = green_judge()
    write_calibrations(tmp_path, judge, ["faithfulness"])
    # One-statement verdict vs two statements -> every item errors.
    judge_bad = ScriptedJudge(
        {
            "faithfulness_statements": StatementsOut(statements=("s1", "s2")),
            "faithfulness_verdicts": VerdictsOut(
                verdicts=(VerdictItem(statement="s1", verdict=1),)
            ),
        },
        judge_model_id="qwen3.5-judge",
        judge_family="qwen",
    )
    suite = RagEvalSuite(
        golden=sample_golden,
        target=target_for(sample_golden),
        judge=judge_bad,
        evaluated_family="claude",
        calibration_dir=str(tmp_path),
        metrics=("faithfulness",),
        thresholds=(),
        max_judge_error_rate=0.2,
    )
    result = suite.run_sync(CONFIG_HASH)
    assert result.passed is False


def test_green_metrics_but_error_rate_kills(sample_golden: GoldenSet, tmp_path: Path) -> None:
    # faithfulness green on 5/6 items, errors on 1 -> mean 0.833 (>=0.77 PASS) but
    # error-rate 1/6 > max 0.1 -> run fails.
    good = VerdictsOut(
        verdicts=(
            VerdictItem(statement="s1", verdict=1),
            VerdictItem(statement="s2", verdict=1),
        )
    )
    bad = VerdictsOut(verdicts=(VerdictItem(statement="s1", verdict=1),))
    judge = ScriptedJudge(
        {
            "faithfulness_statements": StatementsOut(statements=("s1", "s2")),
            "faithfulness_verdicts": [good, good, good, good, good, bad],
        },
        judge_model_id="qwen3.5-judge",
        judge_family="qwen",
    )
    write_calibrations(tmp_path, judge, ["faithfulness"])
    from ne.eval.thresholds import ThresholdSpec

    suite = RagEvalSuite(
        golden=sample_golden,
        target=target_for(sample_golden),
        judge=judge,
        evaluated_family="claude",
        calibration_dir=str(tmp_path),
        metrics=("faithfulness",),
        thresholds=(ThresholdSpec(metric="faithfulness", threshold=0.80, tolerance=0.03),),
        max_judge_error_rate=0.1,
    )
    result = suite.run_sync(CONFIG_HASH)
    assert result.metrics["faithfulness"] == pytest.approx(5 / 6)
    assert result.passed is False


def test_uncalibrated_judge_refuses_before_any_call(
    sample_golden: GoldenSet, tmp_path: Path
) -> None:
    judge = green_judge()
    target = target_for(sample_golden)
    suite = RagEvalSuite(
        golden=sample_golden,
        target=target,
        judge=judge,
        evaluated_family="claude",
        calibration_dir=str(tmp_path),  # empty -> no artifacts
        metrics=ALL_METRICS,
    )
    from ne.eval.errors import CalibrationMissingError

    with pytest.raises(CalibrationMissingError):
        suite.run_sync(CONFIG_HASH)
    assert judge.calls == []
    assert target.retrieve_calls == []


def test_same_family_refused(sample_golden: GoldenSet, tmp_path: Path) -> None:
    judge = green_judge(judge_family="claude")
    write_calibrations(tmp_path, judge, ALL_METRICS)
    suite = RagEvalSuite(
        golden=sample_golden,
        target=target_for(sample_golden),
        judge=judge,
        evaluated_family="claude",
        calibration_dir=str(tmp_path),
        metrics=ALL_METRICS,
    )
    with pytest.raises(CrossFamilyError):
        suite.run_sync(CONFIG_HASH)


def test_judged_metric_without_judge_raises(sample_golden: GoldenSet) -> None:
    suite = RagEvalSuite(
        golden=sample_golden,
        target=target_for(sample_golden),
        judge=None,
        evaluated_family="claude",
        metrics=("faithfulness",),
    )
    with pytest.raises(EvalConfigError):
        suite.run_sync(CONFIG_HASH)


def test_recall_only_no_judge_needed(sample_golden: GoldenSet) -> None:
    suite = RagEvalSuite(
        golden=sample_golden,
        target=target_for(sample_golden),
        judge=None,
        metrics=("recall_at_10",),
        thresholds=(),
    )
    result = suite.run_sync(CONFIG_HASH)
    assert result.metrics["recall_at_10"] == 1.0


def test_satisfies_eval_suite_protocol(sample_golden: GoldenSet) -> None:
    suite = RagEvalSuite(golden=sample_golden, target=target_for(sample_golden), judge=None)
    checked: EvalSuite = suite
    assert checked.name == "rag"
