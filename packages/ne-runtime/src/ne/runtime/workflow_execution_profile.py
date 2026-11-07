# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Versioned, self-authenticating deterministic Workflow execution profiles.

The public ``WorkflowView`` is presentation metadata.  This module is the internal
execution contract: it freezes the typed inputs, server-owned constants and ordered
plan that a deterministic Agent version actually runs.  A profile is constructible
only when its declared hash matches those canonical bytes, so changing a plan while
leaving its version/hash pins in place fails at boot.

Hybrid is intentionally absent.  PRE-010D has no typed ``AgenticStep`` or child-Run
contract, so the closed strategy parser rejects ``"hybrid"`` instead of admitting a
hidden engine.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "MERIDIAN_B1_DISASTER_HARDSHIP_PLAN",
    "MERIDIAN_B1_DISASTER_HARDSHIP_PROFILE",
    "MERIDIAN_B2_CURRENT_SOP_PLAN",
    "MERIDIAN_B2_CURRENT_SOP_PROFILE",
    "MERIDIAN_B3_FABRICATION_REFUSAL_PLAN",
    "MERIDIAN_B3_FABRICATION_REFUSAL_PROFILE",
    "MERIDIAN_S9_OVERDUE_CREDIT_PLAN",
    "MERIDIAN_S9_OVERDUE_CREDIT_PROFILE",
    "ExecutionStrategy",
    "MeridianB1WorkflowInput",
    "MeridianB2WorkflowInput",
    "MeridianB3WorkflowInput",
    "MeridianS9WorkflowInput",
    "WorkflowExecutionProfile",
    "WorkflowPlanStep",
]

_PROFILE_HASH_DOMAIN = b"neutron:workflow-execution-profile:v1\0"
_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class ExecutionStrategy(StrEnum):
    """The closed PRE-010D strategy vocabulary; executable Hybrid is post-demo."""

    DETERMINISTIC = "deterministic"
    AGENTIC = "agentic"

    @classmethod
    def parse(cls, value: str | ExecutionStrategy) -> ExecutionStrategy:
        """Parse a server-authored strategy and fail clearly on unavailable Hybrid."""
        if isinstance(value, cls):
            return value
        if value == "hybrid":
            raise ValueError(
                "hybrid execution is unavailable until the typed AgenticStep/child-Run "
                "contract exists"
            )
        try:
            return cls(value)
        except ValueError as exc:
            raise ValueError(f"unsupported execution strategy {value!r}") from exc


class WorkflowPlanStep(BaseModel):
    """One displayable step in the immutable server-authored execution sequence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ordinal: int = Field(ge=1)
    step_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    kind: Literal["governed-tool", "deterministic-analysis", "model-judgment"]
    action: str = Field(min_length=1, max_length=255)
    model_role: str | None = Field(default=None, min_length=1, max_length=255)

    def model_post_init(self, __context: Any) -> None:
        if self.kind == "model-judgment" and self.model_role is None:
            raise ValueError("a model-judgment step must name its bounded model role")
        if self.kind != "model-judgment" and self.model_role is not None:
            raise ValueError("only a model-judgment step may declare a model role")


class MeridianB1WorkflowInput(BaseModel):
    """The sole caller-supplied input of the Meridian B1 deterministic Workflow."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_subjects: int = Field(ge=1, le=5)


class MeridianB2WorkflowInput(BaseModel):
    """The sole caller-supplied input of the Meridian B2 deterministic Workflow."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_subjects: int = Field(ge=1, le=5)


class MeridianB3WorkflowInput(BaseModel):
    """The sole caller-supplied input of the Meridian B3 deterministic Workflow."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_subjects: int = Field(ge=1, le=5)


