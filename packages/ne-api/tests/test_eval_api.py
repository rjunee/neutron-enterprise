# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Eval read surface — shape, suite filter, limit bounds, 503 when unconfigured."""

from __future__ import annotations

from datetime import UTC, datetime

from ne.eval.store import EvalRunRecord

from .conftest import AppBuilder, get_state, run

_EVAL_KEYS = {
    "run_id",
    "suite",
    "config_hash",
    "golden_set_hash",
    "judge_model_id",
    "judge_family",
    "rubric_versions",
    "metrics",
    "thresholds",
    "statuses",
    "judge_error_rate",
    "passed",
    "created_at",
}


def _seed(h, *, run_id: str, suite: str) -> None:
    get_state(h.app).eval_store.append(  # type: ignore[union-attr]
        EvalRunRecord(
            run_id=run_id,
            suite=suite,
            config_hash="c",
            golden_set_hash="g",
            judge_model_id=None,
            judge_family=None,
            rubric_versions={},
            metrics={"recall": 0.9},
            thresholds={"recall": 0.8},
            statuses={"recall": "pass"},
            judge_error_rate=0.0,
            passed=True,
            created_at=datetime.now(UTC),
        )
    )


def test_eval_runs_shape(make_app: AppBuilder) -> None:
    h = make_app()
    _seed(h, run_id="r1", suite="retrieval-baseline")

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get("/api/v1/eval/runs", headers=h.bearer("u", groups=[]))
            assert r.status_code == 200
            body = r.json()
            assert set(body) == {"runs"}
            assert body["runs"]
            assert set(body["runs"][0]) == _EVAL_KEYS

    run(_t())


def test_eval_suite_filter_and_limit(make_app: AppBuilder) -> None:
    h = make_app()
    _seed(h, run_id="r1", suite="retrieval-baseline")
    _seed(h, run_id="r2", suite="other-suite")

    async def _t() -> None:
        async with h.client() as client:
            headers = h.bearer("u", groups=[])
            r = await client.get("/api/v1/eval/runs?suite=retrieval-baseline", headers=headers)
            assert r.status_code == 200
            suites = {run["suite"] for run in r.json()["runs"]}
            assert suites == {"retrieval-baseline"}

            r0 = await client.get("/api/v1/eval/runs?limit=0", headers=headers)
            assert r0.status_code == 422

    run(_t())


def test_eval_unconfigured_is_503(make_app: AppBuilder) -> None:
    h = make_app(eval_store_configured=False)

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get("/api/v1/eval/runs", headers=h.bearer("u", groups=[]))
            assert r.status_code == 503
            assert "eval store not configured" in r.json()["detail"]

    run(_t())


def test_eval_any_authenticated_principal(make_app: AppBuilder) -> None:
    h = make_app()
    _seed(h, run_id="r1", suite="s")

    async def _t() -> None:
        async with h.client() as client:
            # No roles needed — any authenticated principal reads.
            r = await client.get("/api/v1/eval/runs", headers=h.bearer("nobody", groups=[]))
            assert r.status_code == 200

    run(_t())
