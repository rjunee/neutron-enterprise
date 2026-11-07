# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Safe, metadata-only HTTP projection of declarative Agent catalog rows."""

from __future__ import annotations

from datetime import datetime
from typing import cast

from pydantic import BaseModel, ConfigDict

from ne.api.agent_catalog import CatalogAgent
from ne.api.agent_execution_caps import enforced_agentic_caps
from ne.api.agent_manifest import (
    AgentBudgets,
    AgentManifestDefinition,
    ExecutionStrategy,
    KnowledgeGrant,
    Lifecycle,
    SkillGrant,
    WorkflowProfileBinding,
)
from ne.api.agent_model_preflight import AgentModelPreflight
from ne.api.agent_preflight import AgentCapabilityPreflight
from ne.runtime.agent_capabilities import CapabilityHealth, KnowledgeTier, ReadWriteClass


class _WireModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class AgentEnforcedCapsItem(_WireModel):
    """The ceilings this Agent's Runs are ACTUALLY measured against, per attempt.

    Served so the console can print an enforced number instead of re-deriving one
    (see :mod:`ne.api.agent_execution_caps`). ``budgets`` above stays exactly as
    declared — it is the manifest's own record and the operator's input — and
    this is what the runtime makes of it. A deterministic Agent has no bounded
    agentic loop, so it carries no enforced caps at all rather than a set of
    zeroes or a copy of numbers nothing measures.
    """

    max_tool_calls: int
    max_model_requests: int
    wall_clock_seconds: float
    max_total_tokens: int
    max_parked_writes: int


class AgentCatalogItem(_WireModel):
    """Public catalog metadata; visibility rules and manifest bytes never leave the server."""

    agent_id: str
    version: int
    label: str
    description: str
    owner_label: str
    lifecycle: Lifecycle
    allowed_strategies: tuple[ExecutionStrategy, ...]
    default_strategy: ExecutionStrategy
    model_policy_id: str
    tool_grants: tuple[str, ...]
    knowledge_sources: tuple[KnowledgeGrant, ...]
    skills: tuple[SkillGrant, ...]
    budgets: AgentBudgets
    enforced_caps: AgentEnforcedCapsItem | None
    workflow: WorkflowProfileBinding | None
    available: bool
    availability_reasons: tuple[str, ...]
    config_hash: str


class AgentCatalogPage(_WireModel):
    items: tuple[AgentCatalogItem, ...]
    next_cursor: str | None


def _enforced_caps_item(definition: AgentManifestDefinition) -> AgentEnforcedCapsItem | None:
    """The enforced ceilings, or None where no bounded agentic loop runs.

    The condition is the SAME one ``ne.api.platform`` binds a BoundedAgenticAdapter
    on — a declared workflow means the deterministic profile executes and the
    agentic budgets are never consulted. Reading it any other way here would put
    a cap on the screen for a Run that never enforces one.
    """
    if definition.workflow is not None or definition.default_strategy != "agentic":
        return None
    caps = enforced_agentic_caps(definition.budgets, definition.tool_grants)
    return AgentEnforcedCapsItem(
        max_tool_calls=caps.max_tool_calls,
        max_model_requests=caps.max_model_requests,
        wall_clock_seconds=caps.wall_clock_seconds,
        max_total_tokens=caps.max_total_tokens,
        max_parked_writes=caps.max_parked_writes,
    )


def agent_catalog_item(row: CatalogAgent) -> AgentCatalogItem:
    definition = row.definition
    return AgentCatalogItem(
        agent_id=definition.agent_id,
        version=definition.version,
        label=definition.label,
        description=definition.description,
        owner_label=definition.owner_label,
        lifecycle=definition.lifecycle,
        allowed_strategies=definition.allowed_strategies,
        default_strategy=definition.default_strategy,
        model_policy_id=definition.model_policy_id,
        tool_grants=definition.tool_grants,
        knowledge_sources=definition.knowledge_sources,
        skills=definition.skills,
        budgets=definition.budgets,
        enforced_caps=_enforced_caps_item(definition),
        workflow=definition.workflow,
        available=row.available,
        availability_reasons=row.availability_reasons,
        config_hash=definition.definition_hash,
    )


class AgentToolArgumentItem(_WireModel):
    """One advertised Tool argument — sanitized schema metadata, never a value.

    Field-for-field from :class:`ne.api.agent_preflight.AgentToolArgument`; the
    identical contract the in-band tool catalog already ships to the model.
    ``null`` anywhere means the schema does not state it.
    """

    name: str
    type_label: str | None
    required: bool
    description: str | None
    enum: tuple[str, ...] | None
    pattern: str | None
    max_length: int | None
    list_max: int | None


class AgentToolCapabilityItem(_WireModel):
    """Safe Tool preflight metadata plus the tool's sanitized advertised contract.

    NE-CAPTAB: ``description``/``arguments``/``answer_columns`` are the
    model-facing contract projected by 011A — never the raw schema mapping, a
    handler or a PDP reason. ``arguments: null`` = "not stated"; ``[]`` = "takes
    no arguments" — a consumer must not collapse the two.
    """

    name: str
    label: str
    read_write_class: ReadWriteClass
    declared_tier: int
    effective_tier: int
    connection_identity_label: str
    health: CapabilityHealth
    description: str
    arguments: tuple[AgentToolArgumentItem, ...] | None
    answer_columns: tuple[str, ...] | None


class AgentConnectorCapabilityItem(_WireModel):
    """Safe Connector identity metadata; never a credential or a customer id."""

    connection_identity_label: str
    label: str
    kind: str
    credential_owner_label: str
    health: CapabilityHealth
    source_id: str
    tiers: tuple[KnowledgeTier, ...]


