# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""SubAgentSpec.route_lane_hint — additive, optional, Lane-typed, frozen
(ADR-0008, 1.5.0).

The hint is a narrow-only signal FROM the (untrusted) planner; it must be
optional (default None so every pre-1.5.0 constructor stays valid), coerce a
lane-shaped string, reject anything else (fail-loud), and stay frozen
(Checkpoint-A immutability). The narrow-only CLAMP behavior itself lives in
ne-runtime's dispatcher, not here — this file only proves the contract shape.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ne.contracts.runtime import SubAgentSpec
from ne.contracts.types import Lane


def test_route_lane_hint_defaults_to_none() -> None:
    # Backward-compat construction: the pre-1.5.0 kwarg set stays valid.
    spec = SubAgentSpec(agent="a", task="t")
    assert spec.route_lane_hint is None


def test_route_lane_hint_accepts_lane_and_coerces_str() -> None:
    spec = SubAgentSpec(agent="a", task="t", route_lane_hint="private-vllm")  # type: ignore[arg-type]
    assert spec.route_lane_hint is Lane.PRIVATE_VLLM


def test_route_lane_hint_rejects_unknown_lane() -> None:
    with pytest.raises(ValidationError):
        SubAgentSpec(agent="a", task="t", route_lane_hint="cloud")  # type: ignore[arg-type]


def test_route_lane_hint_frozen() -> None:
    spec = SubAgentSpec(agent="a", task="t", route_lane_hint=Lane.PRIVATE_VLLM)
    with pytest.raises(ValidationError):
        spec.route_lane_hint = None  # type: ignore[misc]
