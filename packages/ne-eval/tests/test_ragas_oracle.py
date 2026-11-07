# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Dev-only RAGAS cross-validation oracle test (skip-if-unavailable, SPEC §11).

Feeds the SAME fixed judge verdicts to BOTH ragas 0.4.3 (via the isolated-venv
``cross_check.py`` subprocess) and our native ``ne.eval.metrics`` (via a
``ScriptedJudge``), and asserts the two agree to ``1e-6`` on faithfulness /
context_precision / context_recall for all 8 fixture cases. Availability is gated
by ``NE_REQUIRE_PUBLIC_BASELINE`` (NE-CKC6): when the flag is unset (local dev)
the oracle SKIPS if the ragas dev venv cannot be built (network-less runner) or
when ``NE_SKIP_RAGAS_ORACLE=1``; when ``NE_REQUIRE_PUBLIC_BASELINE=1`` (CI,
fail-closed) either condition is a HARD FAILURE — an unbuildable venv can no
longer silently skip the Checkpoint-C oracle, and ``NE_SKIP_RAGAS_ORACLE=1`` is
refused as a contradictory config (REQUIRE wins).
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, NoReturn

import pytest

from ne.eval.judge import (
    PrecisionOut,
    RecallClassificationItem,
    RecallClassificationsOut,
    StatementsOut,
    VerdictItem,
    VerdictsOut,
)
from ne.eval.metrics import context_precision, context_recall, faithfulness
from ne.eval.ragas_oracle import ensure_ragas_env, run_cross_check
from ne.eval.testing import ScriptedJudge

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "ragas_cross_check.json"
_TOLERANCE = 1e-6


@pytest.fixture(scope="module")
def oracle(
    baseline_guard: Callable[[str], NoReturn],
) -> tuple[dict[str, Any], dict[str, Any]]:
    if os.environ.get("NE_SKIP_RAGAS_ORACLE") == "1":
        baseline_guard("NE_SKIP_RAGAS_ORACLE=1 — ragas oracle skipped by request")
    try:
        ensure_ragas_env()
    except Exception as exc:
        baseline_guard(f"ragas dev venv unavailable ({exc!r})")
    fixture = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    return run_cross_check(fixture), fixture


def _native_faithfulness(case: dict[str, Any]) -> float:
    faith = case["faithfulness"]
    statements = list(faith["statements"])
    verdicts = list(faith["verdicts"])
    judge = ScriptedJudge(
        {
            "faithfulness_statements": StatementsOut(statements=tuple(statements)),
            "faithfulness_verdicts": VerdictsOut(
                verdicts=tuple(
                    VerdictItem(statement=s, verdict=v)
                    for s, v in zip(statements, verdicts, strict=True)
                )
            ),
        }
    )
    score = asyncio.run(
        faithfulness(
            judge,
            item_id=case["id"],
            question=case["question"],
            answer=case["answer"],
            contexts=case["contexts"],
        )
    )
    return score.score


def _native_context_precision(case: dict[str, Any]) -> float:
    verdicts = list(case["context_precision"]["verdicts"])
    judge = ScriptedJudge({"context_precision": [PrecisionOut(verdict=v) for v in verdicts]})
    score = asyncio.run(
        context_precision(
            judge,
            item_id=case["id"],
            question=case["question"],
            reference_answer=case["reference"],
            contexts=case["contexts"],
        )
    )
    return score.score


def _native_context_recall(case: dict[str, Any]) -> float:
    recall = case["context_recall"]
    statements = list(recall["statements"])
    attributed = list(recall["attributed"])
    judge = ScriptedJudge(
        {
            "faithfulness_statements": StatementsOut(statements=tuple(statements)),
            "context_recall": RecallClassificationsOut(
                classifications=tuple(
                    RecallClassificationItem(statement=s, attributed=a)
                    for s, a in zip(statements, attributed, strict=True)
                )
            ),
        }
    )
    score = asyncio.run(
        context_recall(
            judge,
            item_id=case["id"],
            question=case["question"],
            reference_answer=case["reference"],
            contexts=case["contexts"],
        )
    )
    return score.score


def test_native_metrics_match_ragas(oracle: tuple[dict[str, Any], dict[str, Any]]) -> None:
    ragas_result, fixture = oracle
    assert ragas_result["ragas_version"] == "0.4.3"
    ragas_by_id = {case["id"]: case for case in ragas_result["cases"]}
    assert len(ragas_by_id) == 8

    for case in fixture["cases"]:
        ragas_case = ragas_by_id[case["id"]]
        native = {
            "faithfulness": _native_faithfulness(case),
            "context_precision": _native_context_precision(case),
            "context_recall": _native_context_recall(case),
        }
        for metric, native_value in native.items():
            assert abs(native_value - ragas_case[metric]) <= _TOLERANCE, (
                case["id"],
                metric,
                native_value,
                ragas_case[metric],
            )
