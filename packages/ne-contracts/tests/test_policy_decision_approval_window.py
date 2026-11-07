# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""PolicyDecision.approval_window_seconds — additive, optional, frozen (ADR-0030, 1.27.0).

The policy-resolved per-action approval window, carried FIRST-CLASS on the
decision — deliberately NOT an obligation, because ``obligations`` is the
Rego/Cedar parity surface and Cedar obligations are static per-rule
annotations. Must be optional (default ``None`` so every pre-1.27.0
constructor stays valid), round-trip through model_dump/model_validate, and
stay frozen (Checkpoint-A immutability).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ne.contracts.governance import DecisionEffect, PolicyDecision


def test_window_field_exists_optional_default_none() -> None:
    # Backward-compat construction: the pre-1.27.0 kwarg set stays valid.
    decision = PolicyDecision(effect=DecisionEffect.REQUIRE_APPROVAL, policy_version="v1", tier=2)
    assert decision.approval_window_seconds is None


def test_window_round_trips() -> None:
    decision = PolicyDecision(
        effect=DecisionEffect.REQUIRE_APPROVAL,
        policy_version="v1",
        tier=2,
        approval_window_seconds=14400,
    )
    dumped = decision.model_dump()
    assert dumped["approval_window_seconds"] == 14400.0
    restored = PolicyDecision.model_validate(dumped)
    assert restored.approval_window_seconds == 14400.0


def test_window_accepts_none_explicitly() -> None:
    decision = PolicyDecision(
        effect=DecisionEffect.ALLOW, policy_version="v1", tier=0, approval_window_seconds=None
    )
    assert decision.approval_window_seconds is None


def test_window_rejects_bool() -> None:
    """``True`` must never become a one-second window via bool→number coercion."""
    with pytest.raises(ValidationError):
        PolicyDecision(
            effect=DecisionEffect.REQUIRE_APPROVAL,
            policy_version="v1",
            tier=2,
            approval_window_seconds=True,  # type: ignore[arg-type]
        )


def test_model_stays_frozen() -> None:
    decision = PolicyDecision(
        effect=DecisionEffect.REQUIRE_APPROVAL,
        policy_version="v1",
        tier=2,
        approval_window_seconds=600.0,
    )
    with pytest.raises(ValidationError):
        decision.approval_window_seconds = 30.0  # type: ignore[misc]


def test_window_rejects_strings_numeric_or_not() -> None:
    """``"600"`` must never become a window via str→number lax coercion — a
    producer that serialises the window as text is the same defect class as
    ``True → 1.0``, and it must fail loudly, not be laundered into a deadline."""
    for bad in ("600", "14400.0", "1e400", "soon", ""):
        with pytest.raises(ValidationError):
            PolicyDecision(
                effect=DecisionEffect.REQUIRE_APPROVAL,
                policy_version="v1",
                tier=2,
                approval_window_seconds=bad,  # type: ignore[arg-type]
            )
