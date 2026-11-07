# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The RAG evaluation suite (implements the frozen ``ne.contracts.EvalSuite``).

``run(config_hash=...)`` enforces the judge policy fail-closed BEFORE any scoring:
if a judged metric is requested there must be a judge, it must be cross-family
against the evaluated model, and every judged rubric must have a matching
calibration artifact. Only then does it retrieve → answer → score each golden
item, aggregate, evaluate thresholds, and (optionally) persist an append-only
eval-run record stamped with the caller-supplied ``config_hash``.

``EvalTarget`` is the seam NE-025 fills with the live retrieval + answer pipeline;
tests fill it with ``ne.eval.testing.ScriptedTarget``.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from ne.contracts.evalobs import EvalRunResult
from ne.contracts.types import RetrievalResult
from ne.eval.calibration import check_calibration
from ne.eval.errors import EvalConfigError
from ne.eval.golden import GoldenSet, golden_set_hash
from ne.eval.judge import Judge, enforce_cross_family
from ne.eval.metrics import (
    METRIC_RUBRICS,
    ItemScore,
    context_precision,
    context_recall,
    faithfulness,
    is_judged_metric,
    mean_score,
    recall_at_k,
)
from ne.eval.rubrics import Rubric, get_rubric
from ne.eval.store import EvalRunRecord, EvalRunStore
from ne.eval.thresholds import (
    DEFAULT_THRESHOLDS,
    MetricStatus,
    ThresholdSpec,
    evaluate_thresholds,
    gate_passed,
)

_ALL_METRICS = ("recall_at_10", "faithfulness", "context_precision", "context_recall")


class EvalTarget(Protocol):
    """The system under evaluation: retrieve contexts, then answer over them."""

    async def retrieve(self, question: str) -> RetrievalResult: ...

    async def answer(self, question: str, contexts: Sequence[str]) -> str: ...


def _rubrics_for(metrics: Sequence[str]) -> list[Rubric]:
    rubric_ids = sorted({rid for metric in metrics for rid in METRIC_RUBRICS.get(metric, ())})
    return [get_rubric(rid) for rid in rubric_ids]