class MeridianS9WorkflowInput(BaseModel):
    """The sole caller-supplied input of the Meridian S9 deterministic Workflow."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_subjects: int = Field(ge=1, le=5)


InputModelT = TypeVar("InputModelT", bound=BaseModel)


def _canonical_payload(
    *,
    workflow_id: str,
    version: int,
    input_type: type[BaseModel],
    fixed_inputs: tuple[tuple[str, Any], ...],
    fixed_plan: tuple[WorkflowPlanStep, ...],
) -> dict[str, Any]:
    return {
        "profile_schema_version": 1,
        "workflow_id": workflow_id,
        "version": version,
        "strategy": ExecutionStrategy.DETERMINISTIC.value,
        "input_schema": input_type.model_json_schema(),
        "fixed_inputs": {name: value for name, value in fixed_inputs},
        "fixed_plan": [step.model_dump(mode="json") for step in fixed_plan],
    }


def _hash_payload(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(_PROFILE_HASH_DOMAIN + canonical).hexdigest()


def _is_immutable_json(value: Any) -> bool:
    if value is None or isinstance(value, str | bool | int):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, tuple):
        return all(_is_immutable_json(item) for item in value)
    return False


@dataclass(frozen=True, slots=True)
class WorkflowExecutionProfile:
    """A boot-validated, versioned deterministic Workflow execution contract."""

    workflow_id: str
    version: int
    config_hash: str
    input_type: type[BaseModel]
    fixed_inputs: tuple[tuple[str, Any], ...]
    fixed_plan: tuple[WorkflowPlanStep, ...]

    def __post_init__(self) -> None:
        if not _SAFE_ID.fullmatch(self.workflow_id):
            raise ValueError("workflow id has an invalid shape")
        if isinstance(self.version, bool) or self.version < 1:
            raise ValueError("workflow profile version must be positive")
        if not _HEX64.fullmatch(self.config_hash):
            raise ValueError("workflow profile config hash must be lowercase sha256")
        if not isinstance(self.input_type, type) or not issubclass(self.input_type, BaseModel):
            raise TypeError("workflow input_type must be a pydantic BaseModel class")
        if (
            self.input_type.model_config.get("extra") != "forbid"
            or self.input_type.model_config.get("frozen") is not True
        ):
            raise ValueError("workflow typed input must be frozen and reject undeclared fields")
        names = [name for name, _value in self.fixed_inputs]
        if any(not _SAFE_ID.fullmatch(name) for name in names):
            raise ValueError("fixed input names must use the safe id shape")
        if names != sorted(names) or len(names) != len(set(names)):
            raise ValueError("fixed inputs must be unique and sorted by name")
        if any(not _is_immutable_json(value) for _name, value in self.fixed_inputs):
            raise ValueError("fixed input values must be immutable canonical JSON values")
        declared_inputs = set(self.input_type.model_fields)
        overlap = declared_inputs.intersection(names)
        if overlap:
            raise ValueError(
                "caller inputs and frozen inputs overlap: " + ", ".join(sorted(overlap))
            )
        if not self.fixed_plan:
            raise ValueError("deterministic workflow plan must not be empty")
        ordinals = tuple(step.ordinal for step in self.fixed_plan)
        if ordinals != tuple(range(1, len(self.fixed_plan) + 1)):
            raise ValueError("fixed plan ordinals must be contiguous and ordered from one")
        step_ids = tuple(step.step_id for step in self.fixed_plan)
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("fixed plan step ids must be unique")
        actual = self.canonical_config_hash()
        if not hmac.compare_digest(actual, self.config_hash):
            raise ValueError(
                "workflow profile config hash mismatch; change the version and hash "
                "whenever typed inputs, frozen inputs or plan bytes change"
            )

    @classmethod
    def define(
        cls,
        *,
        workflow_id: str,
        version: int,
        input_type: type[InputModelT],
        fixed_inputs: dict[str, Any],
        fixed_plan: tuple[WorkflowPlanStep, ...],
    ) -> WorkflowExecutionProfile:
        """Create a new profile and derive its canonical config hash exactly once."""
        frozen_inputs = tuple(sorted(fixed_inputs.items()))
        payload = _canonical_payload(
            workflow_id=workflow_id,
            version=version,
            input_type=input_type,
            fixed_inputs=frozen_inputs,
            fixed_plan=fixed_plan,
        )
        return cls(
            workflow_id=workflow_id,
            version=version,
            config_hash=_hash_payload(payload),
            input_type=input_type,
            fixed_inputs=frozen_inputs,
            fixed_plan=fixed_plan,
        )

    @property
    def strategy(self) -> ExecutionStrategy:
        return ExecutionStrategy.DETERMINISTIC

    @property
    def input_schema(self) -> MappingProxyType[str, Any]:
        """Return an immutable top-level copy of the exact hashed JSON schema."""
        return MappingProxyType(self.input_type.model_json_schema())

    def canonical_payload(self) -> dict[str, Any]:
        """Return the semantic payload covered by ``config_hash``."""
        return _canonical_payload(
            workflow_id=self.workflow_id,
            version=self.version,
            input_type=self.input_type,
            fixed_inputs=self.fixed_inputs,
            fixed_plan=self.fixed_plan,
        )

    def canonical_config_hash(self) -> str:
        return _hash_payload(self.canonical_payload())

    def validate_input(self, value: Any) -> BaseModel:
        """Strictly validate one submission against the profile's typed schema."""
        return self.input_type.model_validate(value, strict=True)


