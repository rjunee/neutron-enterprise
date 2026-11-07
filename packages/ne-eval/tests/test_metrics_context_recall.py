# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Context-recall metric tests (judge-scored, fail-closed)."""

from __future__ import annotations

import asyncio

import pytest

from ne.eval.judge import RecallClassificationItem, RecallClassificationsOut, StatementsOut
from ne.eval.metrics import context_recall
from ne.eval.testing import ScriptedJudge


def _run(judge: ScriptedJudge) -> object:
    return asyncio.run(
        context_recall(judge, item_id="i1", question="q", reference_answer="ref", contexts=["ctx"])
    )


def test_three_of_four_attributed() -> None:
    judge = ScriptedJudge(
        {
            "faithfulness_statements": StatementsOut(statements=("s1", "s2", "s3", "s4")),
            "context_recall": RecallClassificationsOut(
                classifications=(
                    RecallClassificationItem(statement="s1", attributed=1),
                    RecallClassificationItem(statement="s2", attributed=1),
                    RecallClassificationItem(statement="s3", attributed=1),
                    RecallClassificationItem(statement="s4", attributed=0),
                )
            ),
        }
    )
    score = _run(judge)
    assert score.score == pytest.approx(0.75)  # type: ignore[attr-defined]


def test_zero_statements() -> None:
    judge = ScriptedJudge({"faithfulness_statements": StatementsOut(statements=())})
    score = _run(judge)
    assert score.score == 0.0  # type: ignore[attr-defined]
    assert score.error is not None  # type: ignore[attr-defined]


def test_classification_count_mismatch() -> None:
    judge = ScriptedJudge(
        {
            "faithfulness_statements": StatementsOut(statements=("s1", "s2")),
            "context_recall": RecallClassificationsOut(
                classifications=(RecallClassificationItem(statement="s1", attributed=1),)
            ),
        }
    )
    score = _run(judge)
    assert score.score == 0.0  # type: ignore[attr-defined]
    assert score.error is not None  # type: ignore[attr-defined]