class RagEvalSuite:
    """A named RAG eval suite (satisfies the frozen ``ne.contracts.EvalSuite``)."""

    def __init__(
        self,
        *,
        name: str = "rag",
        golden: GoldenSet,
        target: EvalTarget,
        judge: Judge | None,
        thresholds: Sequence[ThresholdSpec] = DEFAULT_THRESHOLDS,
        metrics: Sequence[str] = _ALL_METRICS,
        evaluated_family: str = "",
        calibration_dir: str = "calibration",
        store: EvalRunStore | None = None,
        top_k: int = 10,
        max_judge_error_rate: float = 0.2,
    ) -> None:
        self._name = name
        self._golden = golden
        self._target = target
        self._judge = judge
        self._thresholds = tuple(thresholds)
        self._metrics = tuple(metrics)
        self._evaluated_family = evaluated_family
        self._calibration_dir = calibration_dir
        self._store = store
        self._top_k = top_k
        self._max_judge_error_rate = max_judge_error_rate

    @property
    def name(self) -> str:
        return self._name

    async def run(self, *, config_hash: str) -> EvalRunResult:
        judged_metrics = [m for m in self._metrics if is_judged_metric(m)]
        rubrics_used = _rubrics_for(judged_metrics)

        # (1) Fail-closed judge-policy enforcement — BEFORE any target/judge call.
        if judged_metrics:
            if self._judge is None:
                raise EvalConfigError(
                    f"judged metrics {judged_metrics} requested but no judge configured"
                )
            enforce_cross_family(self._judge.judge_family, self._evaluated_family)
            check_calibration(self._calibration_dir, self._judge, rubrics_used)

        # (2) Retrieve → answer → score each golden item.
        scores: dict[str, list[ItemScore]] = {m: [] for m in self._metrics}
        judged_total = 0
        judged_errors = 0
        want_answer = "faithfulness" in self._metrics
        for item in self._golden.items:
            retrieved = await self._target.retrieve(item.question)
            ranked = sorted(retrieved.chunks, key=lambda sc: sc.score, reverse=True)
            contexts = [sc.chunk.text for sc in ranked[: self._top_k]]
            answer = await self._target.answer(item.question, contexts) if want_answer else ""
            for metric in self._metrics:
                item_score = await self._score_one(
                    metric,
                    item_id=item.id,
                    question=item.question,
                    reference_answer=item.reference_answer,
                    answer=answer,
                    contexts=contexts,
                    golden_item=item,
                    retrieved=retrieved,
                )
                scores[metric].append(item_score)
                if is_judged_metric(metric):
                    judged_total += 1
                    if item_score.error is not None:
                        judged_errors += 1

        # (3) Aggregate.
        metrics_out: dict[str, float] = {}
        for metric in self._metrics:
            mean = mean_score(scores[metric])
            if mean is not None:
                metrics_out[metric] = mean
        judge_error_rate = judged_errors / judged_total if judged_total else 0.0

        # (4) Thresholds + pass/fail (fail-closed on a flaky judge).
        statuses = evaluate_thresholds(metrics_out, self._thresholds)
        passed = gate_passed(statuses) and judge_error_rate <= self._max_judge_error_rate
        thresholds_map = {spec.metric: spec.threshold for spec in self._thresholds}

        # (5) Result stamped with the supplied config_hash.
        result = EvalRunResult(
            suite=self._name,
            config_hash=config_hash,
            metrics=metrics_out,
            thresholds=thresholds_map,
            passed=passed,
        )

        # (6) Persist an append-only run record.
        if self._store is not None:
            self._store.append(
                self._build_record(
                    config_hash=config_hash,
                    rubrics_used=rubrics_used,
                    metrics_out=metrics_out,
                    thresholds_map=thresholds_map,
                    statuses=statuses,
                    judge_error_rate=judge_error_rate,
                    passed=passed,
                )
            )
        return result

    async def _score_one(
        self,
        metric: str,
        *,
        item_id: str,
        question: str,
        reference_answer: str,
        answer: str,
        contexts: Sequence[str],
        golden_item: object,
        retrieved: RetrievalResult,
    ) -> ItemScore:
        if metric == "recall_at_10":
            from ne.eval.golden import GoldenItem

            assert isinstance(golden_item, GoldenItem)
            return recall_at_k(golden_item, retrieved, k=self._top_k)
        judge = self._judge
        assert judge is not None  # guaranteed: judged metrics required a judge above
        if metric == "faithfulness":
            return await faithfulness(
                judge, item_id=item_id, question=question, answer=answer, contexts=contexts
            )
        if metric == "context_precision":
            return await context_precision(
                judge,
                item_id=item_id,
                question=question,
                reference_answer=reference_answer,
                contexts=contexts,
            )
        if metric == "context_recall":
            return await context_recall(
                judge,
                item_id=item_id,
                question=question,
                reference_answer=reference_answer,
                contexts=contexts,
            )
        raise EvalConfigError(f"unknown metric {metric!r}")

    def _build_record(
        self,
        *,
        config_hash: str,
        rubrics_used: Sequence[Rubric],
        metrics_out: dict[str, float],
        thresholds_map: dict[str, float],
        statuses: dict[str, MetricStatus],
        judge_error_rate: float,
        passed: bool,
    ) -> EvalRunRecord:
        rubric_versions = {
            r.id: {"version": r.version, "content_hash": r.content_hash} for r in rubrics_used
        }
        judge = self._judge
        return EvalRunRecord(
            run_id=uuid4().hex,
            suite=self._name,
            config_hash=config_hash,
            golden_set_hash=golden_set_hash(self._golden),
            judge_model_id=judge.judge_model_id if judge is not None else None,
            judge_family=judge.judge_family if judge is not None else None,
            rubric_versions=rubric_versions,
            metrics=metrics_out,
            thresholds=thresholds_map,
            statuses={k: v.value for k, v in statuses.items()},
            judge_error_rate=judge_error_rate,
            passed=passed,
            created_at=datetime.now(UTC),
        )

    def run_sync(self, config_hash: str) -> EvalRunResult:
        """Synchronous convenience wrapper over :meth:`run` (repo asyncio.run convention)."""
        import asyncio

        return asyncio.run(self.run(config_hash=config_hash))
