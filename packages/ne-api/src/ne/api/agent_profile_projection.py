# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Principal-visible projection of deterministic Agent execution profiles.

The profile is execution authority, not presentation metadata.  This projection
therefore copies the exact hash-covered input schema, frozen inputs and ordered
plan from the boot-composed :class:`WorkflowExecutionProfile`.  It never derives
an input or a step from catalog prose (and the client never has to infer either).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from ne.api.agent_catalog import AgentCatalog
from ne.api.agent_manifest import WorkflowProfileBinding
from ne.contracts.types import Principal
from ne.runtime.workflow_execution_profile import WorkflowExecutionProfile

__all__ = [
    "AgentProfileProjectionState",
    "AgentWorkflowProfileView",
    "AgentWorkflowStepView",
    "project_visible_agent_profile",
]


class _WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AgentWorkflowStepView(_WireModel):
    """One server-authored step in the immutable deterministic plan."""

    ordinal: int
    step_id: str
    kind: Literal["governed-tool", "deterministic-analysis", "model-judgment"]
    action: str
    model_role: str | None


class AgentWorkflowProfileView(_WireModel):
    """The exact executable profile bound to one visible Agent version."""

    agent_id: str
    agent_version: int
    agent_config_hash: str
    strategy: Literal["deterministic"]
    workflow_id: str
    workflow_version: int
    workflow_config_hash: str
    input_schema: dict[str, Any]
    fixed_inputs: dict[str, Any]
    fixed_plan: tuple[AgentWorkflowStepView, ...]


@dataclass(frozen=True, slots=True)
class AgentProfileProjectionState:
    """Typed, request-local view of the two boot-composed authorities."""

    agent_catalog: AgentCatalog
    workflow_execution_profiles: Mapping[str, WorkflowExecutionProfile]


def _declared_fixed_inputs(declared: WorkflowProfileBinding) -> dict[str, Any]:
    return declared.frozen_inputs.canonical_values()


def project_visible_agent_profile(
    state: AgentProfileProjectionState,
    *,
    agent_id: str,
    principal: Principal,
) -> AgentWorkflowProfileView | None:
    """Project one exact profile after visibility and binding checks.

    Every failure is deliberately one ``None``.  The HTTP layer maps that to a
    uniform 404 so a hidden Agent, an unknown id, an agentic Agent and an invalid
    deterministic binding cannot be distinguished by an unauthorized caller.
    """
    row = state.agent_catalog.get_visible(agent_id, principal)
    if row is None:
        return None
    declared = row.definition.workflow
    if row.definition.default_strategy != "deterministic" or declared is None:
        return None
    profile = state.workflow_execution_profiles.get(declared.profile_id)
    if profile is None:
        return None
    if (profile.version, profile.config_hash) != (declared.version, declared.config_hash):
        return None

    payload = profile.canonical_payload()
    if (
        payload.get("workflow_id") != declared.profile_id
        or payload.get("version") != declared.version
        or payload.get("strategy") != "deterministic"
    ):
        return None
    fixed_inputs = payload["fixed_inputs"]
    input_schema = payload["input_schema"]
    fixed_plan = payload["fixed_plan"]
    if not isinstance(fixed_inputs, dict) or fixed_inputs != _declared_fixed_inputs(declared):
        return None
    if not isinstance(input_schema, dict) or not isinstance(fixed_plan, list):
        return None

    return AgentWorkflowProfileView(
        agent_id=row.definition.agent_id,
        agent_version=row.definition.version,
        agent_config_hash=row.definition.definition_hash,
        strategy="deterministic",
        workflow_id=payload["workflow_id"],
        workflow_version=payload["version"],
        workflow_config_hash=profile.config_hash,
        input_schema=input_schema,
        fixed_inputs=fixed_inputs,
        fixed_plan=tuple(AgentWorkflowStepView.model_validate(step) for step in fixed_plan),
    )
