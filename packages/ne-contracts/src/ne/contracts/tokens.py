# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Context-budget PRIMITIVE layer.

This module defines the pure-stdlib token-estimation primitives and the frozen
:class:`ContextBudget` value type that Layer-2 (ne-runtime trim) and Layer-1
(ne-gateway pre-flight) both derive from. It has ZERO egress and no dependency
beyond stdlib + pydantic, so it stays importable by ne-runtime, ne-gateway, and
ne-brain-ontology under the existing import-linter lanes (.importlinter C3).

Single source of truth: the estimator PARAMETERS
(``bytes_per_token`` / ``per_call_overhead`` / margin policy) are OWNED by
``GatewaySettings`` downstream (NE-CTXB-2a). This module only defines the
primitive and its module-level DEFAULTS; the platform composition threads a
single configured estimator into both layers so they never disagree. Downstream
units MUST inherit :data:`DEFAULT_BYTES_PER_TOKEN` from here — do NOT hard-code
the design prose's original ``3.0``, which the vendored oracle refuted (ADR-0012).
"""

from __future__ import annotations

import math
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

# The oracle-selected default (ADR-0012). Design §2.3 proposed 3.0; the vendored
# multi-script token oracle (tests/fixtures/token_oracle/goldens.json, checked-in
# REAL Qwen3 + o200k_base counts) refuted it — demo-shaped urn:ne:entity table
# rows tokenize on Qwen3 as densely as ~1.77 bytes/token — so per design §2.3's
# own contingency the divisor was lowered to the largest ladder rung that keeps
# the estimator one-sided-conservative with >=10% generation-time headroom on
# every fixture. This value is PINNED to the goldens by
# test_token_estimator_oracle.py::test_goldens_are_real_and_complete — it is not
# a free parameter, it is the oracle's output.
DEFAULT_BYTES_PER_TOKEN: float = 1.5
DEFAULT_PER_CALL_OVERHEAD: int = 8


class TokenEstimator(Protocol):
    """Estimate the token count of a text for context-budget arithmetic.

    A plain structural Protocol (not ``runtime_checkable`` — house style, cf.
    identity.py). Conformance is proven at type-check time, not by isinstance.
    """

    def estimate(self, text: str) -> int: ...


class ByteTokenEstimator:
    """``ceil(len(text.encode("utf-8")) / bytes_per_token) + per_call_overhead``.

    Byte-based (not char-based) so it is one-sided-conservative across scripts:
    CJK (3 UTF-8 bytes/char) estimates roughly one token per character, emoji at
    least ~1.3. This one-sidedness (``estimate(text) >= actual_tokens``) is NOT
    an argument in this docstring — it TRACES TO the vendored real-tokenizer
    oracle test ``tests/test_token_estimator_oracle.py``, which asserts it per
    script class over checked-in Qwen3 + o200k_base counts and pins the default
    divisor to the oracle's selection.
    """

    def __init__(
        self,
        bytes_per_token: float = DEFAULT_BYTES_PER_TOKEN,
        per_call_overhead: int = DEFAULT_PER_CALL_OVERHEAD,
    ) -> None:
        if not math.isfinite(bytes_per_token) or bytes_per_token <= 0:
            # Reject inf/nan too: inf silently collapses every estimate to just
            # per_call_overhead (disabling budget enforcement) and nan defers a
            # crash to the first estimate() call. Fail loud at construction.
            raise ValueError("bytes_per_token must be a finite value > 0")
        if per_call_overhead < 0:
            raise ValueError("per_call_overhead must be >= 0")
        self.bytes_per_token = bytes_per_token
        self.per_call_overhead = per_call_overhead

    def estimate(self, text: str) -> int:
        return math.ceil(len(text.encode("utf-8")) / self.bytes_per_token) + self.per_call_overhead


class ContextBudget(BaseModel):
    """The input-token budget derived from a serving window (design §2.2).

    Frozen and ``extra="forbid"``: a boot-time misconfiguration (output reserve +
    margin swallowing the whole window) raises at construction — the fail-loud
    posture of design §2.2, not a silent runtime surprise.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    context_window: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)
    overhead_margin: int = Field(ge=0)

    @model_validator(mode="after")
    def _input_budget_must_be_positive(self) -> ContextBudget:
        if self.max_output_tokens + self.overhead_margin >= self.context_window:
            # Counts only (C12): no free text beyond the field values.
            raise ValueError(
                "input budget must be positive: "
                f"context_window={self.context_window} "
                f"max_output_tokens={self.max_output_tokens} "
                f"overhead_margin={self.overhead_margin}"
            )
        return self

    @property
    def input_budget(self) -> int:
        return self.context_window - self.max_output_tokens - self.overhead_margin
