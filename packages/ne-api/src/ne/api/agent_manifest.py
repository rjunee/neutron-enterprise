# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Strict, hash-pinned operator manifest for the PRE-008 Agent catalog.

The manifest is configuration, not an executable runtime definition.  It carries
only stable ids, declared grants and bounded server-owned descriptors.  In
particular this module neither imports nor serializes ``runtime.AgentDefinition``.
Both the canonical semantic model and the exact mounted bytes are hashed: the
former prevents representation-dependent meaning, while the latter proves the
API and worker consumed the same immutable artifact.
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from datetime import date
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_OPAQUE_AGENT_ID = re.compile(r"^agt_[a-z0-9]{24,48}$")
_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9._:/-]{1,127}$")

ExecutionStrategy = Literal["agentic", "deterministic"]
Lifecycle = Literal["active", "deprecated", "retired"]
KnowledgeTier = Literal["t1", "t2", "t3"]
DependencyKind = Literal["tool", "knowledge", "skill", "workflow-profile", "model-policy"]


class AgentManifestError(RuntimeError):
    """Configured Agent registry bytes are missing, malformed, or unsafe."""


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _sorted_unique(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted(set(values)))


class AgentVisibility(_FrozenModel):
    """Explicit deny-default visibility selectors.

    An all-false, all-empty value is valid but admits nobody.  Requiring the
    object itself prevents an omitted rule from becoming global visibility.
    """

    all_authenticated: bool
    principals: tuple[str, ...] = ()
    groups: tuple[str, ...] = ()
    roles: tuple[str, ...] = ()
    departments: tuple[str, ...] = ()

    @field_validator("principals", "groups", "roles", "departments")
    @classmethod
    def _canonical_selectors(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item or item.strip() != item for item in value):
            raise ValueError("visibility selectors must be non-empty trimmed strings")
        return _sorted_unique(value)


class AgentBudgets(_FrozenModel):
    max_steps: int = Field(ge=1, le=64)
    max_tool_calls: int = Field(ge=0, le=128)
    #: NE-WRAPPED-CALL-FUNDED raised this guard 2_000_000 -> 4_000_000, and the
    #: reason is worth stating because a raised sanity bound is otherwise
    #: indistinguishable from a removed one. ``max_input_tokens`` is CUMULATIVE
    #: across every model request of a turn, so it grows with the REQUEST
    #: CEILING, and ``runner.model_request_limit`` is now
    #: ``2 * max_tool_calls + 7`` (the ``wrapped-call`` allowance is one
    #: corrective round per rung of the ladder). At the Meridian Analyst's 34
    #: tool calls that is 75 requests, and the derivation in
    #: ``deploy/agents/registry.toml`` — re-checked by
    #: ``test_agent_execution_caps.py`` — comes to 3662637. The old 2_000_000
    #: was blown by the request COUNT alone: even at a zero-cost wrapped reply
    #: and a zero-cost correction the same derivation needs 3049833, because 40
    #: retry requests each re-pay a 34-call history. Raised to the next round
    #: number above the largest derived budget rather than removed.
    max_input_tokens: int = Field(ge=1, le=4_000_000)
    max_output_tokens: int = Field(ge=1, le=200_000)


class KnowledgeGrant(_FrozenModel):
    source_id: str
    tiers: tuple[KnowledgeTier, ...] = Field(min_length=1)

    @field_validator("source_id")
    @classmethod
    def _safe_source_id(cls, value: str) -> str:
        if not _SAFE_ID.fullmatch(value):
            raise ValueError("knowledge source id has an invalid shape")
        return value

    @field_validator("tiers")
    @classmethod
    def _canonical_tiers(cls, value: tuple[KnowledgeTier, ...]) -> tuple[KnowledgeTier, ...]:
        return tuple(sorted(set(value)))


class SkillGrant(_FrozenModel):
    skill_id: str
    version: int = Field(ge=1)
    content_hash: str

    @field_validator("skill_id")
    @classmethod
    def _safe_skill_id(cls, value: str) -> str:
        if not _SAFE_ID.fullmatch(value):
            raise ValueError("Skill id has an invalid shape")
        return value

    @field_validator("content_hash")
    @classmethod
    def _hash_shape(cls, value: str) -> str:
        if not _HEX64.fullmatch(value):
            raise ValueError("Skill content hash must be lowercase sha256")
        return value


