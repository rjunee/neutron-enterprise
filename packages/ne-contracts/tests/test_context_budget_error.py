# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Tests for ContextBudgetExceededError (NE-CTXB-1) — C12 counts-only pin."""

from __future__ import annotations

import pytest

from ne.contracts.errors import ContextBudgetExceededError, NeContractsError


def test_fields_and_inheritance() -> None:
    err = ContextBudgetExceededError(
        stage="runtime-trim",
        estimated_tokens=31000,
        input_budget=27648,
        context_window=32768,
        dropped_turns=12,
    )
    assert isinstance(err, NeContractsError)
    assert err.stage == "runtime-trim"
    assert err.estimated_tokens == 31000
    assert err.input_budget == 27648
    assert err.context_window == 32768
    assert err.dropped_turns == 12


def test_message_is_counts_only_exact() -> None:
    err = ContextBudgetExceededError(
        stage="runtime-trim",
        estimated_tokens=31000,
        input_budget=27648,
        context_window=32768,
        dropped_turns=12,
    )
    assert str(err) == (
        "context budget exceeded at runtime-trim: "
        "estimated_tokens=31000 input_budget=27648 "
        "context_window=32768 dropped_turns=12"
    )


def test_dropped_turns_defaults_zero() -> None:
    err = ContextBudgetExceededError(
        stage="gateway-preflight",
        estimated_tokens=40000,
        input_budget=27648,
        context_window=32768,
    )
    assert err.dropped_turns == 0
    assert str(err) == (
        "context budget exceeded at gateway-preflight: "
        "estimated_tokens=40000 input_budget=27648 "
        "context_window=32768 dropped_turns=0"
    )


def test_invalid_stage_raises() -> None:
    with pytest.raises(ValueError, match="stage must be"):
        ContextBudgetExceededError(
            stage="other",  # type: ignore[arg-type]
            estimated_tokens=1,
            input_budget=1,
            context_window=1,
        )


def test_negative_counts_raise() -> None:
    with pytest.raises(ValueError, match="estimated_tokens must be >= 0"):
        ContextBudgetExceededError(
            stage="runtime-trim", estimated_tokens=-1, input_budget=0, context_window=0
        )
    with pytest.raises(ValueError, match="input_budget must be >= 0"):
        ContextBudgetExceededError(
            stage="runtime-trim", estimated_tokens=0, input_budget=-1, context_window=0
        )
    with pytest.raises(ValueError, match="context_window must be >= 0"):
        ContextBudgetExceededError(
            stage="runtime-trim", estimated_tokens=0, input_budget=0, context_window=-1
        )
    with pytest.raises(ValueError, match="dropped_turns must be >= 0"):
        ContextBudgetExceededError(
            stage="runtime-trim",
            estimated_tokens=0,
            input_budget=0,
            context_window=0,
            dropped_turns=-1,
        )
