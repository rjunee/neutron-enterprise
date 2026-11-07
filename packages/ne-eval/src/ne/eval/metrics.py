# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Native RAGAS-methodology metrics (SPEC §11 decision).

The RAGAS *library* is excluded (its heavy ML closure — scipy/langchain/openai-SDK/
datasets — trips the C4 license gate); its metric *algorithms* are reimplemented
here over the platform's own pluggable :class:`~ne.eval.judge.Judge`. Four metrics:

* ``recall_at_k`` — DETERMINISTIC, no judge: fraction of relevant documents found
  in the top-k retrieved chunks. Items with no relevant-document labels are
  EXCLUDED from the metric's mean.
* ``faithfulness`` — decompose the answer into atomic statements, then judge each
  as supported/unsupported by the contexts; score = supported / total.
* ``context_precision`` — one usefulness verdict per context; score is the
  rank-weighted mean precision over useful contexts.
* ``context_recall`` — decompose the *reference* answer into statements, then
  judge each as attributable to the contexts; score = attributed / total.

Every judge fault inside an item is caught and turned into a 0.0 score with an
error string (fail-closed) — a flaky judge never crashes the run; the run-level
``judge_error_rate`` (computed by the suite) is the kill-switch.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field

from ne.contracts.types import RetrievalResult
from ne.eval.errors import JudgeError, JudgeOutputError
from ne.eval.golden import GoldenItem
from ne.eval.judge import (
    Judge,
    PrecisionOut,
    RecallClassificationsOut,
    StatementsOut,
    VerdictsOut,
)
from ne.eval.rubrics import get_rubric

DEFAULT_TOP_K = 10
_CONTEXT_SEP = "\n---\n"

#: The judge rubrics each metric depends on (empty for the deterministic metric).
#: Drives which calibration artifacts a run requires (see the suite / calibration).
METRIC_RUBRICS: dict[str, tuple[str, ...]] = {
    "recall_at_10": (),
    "faithfulness": ("faithfulness_statements", "faithfulness_verdicts"),
    "context_precision": ("context_precision",),
    "context_recall": ("faithfulness_statements", "context_recall"),
}

#: The metric names this harness knows how to compute.
KNOWN_METRICS: tuple[str, ...] = tuple(METRIC_RUBRICS)


def is_judged_metric(metric: str) -> bool:
    """True if ``metric`` requires a judge (i.e. depends on one or more rubrics)."""
    return bool(METRIC_RUBRICS.get(metric))


class ItemScore(BaseModel):
    """The score a single metric assigns to a single golden item."""

    model_config = ConfigDict(frozen=True)

    item_id: str
    score: float
    error: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


def mean_score(scores: Sequence[ItemScore]) -> float | None:
    """Arithmetic mean over non-excluded item scores; ``None`` if all excluded/empty."""
    included = [s for s in scores if not s.detail.get("excluded")]
    if not included:
        return None
    return sum(s.score for s in included) / len(included)


def recall_at_k(
    golden: GoldenItem, retrieved: RetrievalResult, k: int = DEFAULT_TOP_K
) -> ItemScore:
    """Fraction of relevant documents appearing among the top-k retrieved chunks."""
    relevant = set(golden.relevant_document_ids)
    if not relevant:
        return ItemScore(
            item_id=golden.id,
            score=0.0,
            detail={"excluded": True, "reason": "no-relevant-documents"},
        )
    ranked = sorted(retrieved.chunks, key=lambda sc: sc.score, reverse=True)
    top = ranked[:k]
    retrieved_docs = {sc.chunk.document_id for sc in top}
    hit = relevant & retrieved_docs
    return ItemScore(
        item_id=golden.id,
        score=len(hit) / len(relevant),
        detail={
            "excluded": False,
            "relevant": sorted(relevant),
            "found": sorted(hit),
            "k": k,
        },
    )


