# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Context-precision metric tests (rank-weighted formula, hand-computed)."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import pytest

from ne.eval.judge import PrecisionOut
from ne.eval.metrics import context_precision
from ne.eval.testing import ScriptedJudge


def _score(verdicts: Sequence[int]) -> float:
    judge = ScriptedJudge({"context_precision": [PrecisionOut(verdict=v) for v in verdicts]})
    contexts = [f"ctx {i}" for i in range(len(verdicts))]
    result = asyncio.run(
        context_precision(
            judge, item_id="i1", question="q", reference_answer="r", contexts=contexts
        )
    )
    return result.score


def test_rank_weighted_101() -> None:
    assert _score([1, 0, 1]) == pytest.approx(0.8333333, abs=1e-6)


def test_all_zero() -> None:
    assert _score([0, 0, 0]) == 0.0


def test_all_one() -> None:
    assert _score([1, 1]) == 1.0


def test_ordering_sensitivity() -> None:
    assert _score([0, 1]) < _score([1, 0])