class AvailabilityDependency(_FrozenModel):
    kind: DependencyKind
    dependency_id: str
    version: int | None = Field(default=None, ge=1)
    content_hash: str | None = None

    @field_validator("dependency_id")
    @classmethod
    def _safe_dependency_id(cls, value: str) -> str:
        if not _SAFE_ID.fullmatch(value):
            raise ValueError("dependency id has an invalid shape")
        return value

    @field_validator("content_hash")
    @classmethod
    def _optional_hash_shape(cls, value: str | None) -> str | None:
        if value is not None and not _HEX64.fullmatch(value):
            raise ValueError("dependency content hash must be lowercase sha256")
        return value

    @model_validator(mode="after")
    def _kind_specific_pin_shape(self) -> Self:
        pinned = self.kind in ("skill", "workflow-profile")
        has_pin = self.version is not None and self.content_hash is not None
        if pinned and not has_pin:
            raise ValueError("Skill and workflow dependencies require version and content hash")
        if not pinned and (self.version is not None or self.content_hash is not None):
            raise ValueError("tool, knowledge, and model-policy dependencies forbid pins")
        return self


class WorkflowFrozenInputs(_FrozenModel):
    """Closed server-owned inputs for the current deterministic profile family.

    B1 freezes its disaster counties and payment window. B2/B3 intentionally have
    no server-owned inputs, represented by the explicit empty TOML table rather
    than invented B1 values that their engines never consume.
    """

    counties: tuple[str, ...] | None = Field(default=None, min_length=1, max_length=32)
    payment_window_start: date | None = None
    payment_window_end: date | None = None

    @field_validator("counties")
    @classmethod
    def _canonical_counties(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is None:
            return None
        if any(not county or county.strip() != county for county in value):
            raise ValueError("counties must be non-empty trimmed strings")
        return _sorted_unique(value)

    @model_validator(mode="after")
    def _ordered_window(self) -> Self:
        values = (self.counties, self.payment_window_start, self.payment_window_end)
        if any(value is not None for value in values) and not all(
            value is not None for value in values
        ):
            raise ValueError("disaster workflow frozen inputs must be all present or all absent")
        if (
            self.payment_window_start is not None
            and self.payment_window_end is not None
            and self.payment_window_end < self.payment_window_start
        ):
            raise ValueError("payment window end precedes start")
        return self

    def canonical_values(self) -> dict[str, object]:
        """Return the exact immutable values compared with a runtime profile."""

        if self.counties is None:
            return {}
        assert self.payment_window_start is not None
        assert self.payment_window_end is not None
        return {
            "counties": self.counties,
            "payment_window_end": self.payment_window_end.isoformat(),
            "payment_window_start": self.payment_window_start.isoformat(),
        }


class WorkflowProfileBinding(_FrozenModel):
    profile_id: str
    version: int = Field(ge=1)
    config_hash: str
    max_subjects: int = Field(ge=1, le=100)
    frozen_inputs: WorkflowFrozenInputs

    @field_validator("profile_id")
    @classmethod
    def _safe_profile_id(cls, value: str) -> str:
        if not _SAFE_ID.fullmatch(value):
            raise ValueError("workflow profile id has an invalid shape")
        return value

    @field_validator("config_hash")
    @classmethod
    def _config_hash_shape(cls, value: str) -> str:
        if not _HEX64.fullmatch(value):
            raise ValueError("workflow profile config hash must be lowercase sha256")
        return value


class AgentManifestDefinition(_FrozenModel):
    agent_id: str
    version: int = Field(ge=1)
    label: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=500)
    owner_label: str = Field(min_length=1, max_length=120)
    visibility: AgentVisibility
    lifecycle: Lifecycle
    allowed_strategies: tuple[ExecutionStrategy, ...] = Field(min_length=1)
    default_strategy: ExecutionStrategy
    #: The Agent's own system prompt. Operator-authored, sent to the model on
    #: every request of an agentic run, AHEAD of the rendered Skill index when
    #: one exists. Optional in general because a Skill-pinned Agent already
    #: composes a non-empty prompt from its index; REQUIRED (see
    #: ``_strategy_contract``) for an agentic Agent with no Skill pins, whose
    #: composed prompt would otherwise be empty — a misconfiguration this
    #: manifest refuses at load time rather than letting every run of that
    #: Agent die at the runtime's non-empty-instructions guard.
    instructions: str | None = Field(default=None, max_length=4000)
    model_policy_id: str
    tool_grants: tuple[str, ...]
    optional_tool_grants: tuple[str, ...] = ()
    knowledge_sources: tuple[KnowledgeGrant, ...]
    optional_knowledge_sources: tuple[KnowledgeGrant, ...] = ()
    skills: tuple[SkillGrant, ...]
    budgets: AgentBudgets
    availability_dependencies: tuple[AvailabilityDependency, ...]
    workflow: WorkflowProfileBinding | None = None

    @field_validator("agent_id")
    @classmethod
    def _opaque_id(cls, value: str) -> str:
        if not _OPAQUE_AGENT_ID.fullmatch(value):
            raise ValueError("Agent id must use the opaque agt_ identifier shape")
        return value

    @field_validator("instructions")
    @classmethod
    def _substantive_instructions(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or value.strip() != value):
            raise ValueError("Agent instructions must be non-empty trimmed text")
        return value

    @field_validator("model_policy_id")
    @classmethod
    def _model_policy_shape(cls, value: str) -> str:
        if not _SAFE_ID.fullmatch(value):
            raise ValueError("model policy id has an invalid shape")
        return value

    @field_validator("allowed_strategies", "tool_grants", "optional_tool_grants")
    @classmethod
    def _canonical_string_sets(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item or item.strip() != item for item in value):
            raise ValueError("set values must be non-empty trimmed strings")
        return _sorted_unique(value)

    @field_validator("knowledge_sources", "optional_knowledge_sources")
    @classmethod
    def _canonical_knowledge(cls, value: tuple[KnowledgeGrant, ...]) -> tuple[KnowledgeGrant, ...]:
        ids = [item.source_id for item in value]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate knowledge source id")
        return tuple(sorted(value, key=lambda item: item.source_id))

    @field_validator("skills")
    @classmethod
    def _canonical_skills(cls, value: tuple[SkillGrant, ...]) -> tuple[SkillGrant, ...]:
        """One id, one pin: an Agent pins one version of a Skill or none.

        The Run surface and the governed Skills view both key an Agent's Skills by
        NAME, so a second grant for the same id has no single answer whatever its
        version or hash.  Rejecting the id subsumes the older ``(skill_id, version)``
        pair check entirely -- equal pairs share an id -- so no pair check remains.
        """
        seen: set[str] = set()
        for item in value:
            if item.skill_id in seen:
                raise ValueError(
                    f"duplicate Skill id {item.skill_id!r}: "
                    "an Agent pins one version of a Skill or none"
                )
            seen.add(item.skill_id)
        return tuple(sorted(value, key=lambda item: (item.skill_id, item.version)))

    @field_validator("availability_dependencies")
    @classmethod
    def _canonical_dependencies(
        cls, value: tuple[AvailabilityDependency, ...]
    ) -> tuple[AvailabilityDependency, ...]:
        keys = [(item.kind, item.dependency_id, item.version, item.content_hash) for item in value]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate availability dependency")
        return tuple(
            sorted(
                value,
                key=lambda item: (
                    item.kind,
                    item.dependency_id,
                    item.version or 0,
                    item.content_hash or "",
                ),
            )
        )

    @model_validator(mode="after")
    def _strategy_contract(self) -> Self:
        if not set(self.optional_tool_grants) <= set(self.tool_grants):
            raise ValueError("optional Tool grants must be a subset of Tool grants")
        declared_knowledge = {
            (grant.source_id, tier) for grant in self.knowledge_sources for tier in grant.tiers
        }
        optional_knowledge = {
            (grant.source_id, tier)
            for grant in self.optional_knowledge_sources
            for tier in grant.tiers
        }
        if not optional_knowledge <= declared_knowledge:
            raise ValueError("optional Knowledge grants must be a subset of Knowledge grants")
        if self.default_strategy not in self.allowed_strategies:
            raise ValueError("default strategy must be allowed")
        if "agentic" in self.allowed_strategies and not self.skills and self.instructions is None:
            # Fail HERE — at manifest load, before the catalog admits the Agent —
            # not at run time. An agentic Agent's runtime prompt is its own
            # instructions plus the rendered Skill index, and ``render_index``
            # deliberately renders zero pins as the empty string; with neither,
            # every run of the Agent would be admitted and then die at the
            # runtime's non-empty-instructions guard. An agentic Agent nobody
            # instructed is a misconfiguration, and the registry is where it is
            # legible and attributable.
            raise ValueError(
                f"agentic Agent {self.agent_id!r} pins no Skills and declares no "
                "instructions, so every run would start with an empty system prompt; "
                "declare `instructions` in the registry"
            )
        if "deterministic" in self.allowed_strategies and self.workflow is None:
            raise ValueError("deterministic Agent requires a workflow profile binding")
        if self.workflow is not None and "deterministic" not in self.allowed_strategies:
            raise ValueError("workflow profile binding requires deterministic strategy")
        return self

    @property
    def definition_hash(self) -> str:
        payload = self.model_dump(mode="json", exclude_none=True)
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class AgentManifest(_FrozenModel):
    schema_version: Annotated[int, Field(ge=1, le=1)]
    generation: int = Field(ge=1)
    agents: tuple[AgentManifestDefinition, ...] = ()

    @field_validator("agents")
    @classmethod
    def _unique_active_agents(
        cls, value: tuple[AgentManifestDefinition, ...]
    ) -> tuple[AgentManifestDefinition, ...]:
        ids = [agent.agent_id for agent in value]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate Agent id")
        return tuple(sorted(value, key=lambda agent: agent.agent_id))