async def faithfulness(
    judge: Judge,
    *,
    item_id: str,
    question: str,
    answer: str,
    contexts: Sequence[str],
) -> ItemScore:
    """Fraction of answer statements supported by the contexts (judge-scored)."""
    try:
        stmts = cast(
            StatementsOut,
            await judge.judge(
                get_rubric("faithfulness_statements"),
                {"question": question, "answer": answer},
                StatementsOut,
            ),
        )
        statements = list(stmts.statements)
        if not statements:
            return ItemScore(item_id=item_id, score=0.0, error="no-statements")
        verdicts_out = cast(
            VerdictsOut,
            await judge.judge(
                get_rubric("faithfulness_verdicts"),
                {
                    "contexts": _CONTEXT_SEP.join(contexts),
                    "statements": json.dumps(statements, ensure_ascii=False),
                },
                VerdictsOut,
            ),
        )
        verdicts = list(verdicts_out.verdicts)
        if len(verdicts) != len(statements):
            raise JudgeOutputError(
                f"faithfulness: {len(verdicts)} verdicts for {len(statements)} statements"
            )
        supported = sum(1 for v in verdicts if v.verdict == 1)
        return ItemScore(
            item_id=item_id,
            score=supported / len(statements),
            detail={"statements": len(statements), "supported": supported},
        )
    except JudgeError as exc:
        return ItemScore(item_id=item_id, score=0.0, error=str(exc))


async def context_precision(
    judge: Judge,
    *,
    item_id: str,
    question: str,
    reference_answer: str,
    contexts: Sequence[str],
) -> ItemScore:
    """Rank-weighted precision: Σ (precision@i · v_i) / Σ v_i over ranked contexts."""
    try:
        verdicts: list[int] = []
        for ctx in contexts:
            out = cast(
                PrecisionOut,
                await judge.judge(
                    get_rubric("context_precision"),
                    {
                        "question": question,
                        "reference_answer": reference_answer,
                        "contexts": ctx,
                    },
                    PrecisionOut,
                ),
            )
            verdicts.append(1 if out.verdict == 1 else 0)
        denom = sum(verdicts)
        if denom == 0:
            return ItemScore(item_id=item_id, score=0.0, detail={"verdicts": verdicts})
        numer = 0.0
        for i, v in enumerate(verdicts, start=1):
            if v:
                precision_at_i = sum(verdicts[:i]) / i
                numer += precision_at_i
        return ItemScore(item_id=item_id, score=numer / denom, detail={"verdicts": verdicts})
    except JudgeError as exc:
        return ItemScore(item_id=item_id, score=0.0, error=str(exc))


async def context_recall(
    judge: Judge,
    *,
    item_id: str,
    question: str,
    reference_answer: str,
    contexts: Sequence[str],
) -> ItemScore:
    """Fraction of reference-answer statements attributable to the contexts."""
    try:
        # Reuse the faithfulness_statements rubric to decompose the REFERENCE answer.
        stmts = cast(
            StatementsOut,
            await judge.judge(
                get_rubric("faithfulness_statements"),
                {"question": question, "answer": reference_answer},
                StatementsOut,
            ),
        )
        statements = list(stmts.statements)
        if not statements:
            return ItemScore(item_id=item_id, score=0.0, error="no-statements")
        cls = cast(
            RecallClassificationsOut,
            await judge.judge(
                get_rubric("context_recall"),
                {
                    "contexts": _CONTEXT_SEP.join(contexts),
                    "statements": json.dumps(statements, ensure_ascii=False),
                },
                RecallClassificationsOut,
            ),
        )
        classifications = list(cls.classifications)
        if len(classifications) != len(statements):
            raise JudgeOutputError(
                f"context_recall: {len(classifications)} classifications for "
                f"{len(statements)} statements"
            )
        attributed = sum(1 for c in classifications if c.attributed == 1)
        return ItemScore(
            item_id=item_id,
            score=attributed / len(statements),
            detail={"statements": len(statements), "attributed": attributed},
        )
    except JudgeError as exc:
        return ItemScore(item_id=item_id, score=0.0, error=str(exc))
