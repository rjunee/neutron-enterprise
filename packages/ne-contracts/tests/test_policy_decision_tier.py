# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""PolicyDecision.tier — additive, optional, frozen (ADR-0004, 1.2.0).

The decided tier is the bundle-decided approval tier carried to the HITL gate.
It must be optional (default None so every pre-1.2.0 constructor stays valid),
round-trip through model_dump/model_validate, and stay frozen (Checkpoint-A
immutability). ``obligations`` — present since 1.0.0 — is regression-pinned.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ne.contracts.governance import DecisionEffect, PolicyDecision


def test_tier_field_exists_optional_default_none() -> None:
    # Backward-compat construction: the pre-1.2.0 kwarg set stays valid.
    decision = PolicyDecision(effect=DecisionEffect.ALLOW, policy_version="v1")
    assert decision.tier is None


def test_tier_round_trips() -> None:
    decision = PolicyDecision(effect=DecisionEffect.REQUIRE_APPROVAL, policy_version="v1", tier=3)
    dumped = decision.model_dump()
    assert dumped["tier"] == 3
    restored = PolicyDecision.model_validate(dumped)
    assert restored.tier == 3


def test_tier_accepts_none_explicitly() -> None:
    decision = PolicyDecision(effect=DecisionEffect.DENY, policy_version="v1", tier=None)
    assert decision.tier is None


def test_model_stays_frozen() -> None:
    decision = PolicyDecision(effect=DecisionEffect.ALLOW, policy_version="v1", tier=2)
    with pytest.raises(ValidationError):
        decision.tier = 3  # type: ignore[misc]


def test_obligations_field_still_present_and_frozen() -> None:
    decision = PolicyDecision(
        effect=DecisionEffect.REQUIRE_APPROVAL,
        policy_version="v1",
        obligations={"second_check": True},
        tier=3,
    )
    assert dict(decision.obligations) == {"second_check": True}
    with pytest.raises(ValidationError):
        decision.obligations = {}  # type: ignore[misc]
