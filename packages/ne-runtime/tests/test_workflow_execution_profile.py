# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""PRE-010D version/hash/typed-input/fixed-plan execution profile proof."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import pytest
from pydantic import ValidationError

from ne.runtime.runner import run_deterministic_profile
from ne.runtime.workflow_execution_profile import (
    MERIDIAN_B1_DISASTER_HARDSHIP_PLAN,
    MERIDIAN_B1_DISASTER_HARDSHIP_PROFILE,
    MERIDIAN_B2_CURRENT_SOP_PLAN,
    MERIDIAN_B2_CURRENT_SOP_PROFILE,
    MERIDIAN_B3_FABRICATION_REFUSAL_PLAN,
    MERIDIAN_B3_FABRICATION_REFUSAL_PROFILE,
    MERIDIAN_S9_OVERDUE_CREDIT_PLAN,
    MERIDIAN_S9_OVERDUE_CREDIT_PROFILE,
    ExecutionStrategy,
    MeridianB1WorkflowInput,
    WorkflowExecutionProfile,
    WorkflowPlanStep,
)

_PROFILE_HASH = "e90381d94480ee1d01f028380be39595b63c4c632d6aff89738b49a440e667e0"
_B2_PROFILE_HASH = "7209a37006427bdc938c34f8e8c3ce798953a1aefa031bb6c81c5d32ffc97fce"
_B3_PROFILE_HASH = "5a70614b9270673416f4f5c521b4d3541a22ff131813d00bf925ddbdc2f6e23a"
_S9_PROFILE_HASH = "f36c68400c64dbbd49f02f38e52cf3face440f9607485de126de5031a60f4708"


def test_meridian_profile_freezes_exact_version_hash_schema_inputs_and_plan() -> None:
    profile = MERIDIAN_B1_DISASTER_HARDSHIP_PROFILE

    assert (profile.workflow_id, profile.version, profile.config_hash, profile.strategy) == (
        "meridian-b1-disaster-hardship",
        1,
        _PROFILE_HASH,
        ExecutionStrategy.DETERMINISTIC,
    )
    assert profile.canonical_config_hash() == _PROFILE_HASH
    assert profile.input_schema["additionalProperties"] is False
    assert profile.input_schema["required"] == ["max_subjects"]
    assert profile.input_schema["properties"]["max_subjects"] == {
        "maximum": 5,
        "minimum": 1,
        "title": "Max Subjects",
        "type": "integer",
    }
    assert dict(profile.fixed_inputs) == {
        "counties": ("Columbia", "Jefferson", "Richmond"),
        "payment_window_end": "2026-09-01",
        "payment_window_start": "2026-08-18",
    }
    assert tuple(step.ordinal for step in profile.fixed_plan) == (1, 2, 3, 4, 5)
    assert tuple(step.action for step in profile.fixed_plan) == (
        "read.kg.customers_in_counties",
        "read.kg.autoloans_for_customer",
        "read.kg.payments_due_window",
        "read.kg.servicing_notes_for_loan",
        "affected-active-due-no-prior-offer-no-repossession",
    )


