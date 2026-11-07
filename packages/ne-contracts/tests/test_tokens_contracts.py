# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Contract tests for ne.contracts.tokens (NE-CTXB-1)."""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from ne.contracts.tokens import (
    DEFAULT_BYTES_PER_TOKEN,
    DEFAULT_PER_CALL_OVERHEAD,
    ByteTokenEstimator,
    ContextBudget,
    TokenEstimator,
)

# PYRIGHT proves structural conformance at type-check time (plain Protocol, no
# isinstance). If ByteTokenEstimator ever stops satisfying TokenEstimator, this
# module-level annotation fails pyright, which is a check-command gate.
_conforms: TokenEstimator = ByteTokenEstimator()


def test_byte_estimator_formula() -> None:
    est = ByteTokenEstimator(bytes_per_token=3.0, per_call_overhead=8)
    # Empty text -> just the overhead.
    assert est.estimate("") == 8
    # 10 ASCII bytes / 3.0 -> ceil(3.333) = 4, + 8 = 12 (non-integer division).
    assert est.estimate("abcdefghij") == math.ceil(10 / 3.0) + 8 == 12
    # A single 3-byte CJK char counts as 3 bytes -> ceil(3/3.0) = 1, + 8 = 9.
    assert len("中".encode()) == 3
    assert est.estimate("中") == 9


def test_byte_estimator_deterministic() -> None:
    est = ByteTokenEstimator()
    text = "the quick brown fox 中文 🚀 urn:ne:entity:acct-00042"
    assert est.estimate(text) == est.estimate(text)


def test_invalid_params_raise() -> None:
    with pytest.raises(ValueError, match="bytes_per_token must be a finite value > 0"):
        ByteTokenEstimator(bytes_per_token=0)
    with pytest.raises(ValueError, match="bytes_per_token must be a finite value > 0"):
        ByteTokenEstimator(bytes_per_token=-1.0)
    # Non-finite divisors must fail loud at construction (C1): inf would collapse
    # every estimate to just the overhead (disabling budget enforcement) and nan
    # would defer a math.ceil crash to the first estimate() call.
    with pytest.raises(ValueError, match="bytes_per_token must be a finite value > 0"):
        ByteTokenEstimator(bytes_per_token=float("inf"))
    with pytest.raises(ValueError, match="bytes_per_token must be a finite value > 0"):
        ByteTokenEstimator(bytes_per_token=float("nan"))
    with pytest.raises(ValueError, match="per_call_overhead must be >= 0"):
        ByteTokenEstimator(per_call_overhead=-1)


def test_byte_estimator_satisfies_protocol() -> None:
    # The pyright proof is the module-level _conforms annotation above; this
    # runtime assertion documents the intent for readers.
    assert _conforms.estimate("hi") == ByteTokenEstimator().estimate("hi")
    assert ByteTokenEstimator().bytes_per_token == DEFAULT_BYTES_PER_TOKEN
    assert ByteTokenEstimator().per_call_overhead == DEFAULT_PER_CALL_OVERHEAD


def test_context_budget_input_budget() -> None:
    budget = ContextBudget(context_window=32768, max_output_tokens=4096, overhead_margin=1024)
    assert budget.input_budget == 27648


def test_context_budget_frozen() -> None:
    budget = ContextBudget(context_window=32768, max_output_tokens=4096, overhead_margin=1024)
    with pytest.raises(ValidationError):
        budget.context_window = 65536  # type: ignore[misc]


def test_context_budget_extra_forbid() -> None:
    with pytest.raises(ValidationError):
        ContextBudget(
            context_window=32768,
            max_output_tokens=4096,
            overhead_margin=1024,
            unexpected="nope",  # type: ignore[call-arg]
        )


def test_context_budget_validation() -> None:
    with pytest.raises(ValidationError):
        ContextBudget(context_window=0, max_output_tokens=4096, overhead_margin=1024)
    with pytest.raises(ValidationError):
        ContextBudget(context_window=32768, max_output_tokens=0, overhead_margin=1024)
    with pytest.raises(ValidationError):
        ContextBudget(context_window=32768, max_output_tokens=4096, overhead_margin=-1)
    # Impossible budget: output + margin swallows the whole window.
    with pytest.raises(ValidationError):
        ContextBudget(context_window=1024, max_output_tokens=1024, overhead_margin=0)
    with pytest.raises(ValidationError):
        ContextBudget(context_window=1024, max_output_tokens=512, overhead_margin=512)
