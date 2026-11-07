# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Tests for the regression-baseline ratchet, trend readout, and their CLI."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import sqlalchemy as sa

from ne.eval.cli import main
from ne.eval.store import EvalRunRecord, EvalRunStore, ensure_eval_schema
from ne.eval.trend import (
    BaselineRecord,
    check_regression,
    load_baseline,
    trend_rows,
    write_baseline,
)


def _baseline() -> BaselineRecord:
    return BaselineRecord(
        suite="rag-public",
        golden_set_hash="abc123",
        judge_model_id="ne-lexical-judge-v1",
        metrics={"recall_at_10": 0.98, "context_precision": 0.8336},
        tolerances={"recall_at_10": 0.02, "context_precision": 0.03},
    )


def test_write_load_round_trip_byte_stable(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    write_baseline(path, _baseline())
    first = path.read_text(encoding="utf-8")
    assert first.endswith("\n")
    loaded = load_baseline(path)
    write_baseline(path, loaded)
    assert path.read_text(encoding="utf-8") == first  # byte-stable
    assert loaded.metrics["recall_at_10"] == 0.98


def test_write_rounds_metrics(tmp_path: Path) -> None:
    path = tmp_path / "b.json"
    record = _baseline().model_copy(update={"metrics": {"recall_at_10": 0.833355131}})
    write_baseline(path, record)
    assert json.loads(path.read_text())["metrics"]["recall_at_10"] == 0.8334


def test_check_regression_semantics() -> None:
    baseline = _baseline()
    assert check_regression(baseline, {"recall_at_10": 0.98, "context_precision": 0.8336}) == []
    # within tolerance -> still clean
    assert check_regression(baseline, {"recall_at_10": 0.965, "context_precision": 0.81}) == []
    # beyond tolerance -> flagged
    dropped = check_regression(baseline, {"recall_at_10": 0.90, "context_precision": 0.8336})
    assert len(dropped) == 1 and "recall_at_10" in dropped[0]
    # missing metric -> flagged
    missing = check_regression(baseline, {"recall_at_10": 0.98})
    assert len(missing) == 1 and "context_precision" in missing[0]


def _store(tmp_path: Path) -> tuple[EvalRunStore, str]:
    url = f"sqlite:///{tmp_path / 'runs.db'}"
    engine = sa.create_engine(url)
    ensure_eval_schema(engine)
    return EvalRunStore(engine), url


def _record(run_id: str, when: datetime, recall: float, passed: bool) -> EvalRunRecord:
    return EvalRunRecord(
        run_id=run_id,
        suite="rag-public",
        config_hash="cfg",
        golden_set_hash="gs",
        judge_model_id="j",
        judge_family="heuristic-lexical",
        rubric_versions={},
        metrics={"recall_at_10": recall},
        thresholds={"recall_at_10": 0.85},
        statuses={"recall_at_10": "pass"},
        judge_error_rate=0.0,
        passed=passed,
        created_at=when,
    )


def test_trend_rows_oldest_first(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    t0 = datetime(2026, 7, 1, tzinfo=UTC)
    store.append(_record("r1", t0, 0.95, True))
    store.append(_record("r2", t0 + timedelta(days=1), 0.98, True))
    rows = trend_rows(store, "rag-public")
    assert [r[1]["recall_at_10"] for r in rows] == [0.95, 0.98]


def test_cli_baseline_write_and_check(tmp_path: Path) -> None:
    metrics_path = tmp_path / "metrics.json"
    metrics_path.write_text(json.dumps({"recall_at_10": 0.98, "context_precision": 0.8336}))
    baseline_path = tmp_path / "baseline.json"
    rc = main(
        [
            "baseline",
            "--write",
            "--path",
            str(baseline_path),
            "--suite",
            "rag-public",
            "--golden-set-hash",
            "abc123",
            "--judge-model",
            "ne-lexical-judge-v1",
            "--metrics-json",
            str(metrics_path),
            "--tolerance",
            "recall_at_10=0.02",
            "--tolerance",
            "context_precision=0.03",
        ]
    )
    assert rc == 0
    loaded = load_baseline(baseline_path)
    assert loaded.metrics == {"recall_at_10": 0.98, "context_precision": 0.8336}

    # clean check -> 0
    assert (
        main(
            [
                "baseline",
                "--check",
                "--path",
                str(baseline_path),
                "--metrics-json",
                str(metrics_path),
            ]
        )
        == 0
    )

    # regressed metrics -> 1
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"recall_at_10": 0.80, "context_precision": 0.8336}))
    assert (
        main(["baseline", "--check", "--path", str(baseline_path), "--metrics-json", str(bad)]) == 1
    )


def test_cli_trend_prints_rows(tmp_path: Path, capsys) -> None:
    store, url = _store(tmp_path)
    store.append(_record("r1", datetime(2026, 7, 1, tzinfo=UTC), 0.98, True))
    rc = main(["trend", "--db", url, "--suite", "rag-public"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "PASS" in out and "recall_at_10=0.9800" in out