@pytest.mark.parametrize(
    ("profile", "workflow_id", "profile_hash", "plan", "actions"),
    [
        (
            MERIDIAN_B2_CURRENT_SOP_PROFILE,
            "meridian-b2-current-sop",
            _B2_PROFILE_HASH,
            MERIDIAN_B2_CURRENT_SOP_PLAN,
            (
                "read.kg.disputes_directory",
                "read.kg.open_disputes_by_status",
                "apply-one-current-sop-to-complete-open-dispute-set",
            ),
        ),
        (
            MERIDIAN_B3_FABRICATION_REFUSAL_PROFILE,
            "meridian-b3-fabrication-refusal",
            _B3_PROFILE_HASH,
            MERIDIAN_B3_FABRICATION_REFUSAL_PLAN,
            (
                "read.kg.disputes_directory",
                "refuse-late-credit-total-and-compute-recorded-credit-control",
            ),
        ),
        (
            MERIDIAN_S9_OVERDUE_CREDIT_PROFILE,
            "meridian-s9-overdue-provisional-credit",
            _S9_PROFILE_HASH,
            MERIDIAN_S9_OVERDUE_CREDIT_PLAN,
            (
                "read.kg.overdue_provisional_credit_disputes",
                "recheck-open-no-credit-and-as-of-past-materialised-deadline",
            ),
        ),
    ],
)
def test_b2_b3_profiles_freeze_exact_hash_empty_server_inputs_and_plans(
    profile: WorkflowExecutionProfile,
    workflow_id: str,
    profile_hash: str,
    plan: tuple[WorkflowPlanStep, ...],
    actions: tuple[str, ...],
) -> None:
    assert (profile.workflow_id, profile.version, profile.config_hash, profile.strategy) == (
        workflow_id,
        1,
        profile_hash,
        ExecutionStrategy.DETERMINISTIC,
    )
    assert profile.canonical_config_hash() == profile_hash
    assert profile.fixed_inputs == ()
    assert profile.fixed_plan == plan
    assert tuple(step.action for step in plan) == actions
    assert profile.input_schema["additionalProperties"] is False
    assert profile.input_schema["required"] == ["max_subjects"]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"max_subjects": 0},
        {"max_subjects": 6},
        {"max_subjects": "5"},
        {"max_subjects": 5, "as_of_date": "2026-08-22"},
    ],
)
@pytest.mark.parametrize(
    "profile",
    [
        MERIDIAN_B1_DISASTER_HARDSHIP_PROFILE,
        MERIDIAN_B2_CURRENT_SOP_PROFILE,
        MERIDIAN_B3_FABRICATION_REFUSAL_PROFILE,
        MERIDIAN_S9_OVERDUE_CREDIT_PROFILE,
    ],
)
def test_meridian_profiles_typed_input_rejects_missing_bounds_coercion_and_extras(
    payload: dict[str, Any], profile: WorkflowExecutionProfile
) -> None:
    with pytest.raises(ValidationError):
        profile.validate_input(payload)


def test_plan_change_without_version_and_hash_movement_fails_boot_validation() -> None:
    changed = (*MERIDIAN_B1_DISASTER_HARDSHIP_PLAN[:-1],)

    with pytest.raises(ValueError, match="config hash mismatch"):
        replace(MERIDIAN_B1_DISASTER_HARDSHIP_PROFILE, fixed_plan=changed)


def test_canonical_hash_is_order_independent_for_frozen_input_declaration() -> None:
    profile = WorkflowExecutionProfile.define(
        workflow_id="meridian-b1-disaster-hardship",
        version=1,
        input_type=MeridianB1WorkflowInput,
        fixed_inputs={
            "payment_window_start": "2026-08-18",
            "counties": ("Columbia", "Jefferson", "Richmond"),
            "payment_window_end": "2026-09-01",
        },
        fixed_plan=MERIDIAN_B1_DISASTER_HARDSHIP_PLAN,
    )

    assert profile.config_hash == _PROFILE_HASH


def test_model_judgment_plan_step_must_name_bounded_role() -> None:
    with pytest.raises(ValidationError, match="bounded model role"):
        WorkflowPlanStep(
            ordinal=1,
            step_id="draft",
            kind="model-judgment",
            action="draft-summary",
        )


def test_deterministic_adapter_validates_then_calls_only_real_executor() -> None:
    received: list[MeridianB1WorkflowInput] = []

    async def actual_executor(value: Any) -> str:
        assert isinstance(value, MeridianB1WorkflowInput)
        received.append(value)
        return f"selected:{value.max_subjects}"

    result = asyncio.run(
        run_deterministic_profile(
            profile=MERIDIAN_B1_DISASTER_HARDSHIP_PROFILE,
            inputs={"max_subjects": 3},
            executor=actual_executor,
        )
    )

    assert result == "selected:3"
    assert received == [MeridianB1WorkflowInput(max_subjects=3)]


def test_deterministic_failure_is_not_routed_to_an_agentic_fallback() -> None:
    calls = 0

    async def failing_executor(_value: Any) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("deterministic failure")

    with pytest.raises(RuntimeError, match="deterministic failure"):
        asyncio.run(
            run_deterministic_profile(
                profile=MERIDIAN_B1_DISASTER_HARDSHIP_PROFILE,
                inputs={"max_subjects": 1},
                executor=failing_executor,
            )
        )

    assert calls == 1
