# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Principal-scoped read of one Agent's currently-eligible capability surface.

This is the sibling of :mod:`ne.api.agent_model_preflight` and holds the same
posture: no HTTP imports, no composition-root imports, every producer injected.
It consumes the completed PRE-011A inventory
(:func:`ne.runtime.agent_capabilities.project_agent_capabilities`, reached only
through the injected provider) and adds the two things the API layer owes the
console and the later submit path -- a safe projection with a stable order, and
the READ half of the submit-time capability-hash join.

WHAT IT IS NOT.

* It is not an existence oracle.  ``AgentCatalog.get_visible`` already answers
  hidden and unknown identically, and that one ``None`` is returned before the
  inventory provider is consulted, so neither cost nor side effect separates
  "not yours" from "no such Agent".
* It is not a capability authority.  Eligibility is decided entirely by 011A;
  this module intersects nothing and widens nothing.  It re-projects what it is
  handed and fingerprints it.
* It authors and projects NO evidence.  No ``EvidenceRecord``, no locator, no
  audit append.
* It activates nothing.  No Run is started, authorized or reserved here, and no
  submit-time gate is satisfied by having called it.

THE CAPABILITY HASH.  :attr:`AgentCapabilitySet.capability_hash` is a sha256
over the exact Agent identity plus every projected field of every projected row,
IN THE PROJECTED ORDER.  Two consequences are deliberate:

* it is SURFACE-scoped, and deliberately not principal-bound.  No principal
  identity enters the digest.  Two principals who see different capabilities on
  the same Agent definition therefore get different hashes, but two who see the
  IDENTICAL surface legitimately share one -- and that is safe, because the join
  re-derives the CURRENT principal's surface on every read and compares against
  that.  A shared digest asserts only "the surface you were shown is still the
  surface you get", which is exactly what the join is for; it conveys no
  authority and carries nothing across principals that they were not already
  entitled to see;
* it covers live health, not just static shape.  A tool flapping to
  ``unhealthy`` moves the hash and therefore refuses a join carrying the older
  one.  This is a fail-closed read gate over "the surface you were shown", not a
  cache key over "the surface's schema", and a caller that would have been happy
  to proceed simply re-reads.

THE REFUSAL SEAM.  When ``presented_capability_hash`` is supplied,
:meth:`AgentCapabilityPreflightService.preflight` RECOMPUTES the hash from the
inventory read in THIS call and returns ``None`` unless the two are equal.  The
stale-hash refusal is the same ``None`` as hidden, as unknown, and as an
identity the provider could not answer for, so the route renders one 404 for all
of them and a caller learns nothing from which refusal it hit.

MISSING FACTS ARE UNAVAILABLE, NEVER INVENTED.  ``capabilities is None`` means
"the producer could not answer"; ``capabilities`` present with empty tuples
means "answered: this principal is eligible for nothing".  The two are distinct
values because collapsing them would report a producer outage as an
authoritative empty grant.  A provider that RAISES is not answering either, but
it is also not a contract state, so the exception propagates rather than being
laundered into the "cannot answer" value.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Protocol

from ne.api.agent_catalog import AgentCatalog
from ne.contracts.types import Principal
from ne.runtime.agent_capabilities import (
    AgentCapabilityInventory,
    CapabilityHealth,
    KnowledgeTier,
    ReadWriteClass,
)

__all__ = [
    "AgentCapabilityInventoryProvider",
    "AgentCapabilityPreflight",
    "AgentCapabilityPreflightService",
    "AgentCapabilitySet",
    "AgentConnectorCapability",
    "AgentSkillDetail",
    "AgentSkillDetailProvider",
    "AgentToolArgument",
    "AgentToolCapability",
]


@dataclass(frozen=True, slots=True)
class AgentToolArgument:
    """One advertised Tool argument — sanitized schema metadata, never a value.

    Carried verbatim from 011A's :class:`ToolArgumentCapability` (see its C12
    rationale: this is the contract the model already sees in the in-band
    catalog). ``None`` anywhere = the schema does not state it.
    """

    name: str
    type_label: str | None
    required: bool
    description: str | None
    enum: tuple[str, ...] | None
    pattern: str | None
    max_length: int | None
    list_max: int | None


@dataclass(frozen=True, slots=True)
class AgentToolCapability:
    """One eligible Tool: safe metadata plus its SANITIZED advertised contract.

    ``description``/``arguments``/``answer_columns`` (NE-CAPTAB) are the tool's
    model-facing contract, projected by 011A from the registered spec — never
    the raw schema mapping, a handler, or a PDP reason. ``arguments is None``
    means "not stated", which is a different fact from an empty tuple's "takes
    no arguments".
    """

    name: str
    label: str
    read_write_class: ReadWriteClass
    declared_tier: int
    effective_tier: int
    connection_identity_label: str
    health: CapabilityHealth
    description: str = ""
    arguments: tuple[AgentToolArgument, ...] | None = None
    answer_columns: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class AgentConnectorCapability:
    """One eligible Connector identity plus the tiers its grant governs."""

    connection_identity_label: str
    label: str
    kind: str
    credential_owner_label: str
    health: CapabilityHealth
    source_id: str
    tiers: tuple[KnowledgeTier, ...]