class AgentCapabilitySetItem(_WireModel):
    """An ANSWERED capability surface; empty tuples mean "eligible for nothing"."""

    agent_version: int
    agent_definition_hash: str
    tools: tuple[AgentToolCapabilityItem, ...]
    connectors: tuple[AgentConnectorCapabilityItem, ...]
    capability_hash: str


class AgentSkillDetailItem(_WireModel):
    """One pinned Skill this principal may see, with its authored procedure.

    NE-CAPTAB-SKILL: Skill-author text and manifest pins only — never customer
    data or a credential. ``body`` is the authored procedure Markdown.
    """

    skill_id: str
    version: int
    content_hash: str
    description: str
    requires_tools: tuple[str, ...]
    acl_labels: tuple[str, ...]
    triggers: tuple[str, ...]
    category: str
    body: str


class AgentCapabilityPreflightItem(_WireModel):
    """Declared Agent facts beside the capabilities this principal may see.

    ``capabilities is None`` is the wire's "the producer could not answer".  It
    is a DIFFERENT value from a present set with empty tuples, so a consumer can
    never read an inventory outage as an authoritative empty grant.

    ``skills`` (NE-CAPTAB-SKILL) mirrors that same None-vs-empty discipline for
    the additive pinned-Skill detail, and is never part of the capability hash.
    """

    agent_id: str
    version: int
    definition_hash: str
    available: bool
    availability_reasons: tuple[str, ...]
    capabilities: AgentCapabilitySetItem | None
    skills: tuple[AgentSkillDetailItem, ...] | None


class AgentModelCandidateItem(_WireModel):
    """One currently evidenced and principal-authorized model choice."""

    model: str
    provider: str
    lane: str
    evidence: str
    observed_at: datetime


class AgentModelPreflightItem(_WireModel):
    """Strategy, budget and model truth from the existing 011D service."""

    task: str
    allowed_strategies: tuple[ExecutionStrategy, ...]
    default_strategy: ExecutionStrategy
    max_steps: int
    max_tool_calls: int
    max_input_tokens: int
    max_output_tokens: int
    candidates: tuple[AgentModelCandidateItem, ...]
    default_model: str | None


class AgentPreflightItem(AgentCapabilityPreflightItem):
    """The singular, complete capability plus model preflight response."""

    model: AgentModelPreflightItem


def agent_capability_preflight_item(
    result: AgentCapabilityPreflight,
) -> AgentCapabilityPreflightItem:
    """Project the service result field-for-field, preserving row order."""
    capabilities = result.capabilities
    return AgentCapabilityPreflightItem(
        agent_id=result.agent_id,
        version=result.agent_version,
        definition_hash=result.definition_hash,
        available=result.available,
        availability_reasons=result.availability_reasons,
        skills=None
        if result.skills is None
        else tuple(
            AgentSkillDetailItem(
                skill_id=skill.skill_id,
                version=skill.version,
                content_hash=skill.content_hash,
                description=skill.description,
                requires_tools=skill.requires_tools,
                acl_labels=skill.acl_labels,
                triggers=skill.triggers,
                category=skill.category,
                body=skill.body,
            )
            for skill in result.skills
        ),
        capabilities=None
        if capabilities is None
        else AgentCapabilitySetItem(
            agent_version=capabilities.agent_version,
            agent_definition_hash=capabilities.agent_definition_hash,
            tools=tuple(
                AgentToolCapabilityItem(
                    name=tool.name,
                    label=tool.label,
                    read_write_class=tool.read_write_class,
                    declared_tier=tool.declared_tier,
                    effective_tier=tool.effective_tier,
                    connection_identity_label=tool.connection_identity_label,
                    health=tool.health,
                    description=tool.description,
                    arguments=None
                    if tool.arguments is None
                    else tuple(
                        AgentToolArgumentItem(
                            name=argument.name,
                            type_label=argument.type_label,
                            required=argument.required,
                            description=argument.description,
                            enum=argument.enum,
                            pattern=argument.pattern,
                            max_length=argument.max_length,
                            list_max=argument.list_max,
                        )
                        for argument in tool.arguments
                    ),
                    answer_columns=tool.answer_columns,
                )
                for tool in capabilities.tools
            ),
            connectors=tuple(
                AgentConnectorCapabilityItem(
                    connection_identity_label=connector.connection_identity_label,
                    label=connector.label,
                    kind=connector.kind,
                    credential_owner_label=connector.credential_owner_label,
                    health=connector.health,
                    source_id=connector.source_id,
                    tiers=connector.tiers,
                )
                for connector in capabilities.connectors
            ),
            capability_hash=capabilities.capability_hash,
        ),
    )


def agent_preflight_item(
    capabilities: AgentCapabilityPreflight,
    model: AgentModelPreflight,
) -> AgentPreflightItem:
    """Join the two existing projections without re-deriving either one."""
    capability_item = agent_capability_preflight_item(capabilities)
    return AgentPreflightItem(
        **capability_item.model_dump(),
        model=AgentModelPreflightItem(
            task=model.task,
            allowed_strategies=cast(tuple[ExecutionStrategy, ...], model.allowed_strategies),
            default_strategy=cast(ExecutionStrategy, model.default_strategy),
            max_steps=model.max_steps,
            max_tool_calls=model.max_tool_calls,
            max_input_tokens=model.max_input_tokens,
            max_output_tokens=model.max_output_tokens,
            candidates=tuple(
                AgentModelCandidateItem(
                    model=candidate.model,
                    provider=candidate.provider,
                    lane=candidate.lane,
                    evidence=candidate.evidence,
                    observed_at=candidate.observed_at,
                )
                for candidate in model.candidates
            ),
            default_model=model.default_model,
        ),
    )
