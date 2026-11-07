# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Calibration: Cohen's kappa, run/save/load, and fail-closed check_calibration."""

from __future__ import annotations

from pathlib import Path

import pytest

from ne.eval.calibration import (
    CalibrationArtifact,
    check_calibration,
    cohen_kappa,
    load_artifacts,
    run_calibration,
    save_artifact,
)
from ne.eval.errors import CalibrationMissingError, CalibrationStaleError
from ne.eval.judge import PrecisionOut
from ne.eval.rubrics import get_rubric
from ne.eval.testing import ScriptedJudge

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_cohen_kappa_perfect() -> None:
    assert cohen_kappa([1, 0, 1, 0], [1, 0, 1, 0]) == 1.0


def test_cohen_kappa_chance_level() -> None:
    # Judge always says 1; humans split 50/50 -> kappa 0.
    assert cohen_kappa([1, 1, 0, 0], [1, 1, 1, 1]) == pytest.approx(0.0)


def test_cohen_kappa_degenerate_pe_one() -> None:
    # All labels identical on both sides -> pe == 1; agreement perfect -> 1.0.
    assert cohen_kappa([1, 1, 1], [1, 1, 1]) == 1.0


def _judge() -> ScriptedJudge:
    return ScriptedJudge(
        {
            "context_precision": [
                PrecisionOut(verdict=1),
                PrecisionOut(verdict=1),
                PrecisionOut(verdict=1),
                PrecisionOut(verdict=0),
            ]
        },
        judge_model_id="qwen3.5-judge",
        judge_family="qwen",
    )


def test_run_calibration_stats() -> None:
    labels = FIXTURES / "context_precision_labels.jsonl"
    artifact = run_calibration(_judge(), get_rubric("context_precision"), labels)
    assert artifact.accuracy == pytest.approx(0.75)
    assert artifact.cohen_kappa == pytest.approx(0.5)
    assert len(artifact.items) == 4
    assert artifact.rubric_hash == get_rubric("context_precision").content_hash


def test_save_load_round_trip(tmp_path: Path) -> None:
    artifact = run_calibration(
        _judge(), get_rubric("context_precision"), FIXTURES / "context_precision_labels.jsonl"
    )
    save_artifact(artifact, tmp_path)
    loaded = load_artifacts(tmp_path)
    assert len(loaded) == 1
    assert loaded[0].accuracy == pytest.approx(0.75)


def test_check_calibration_passes(tmp_path: Path) -> None:
    judge = _judge()
    artifact = run_calibration(
        judge, get_rubric("context_precision"), FIXTURES / "context_precision_labels.jsonl"
    )
    save_artifact(artifact, tmp_path)
    check_calibration(tmp_path, judge, [get_rubric("context_precision")])


def test_check_calibration_missing(tmp_path: Path) -> None:
    with pytest.raises(CalibrationMissingError):
        check_calibration(tmp_path, _judge(), [get_rubric("context_precision")])


def test_check_calibration_stale_on_edited_rubric(tmp_path: Path) -> None:
    judge = _judge()
    rubric = get_rubric("context_precision")
    artifact = run_calibration(judge, rubric, FIXTURES / "context_precision_labels.jsonl")
    save_artifact(artifact, tmp_path)
    tampered = rubric.model_copy(update={"content_hash": "f" * 64})
    with pytest.raises(CalibrationStaleError):
        check_calibration(tmp_path, judge, [tampered])


def test_check_calibration_stale_on_different_judge(tmp_path: Path) -> None:
    artifact = run_calibration(
        _judge(), get_rubric("context_precision"), FIXTURES / "context_precision_labels.jsonl"
    )
    save_artifact(artifact, tmp_path)
    other_judge = ScriptedJudge(
        {"context_precision": PrecisionOut(verdict=1)},
        judge_model_id="different-judge",
        judge_family="glm",
    )
    with pytest.raises(CalibrationStaleError):
        check_calibration(tmp_path, other_judge, [get_rubric("context_precision")])


def test_committed_fixture_artifact_loads() -> None:
    loaded = load_artifacts(FIXTURES / "calibration")
    assert any(a.rubric_id == "context_precision" for a in loaded)
    assert all(isinstance(a, CalibrationArtifact) for a in loaded)