@dataclass(frozen=True, slots=True)
class AgentCapabilitySet:
    """The answered capability surface, bound to one exact Agent definition."""

    agent_version: int
    agent_definition_hash: str
    tools: tuple[AgentToolCapability, ...]
    connectors: tuple[AgentConnectorCapability, ...]
    capability_hash: str


@dataclass(frozen=True, slots=True)
class AgentSkillDetail:
    """One pinned Skill this principal may see, with its authored procedure.

    NE-CAPTAB-SKILL: the fields are the Skill pack's own governance metadata plus
    the authored ``body`` (the procedure the agent is bound by). Every value is
    the Skill author's text or a manifest pin — never customer data. The pack is
    resolved through the SAME governed selection the runtime activates
    (``AgentSkillService.select``: exact pin → ACL → required-tools → platform),
    so a Skill the principal could not activate never appears here, and the
    ``content_hash`` shown is the one the Run would freeze against.
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


@dataclass(frozen=True, slots=True)
class AgentCapabilityPreflight:
    """Declared Agent facts beside the capability surface one principal may see.

    ``skills`` (NE-CAPTAB-SKILL) is an ADDITIVE, principal-scoped projection of
    the Agent's pinned Skills — deliberately NOT part of ``capabilities`` and
    NOT folded into the capability hash, because it is display detail, not an
    eligibility surface a Run is admitted against. ``None`` means no Skill
    detail producer was composed (the honest "cannot answer"); an empty tuple
    means the producer answered "no Skill visible to you".
    """

    agent_id: str
    agent_version: int
    definition_hash: str
    available: bool
    availability_reasons: tuple[str, ...]
    capabilities: AgentCapabilitySet | None
    skills: tuple[AgentSkillDetail, ...] | None = None


class AgentCapabilityInventoryProvider(Protocol):
    """The composition-bound 011A inventory read, narrowed to what is needed.

    ``None`` is the ONE way to say "this deployment cannot answer for that exact
    definition".  Returning an inventory bound to a different identity is a
    producer defect and is treated as not-answered, never re-labelled onto the
    requested Agent.
    """

    async def __call__(
        self,
        *,
        agent_id: str,
        agent_version: int,
        agent_definition_hash: str,
        principal: Principal,
    ) -> AgentCapabilityInventory | None: ...


class AgentSkillDetailProvider(Protocol):
    """The composition-bound principal-scoped pinned-Skill detail read.

    ``None`` = no producer is composed (this deployment cannot answer for
    Skills); an empty tuple = the producer answered and nothing is visible to
    this principal. The provider owns the ACL/pin/tool/platform filtering — this
    module never re-decides Skill visibility.
    """

    async def __call__(
        self,
        *,
        agent_id: str,
        principal: Principal,
    ) -> tuple[AgentSkillDetail, ...] | None: ...


def _capability_hash(
    *,
    agent_id: str,
    agent_version: int,
    agent_definition_hash: str,
    tools: tuple[AgentToolCapability, ...],
    connectors: tuple[AgentConnectorCapability, ...],
) -> str:
    """sha256 over the exact identity and the ORDERED projected rows.

    Rows are encoded as positional lists, so re-ordering the projection moves the
    digest.  ``sort_keys`` only canonicalizes the fixed outer key set; it cannot
    reach inside a row.
    """
    payload = {
        "agent_id": agent_id,
        "agent_version": agent_version,
        "agent_definition_hash": agent_definition_hash,
        "tools": [
            [
                tool.name,
                tool.label,
                tool.read_write_class,
                tool.declared_tier,
                tool.effective_tier,
                tool.connection_identity_label,
                tool.health,
            ]
            for tool in tools
        ],
        "connectors": [
            [
                connector.connection_identity_label,
                connector.label,
                connector.kind,
                connector.credential_owner_label,
                connector.health,
                connector.source_id,
                list(connector.tiers),
            ]
            for connector in connectors
        ],
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


class AgentCapabilityPreflightService:
    """Projects one Agent definition against its live eligible capabilities."""

    def __init__(
        self,
        *,
        agent_catalog: AgentCatalog,
        inventory_provider: AgentCapabilityInventoryProvider,
        skill_detail_provider: AgentSkillDetailProvider | None = None,
    ) -> None:
        self._agent_catalog = agent_catalog
        self._inventory_provider = inventory_provider
        # Optional and additive: a deployment with no Skill substrate composed
        # leaves this None, and the projection's ``skills`` field stays None.
        self._skill_detail_provider = skill_detail_provider

    async def preflight(
        self,
        *,
        agent_id: str,
        principal: Principal,
        presented_capability_hash: str | None = None,
    ) -> AgentCapabilityPreflight | None:
        """Return the projection, or ``None`` for every refusal alike.

        ``None`` covers hidden, unknown, and a presented hash that no longer
        matches the surface read here.  A caller cannot tell them apart.
        """
        agent = self._agent_catalog.get_visible(agent_id, principal)
        if agent is None:
            # Deliberately the FIRST statement with an exit: the inventory
            # provider is not consulted, so hidden and unknown cost the same and
            # touch the same nothing.
            return None

        definition = agent.definition
        inventory = await self._inventory_provider(
            agent_id=definition.agent_id,
            agent_version=definition.version,
            agent_definition_hash=definition.definition_hash,
            principal=principal,
        )
        capabilities = self._project(
            agent_id=definition.agent_id,
            agent_version=definition.version,
            definition_hash=definition.definition_hash,
            row=inventory,
        )

        if presented_capability_hash is not None and (
            capabilities is None or capabilities.capability_hash != presented_capability_hash
        ):
            # The hash is recomputed from THIS read, never trusted from the
            # caller and never carried forward from an earlier one.  An
            # unanswerable surface cannot satisfy a join either.
            return None

        # Additive Skill detail, resolved AFTER the capability-hash join so it can
        # never perturb that join. A provider failure is not laundered into an
        # empty answer: absent producer stays None here.
        skills = (
            None
            if self._skill_detail_provider is None
            else await self._skill_detail_provider(
                agent_id=definition.agent_id,
                principal=principal,
            )
        )

        return AgentCapabilityPreflight(
            agent_id=definition.agent_id,
            agent_version=definition.version,
            definition_hash=definition.definition_hash,
            # Copied verbatim, exactly as PRE-011D copies them: this report
            # states the catalog's dependency verdict, it does not re-decide it.
            available=agent.available,
            availability_reasons=agent.availability_reasons,
            capabilities=capabilities,
            skills=skills,
        )

    def _project(
        self,
        *,
        agent_id: str,
        agent_version: int,
        definition_hash: str,
        row: AgentCapabilityInventory | None,
    ) -> AgentCapabilitySet | None:
        """Re-project 011A's rows, preserving their order exactly."""
        if row is None:
            return None
        if (row.agent_id, row.agent_version, row.agent_definition_hash) != (
            agent_id,
            agent_version,
            definition_hash,
        ):
            # The WHOLE identity triple, not the hash alone.  An inventory
            # answering for another Agent -- or another version of this one --
            # is not an answer about this one, and relabelling it would publish
            # a foreign grant set under this Agent's identity and then mint a
            # capability hash over it.  Checking only the definition hash left
            # ``agent_id`` and ``version`` free: a provider could return a
            # foreign id at version 99 carrying the requested hash and be
            # accepted.  All three are compared as one tuple so no single field
            # can be dropped without a control noticing.
            return None
        # ``tuple(...)`` over the producer's sequence: the 011A order is carried
        # through unchanged.  Re-sorting here would discard an ordering the
        # producer already owns and make the hash disagree with the surface that
        # produced it.
        tools = tuple(
            AgentToolCapability(
                name=tool.name,
                label=tool.label,
                read_write_class=tool.read_write_class,
                # Plain ``int``, not the ``ApprovalTier`` member: a consumer
                # comparing a tier must not have to import the enum.
                declared_tier=int(tool.declared_tier),
                effective_tier=int(tool.effective_tier),
                connection_identity_label=tool.connection_identity_label,
                health=tool.health,
                description=tool.description,
                # NE-CAPTAB: contract metadata rides along for display. It is
                # DELIBERATELY absent from `_capability_hash`: the hash answers
                # "did this principal's eligibility move?", and the argument
                # contract is derived from the same immutable definition the
                # hash already pins via agent_definition_hash.
                arguments=None
                if tool.arguments is None
                else tuple(
                    AgentToolArgument(
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
            for tool in row.tools
        )
        connectors = tuple(
            AgentConnectorCapability(
                connection_identity_label=connector.connection_identity_label,
                label=connector.label,
                kind=connector.kind,
                credential_owner_label=connector.credential_owner_label,
                health=connector.health,
                source_id=connector.governing_grant.source_id,
                tiers=connector.governing_grant.tiers,
            )
            for connector in row.connectors
        )
        return AgentCapabilitySet(
            agent_version=row.agent_version,
            agent_definition_hash=row.agent_definition_hash,
            tools=tools,
            connectors=connectors,
            capability_hash=_capability_hash(
                agent_id=row.agent_id,
                agent_version=row.agent_version,
                agent_definition_hash=row.agent_definition_hash,
                tools=tools,
                connectors=connectors,
            ),
        )
