# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Faithfulness metric tests (judge-scored, fail-closed)."""

from __future__ import annotations

import asyncio

import pytest

from ne.eval.judge import StatementsOut, VerdictItem, VerdictsOut
from ne.eval.metrics import faithfulness
from ne.eval.testing import ScriptedJudge


def _run(judge: ScriptedJudge) -> object:
    return asyncio.run(
        faithfulness(judge, item_id="i1", question="q", answer="a", contexts=["ctx one", "ctx two"])
    )


def test_two_of_three_supported() -> None:
    judge = ScriptedJudge(
        {
            "faithfulness_statements": StatementsOut(statements=("s1", "s2", "s3")),
            "faithfulness_verdicts": VerdictsOut(
                verdicts=(
                    VerdictItem(statement="s1", verdict=1),
                    VerdictItem(statement="s2", verdict=1),
                    VerdictItem(statement="s3", verdict=0),
                )
            ),
        }
    )
    score = _run(judge)
    assert score.score == pytest.approx(2 / 3)  # type: ignore[attr-defined]
    assert score.error is None  # type: ignore[attr-defined]


def test_empty_statements() -> None:
    judge = ScriptedJudge({"faithfulness_statements": StatementsOut(statements=())})
    score = _run(judge)
    assert score.score == 0.0  # type: ignore[attr-defined]
    assert score.error == "no-statements"  # type: ignore[attr-defined]


def test_verdict_count_mismatch() -> None:
    judge = ScriptedJudge(
        {
            "faithfulness_statements": StatementsOut(statements=("s1", "s2", "s3")),
            "faithfulness_verdicts": VerdictsOut(
                verdicts=(VerdictItem(statement="s1", verdict=1),)
            ),
        }
    )
    score = _run(judge)
    assert score.score == 0.0  # type: ignore[attr-defined]
    assert score.error is not None  # type: ignore[attr-defined]


def test_judge_output_error_is_fail_closed() -> None:
    judge = ScriptedJudge(
        {"faithfulness_statements": StatementsOut(statements=("s1",))},
        raise_for=["faithfulness_statements"],
    )
    score = _run(judge)
    assert score.score == 0.0  # type: ignore[attr-defined]
    assert score.error is not None  # type: ignore[attr-defined]