class AgentManifestFingerprint(_FrozenModel):
    schema_version: int
    generation: int
    raw_hash: str
    semantic_hash: str


class LoadedAgentManifest(_FrozenModel):
    configured: bool
    manifest: AgentManifest
    raw_hash: str | None
    semantic_hash: str | None

    @property
    def fingerprint(self) -> AgentManifestFingerprint | None:
        if not self.configured:
            return None
        assert self.raw_hash is not None
        assert self.semantic_hash is not None
        return AgentManifestFingerprint(
            schema_version=self.manifest.schema_version,
            generation=self.manifest.generation,
            raw_hash=self.raw_hash,
            semantic_hash=self.semantic_hash,
        )


def _semantic_hash(manifest: AgentManifest) -> str:
    payload = manifest.model_dump(mode="json", exclude_none=True)
    # Generation is an ordering/fencing coordinate carried separately in the
    # fingerprint. Semantic identity describes only the registry definition
    # set, so a generation-only or comment-only rewrite cannot masquerade as a
    # genuine add/remove/higher-version transition.
    payload.pop("generation")
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_agent_manifest(path: str | Path) -> LoadedAgentManifest:
    """Load one configured TOML file, or return the honest default-off registry."""
    if not path:
        empty = AgentManifest(schema_version=1, generation=1, agents=())
        return LoadedAgentManifest(
            configured=False,
            manifest=empty,
            raw_hash=None,
            semantic_hash=None,
        )

    source = Path(path)
    try:
        raw = source.read_bytes()
    except OSError as exc:
        raise AgentManifestError("configured Agent manifest is unavailable") from exc
    try:
        decoded = raw.decode("utf-8")
        document = tomllib.loads(decoded)
        manifest = AgentManifest.model_validate(document)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError, ValidationError) as exc:
        raise AgentManifestError("invalid Agent manifest") from exc
    return LoadedAgentManifest(
        configured=True,
        manifest=manifest,
        raw_hash=hashlib.sha256(raw).hexdigest(),
        semantic_hash=_semantic_hash(manifest),
    )