MERIDIAN_B1_DISASTER_HARDSHIP_PLAN = (
    WorkflowPlanStep(
        ordinal=1,
        step_id="enumerate-affected-customers",
        kind="governed-tool",
        action="read.kg.customers_in_counties",
    ),
    WorkflowPlanStep(
        ordinal=2,
        step_id="enumerate-customer-auto-loans",
        kind="governed-tool",
        action="read.kg.autoloans_for_customer",
    ),
    WorkflowPlanStep(
        ordinal=3,
        step_id="read-scheduled-payment-window",
        kind="governed-tool",
        action="read.kg.payments_due_window",
    ),
    WorkflowPlanStep(
        ordinal=4,
        step_id="read-loan-servicing-notes",
        kind="governed-tool",
        action="read.kg.servicing_notes_for_loan",
    ),
    WorkflowPlanStep(
        ordinal=5,
        step_id="select-actionable-hardship-population",
        kind="deterministic-analysis",
        action="affected-active-due-no-prior-offer-no-repossession",
    ),
)

MERIDIAN_B2_CURRENT_SOP_PLAN = (
    WorkflowPlanStep(
        ordinal=1,
        step_id="read-complete-dispute-directory",
        kind="governed-tool",
        action="read.kg.disputes_directory",
    ),
    WorkflowPlanStep(
        ordinal=2,
        step_id="read-open-disputes-with-current-sop",
        kind="governed-tool",
        action="read.kg.open_disputes_by_status",
    ),
    WorkflowPlanStep(
        ordinal=3,
        step_id="apply-current-sop",
        kind="deterministic-analysis",
        action="apply-one-current-sop-to-complete-open-dispute-set",
    ),
)

MERIDIAN_B3_FABRICATION_REFUSAL_PLAN = (
    WorkflowPlanStep(
        ordinal=1,
        step_id="read-complete-dispute-directory",
        kind="governed-tool",
        action="read.kg.disputes_directory",
    ),
    WorkflowPlanStep(
        ordinal=2,
        step_id="refuse-unsupported-total",
        kind="deterministic-analysis",
        action="refuse-late-credit-total-and-compute-recorded-credit-control",
    ),
)


# S9 — the judgement question ("which open debit-card disputes are overdue for
# provisional credit under the SOP?").  The failure this plan exists to beat is
# not a missing fact but a judgement call: converting "N business days" into a
# date comparison at answer time.  Here NO date arithmetic happens at run time at
# all — the provisional-credit deadline is a MATERIALISED served cell (computed
# at load time by trusted code, carrying the tier and policy it was derived
# under), and the one governed read below is a served-cell comparison the
# analysis step merely re-verifies.  Zero caller inputs reach the query.
MERIDIAN_S9_OVERDUE_CREDIT_PLAN = (
    WorkflowPlanStep(
        ordinal=1,
        step_id="read-overdue-provisional-credit-disputes",
        kind="governed-tool",
        action="read.kg.overdue_provisional_credit_disputes",
    ),
    WorkflowPlanStep(
        ordinal=2,
        step_id="verify-served-deadline-comparison",
        kind="deterministic-analysis",
        action="recheck-open-no-credit-and-as-of-past-materialised-deadline",
    ),
)


MERIDIAN_B1_DISASTER_HARDSHIP_PROFILE = WorkflowExecutionProfile(
    workflow_id="meridian-b1-disaster-hardship",
    version=1,
    config_hash="e90381d94480ee1d01f028380be39595b63c4c632d6aff89738b49a440e667e0",
    input_type=MeridianB1WorkflowInput,
    fixed_inputs=(
        ("counties", ("Columbia", "Jefferson", "Richmond")),
        ("payment_window_end", "2026-09-01"),
        ("payment_window_start", "2026-08-18"),
    ),
    fixed_plan=MERIDIAN_B1_DISASTER_HARDSHIP_PLAN,
)

MERIDIAN_B2_CURRENT_SOP_PROFILE = WorkflowExecutionProfile(
    workflow_id="meridian-b2-current-sop",
    version=1,
    config_hash="7209a37006427bdc938c34f8e8c3ce798953a1aefa031bb6c81c5d32ffc97fce",
    input_type=MeridianB2WorkflowInput,
    fixed_inputs=(),
    fixed_plan=MERIDIAN_B2_CURRENT_SOP_PLAN,
)

MERIDIAN_B3_FABRICATION_REFUSAL_PROFILE = WorkflowExecutionProfile(
    workflow_id="meridian-b3-fabrication-refusal",
    version=1,
    config_hash="5a70614b9270673416f4f5c521b4d3541a22ff131813d00bf925ddbdc2f6e23a",
    input_type=MeridianB3WorkflowInput,
    fixed_inputs=(),
    fixed_plan=MERIDIAN_B3_FABRICATION_REFUSAL_PLAN,
)

MERIDIAN_S9_OVERDUE_CREDIT_PROFILE = WorkflowExecutionProfile(
    workflow_id="meridian-s9-overdue-provisional-credit",
    version=1,
    config_hash="f36c68400c64dbbd49f02f38e52cf3face440f9607485de126de5031a60f4708",
    input_type=MeridianS9WorkflowInput,
    fixed_inputs=(),
    fixed_plan=MERIDIAN_S9_OVERDUE_CREDIT_PLAN,
)
