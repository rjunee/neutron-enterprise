# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Eval-run store tests: schema, append, newest-first list, append-only surface."""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa

from ne.eval.store import EvalRunRecord, EvalRunStore


def _record(run_id: str, *, suite: str = "rag", when: datetime) -> EvalRunRecord:
    return EvalRunRecord(
        run_id=run_id,
        suite=suite,
        config_hash="cfg-" + run_id,
        golden_set_hash="gs-hash",
        judge_model_id="qwen3.5-judge",
        judge_family="qwen",
        rubric_versions={"context_precision": {"version": "v1", "content_hash": "abc"}},
        metrics={"recall_at_10": 1.0},
        thresholds={"recall_at_10": 0.85},
        statuses={"recall_at_10": "pass"},
        judge_error_rate=0.0,
        passed=True,
        created_at=when,
    )


def test_append_and_round_trip(engine: sa.Engine) -> None:
    store = EvalRunStore(engine)
    rec = _record("r1", when=datetime(2026, 1, 1, tzinfo=UTC))
    store.append(rec)
    runs = store.list_runs()
    assert len(runs) == 1
    got = runs[0]
    assert got.config_hash == "cfg-r1"
    assert got.golden_set_hash == "gs-hash"
    assert got.rubric_versions == {"context_precision": {"version": "v1", "content_hash": "abc"}}
    assert got.statuses == {"recall_at_10": "pass"}
    assert got.metrics == {"recall_at_10": 1.0}


def test_list_newest_first_and_suite_filter(engine: sa.Engine) -> None:
    store = EvalRunStore(engine)
    store.append(_record("old", suite="rag", when=datetime(2026, 1, 1, tzinfo=UTC)))
    store.append(_record("new", suite="rag", when=datetime(2026, 6, 1, tzinfo=UTC)))
    store.append(_record("other", suite="qa", when=datetime(2026, 3, 1, tzinfo=UTC)))
    all_rag = store.list_runs(suite="rag")
    assert [r.run_id for r in all_rag] == ["new", "old"]
    assert [r.run_id for r in store.list_runs(suite="qa")] == ["other"]


def test_no_update_or_delete_surface(engine: sa.Engine) -> None:
    store = EvalRunStore(engine)
    assert not hasattr(store, "update")
    assert not hasattr(store, "delete")


def test_get_run_returns_record_and_none_for_unknown(engine: sa.Engine) -> None:
    store = EvalRunStore(engine)
    rec = _record("r1", when=datetime(2026, 1, 1, tzinfo=UTC))
    store.append(rec)
    got = store.get_run("r1")
    assert got is not None
    assert got.run_id == "r1"
    assert got.config_hash == "cfg-r1"
    assert got.passed is True
    assert store.get_run("does-not-exist") is None
