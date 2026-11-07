# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Principal-scoped, metadata-only Agent Tool and Connector inventory.

Composition injects exact registered Tool specs, production-shaped Agent Tool and
Knowledge grants, closed safe display metadata, static visibility, and composed
health. This module only intersects and projects those inputs. It exposes the canonical
manifest source id needed to bind a Knowledge grant and (NE-CAPTAB) a SANITIZED
projection of each Tool's advertised input contract — argument names, types,
constraints and descriptions, i.e. exactly the schema metadata the in-band tool
catalog already ships to the model on every turn — but never discovered
source/customer identity, the raw schema mapping itself, handlers, PDP reasons,
credentials, or evidence/audit state.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, TypeGuard

from ne.contracts.runtime import ToolSpec
from ne.contracts.types import ApprovalTier, Principal
from ne.runtime.actions import tier_for_action
from ne.runtime.errors import ToolRegistrationError

CapabilityHealth = Literal["healthy", "unhealthy", "unknown"]
KnowledgeTier = Literal["t1", "t2", "t3"]
ReadWriteClass = Literal["read", "write"]

_MAX_SAFE_LABEL_CHARS = 256
_READ_WRITE_CLASSES = frozenset({"read", "write"})
_KNOWLEDGE_TIERS = frozenset({"t1", "t2", "t3"})


@dataclass(frozen=True, slots=True)
class StaticCapabilityVisibility:
    """Deny-default static visibility for one Tool or configured Connector."""

    all_authenticated: bool = False
    principals: tuple[str, ...] = ()
    groups: tuple[str, ...] = ()
    roles: tuple[str, ...] = ()
    required_scopes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AgentToolGrant:
    """One Tool grant bound to an exact immutable Agent definition."""

    agent_id: str
    agent_version: int
    agent_definition_hash: str
    tool_name: str


@dataclass(frozen=True, slots=True)
class AgentSourceGrant:
    """One production-shaped Knowledge grant bound to an exact Agent definition."""

    agent_id: str
    agent_version: int
    agent_definition_hash: str
    source_id: str
    tiers: tuple[KnowledgeTier, ...]


@dataclass(frozen=True, slots=True)
class ToolCapabilityBinding:
    """Composition-authored closed safe metadata for one registered Tool.

    ``answer_columns`` (NE-CAPTAB): the named columns a verified-answer template
    declares in its ``[answer]`` block — schema metadata (column NAMES, never
    values), authored by composition from the template registry. ``None`` — the
    default and every non-template tool — means "this tool does not state a
    column contract", which the console renders as an honest gap, not as
    "returns nothing".
    """

    tool_name: str
    safe_label: str
    read_write_class: ReadWriteClass
    connection_identity_label: str
    answer_columns: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class ConnectorCapabilityBinding:
    """Explicit source-to-safe-configured-Connector composition mapping.

    ``source_id`` is the canonical manifest join key, not a discovered customer id.
    The remaining strings are explicitly sanitized configuration metadata.
    """

    source_id: str
    connection_identity_label: str
    safe_label: str
    kind: str
    sanitized_credential_owner_label: str
    composed_tiers: tuple[KnowledgeTier, ...]
    composed_health: bool | None


@dataclass(frozen=True, slots=True)
class ToolArgumentCapability:
    """One argument of a Tool's advertised input contract — SCHEMA METADATA ONLY.

    Every field here is derived from the registered ``ToolSpec.input_schema``,
    which already ships verbatim to the model in the in-band tool catalog on
    every turn — so projecting it to the console publishes nothing new (the
    ``ArgDef.enum`` C12 argument, applied to the whole schema). Values, retrieved
    content and credentials never appear in an input schema and so can never
    appear here. ``None`` on any optional field means the schema does not state
    it — an honest gap, never a default.
    """

    name: str
    #: Human type word from the schema — "string", "date", "integer", … —
    #: or ``None`` when the schema does not state one.
    type_label: str | None
    required: bool
    description: str | None
    enum: tuple[str, ...] | None
    pattern: str | None
    max_length: int | None
    #: Cardinality cap when the schema admits a list of values (NE-KG-MULTIKEY's
    #: ``scalar | list`` union); ``None`` = single value only.
    list_max: int | None


@dataclass(frozen=True, slots=True)
class EligibleToolCapability:
    """Safe Tool preflight metadata; deliberately not a raw ``ToolSpec``.

    NE-CAPTAB widened this from labels-only to the tool's advertised CONTRACT —
    ``description``, ``arguments`` and ``answer_columns`` — because the contract
    is precisely what the model already sees in the in-band catalog, and a
    console that hides it forces an operator to guess what an argument means.
    Still never a handler, a PDP reason, or the raw schema mapping itself.
    """

    name: str
    label: str
    read_write_class: ReadWriteClass
    declared_tier: ApprovalTier
    effective_tier: ApprovalTier
    connection_identity_label: str
    health: CapabilityHealth
    governing_grant: AgentToolGrant
    #: The spec's model-facing description; "" when the spec carries none.
    description: str = ""
    #: ``None`` = the input schema stated no property map (unknowable); an empty
    #: tuple = the schema states the tool takes no arguments. The distinction is
    #: load-bearing for an honest console ("no arguments" vs "not stated").
    arguments: tuple[ToolArgumentCapability, ...] | None = None
    #: Declared answer columns (verified templates); ``None`` = not stated.
    answer_columns: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class EligibleSourceGrantMetadata:
    """Safe identity/tier projection of a governing Knowledge grant."""

    agent_id: str
    agent_version: int
    agent_definition_hash: str
    source_id: str
    tiers: tuple[KnowledgeTier, ...]


@dataclass(frozen=True, slots=True)
class EligibleConnectorCapability:
    """Safe metadata for one explicitly configured Connector identity."""

    connection_identity_label: str
    label: str
    kind: str
    credential_owner_label: str
    health: CapabilityHealth
    governing_grant: EligibleSourceGrantMetadata


@dataclass(frozen=True, slots=True)
class AgentCapabilityInventory:
    """One exact immutable Agent-definition-bound capability snapshot."""

    agent_id: str
    agent_version: int
    agent_definition_hash: str
    tools: tuple[EligibleToolCapability, ...]
    connectors: tuple[EligibleConnectorCapability, ...]


def _safe_label(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    if not value or value.strip() != value or len(value) > _MAX_SAFE_LABEL_CHARS:
        return None
    if not value.isprintable():
        return None
    return value


def _valid_agent_identity(agent_id: object, version: object, definition_hash: object) -> bool:
    return (
        _safe_label(agent_id) is not None
        and isinstance(version, int)
        and not isinstance(version, bool)
        and version >= 1
        and isinstance(definition_hash, str)
        and len(definition_hash) == 64
        and all(character in "0123456789abcdef" for character in definition_hash)
    )


def _valid_tiers(tiers: tuple[KnowledgeTier, ...]) -> bool:
    return bool(tiers) and len(tiers) == len(set(tiers)) and set(tiers).issubset(_KNOWLEDGE_TIERS)


def _principal_scopes(principal: Principal) -> frozenset[str]:
    claim = principal.claims.get("scope")
    if not isinstance(claim, str):
        return frozenset()
    return frozenset(claim.split())


def _is_statically_visible(rule: StaticCapabilityVisibility, principal: Principal) -> bool:
    if not set(rule.required_scopes).issubset(_principal_scopes(principal)):
        return False
    if rule.all_authenticated or principal.id in rule.principals:
        return True
    groups = set(principal.groups)
    if groups.intersection(rule.groups):
        return True
    return bool(groups.intersection({f"role:{role}" for role in rule.roles}))


_MAX_SAFE_TEXT_CHARS = 2000


def _safe_text(value: object) -> str | None:
    """Sanitize free-text schema metadata (a description): printable, bounded."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > _MAX_SAFE_TEXT_CHARS or not text.isprintable():
        return None
    return text


def _is_sequence(value: object) -> TypeGuard[Sequence[object]]:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes))


def _scalar_branch(prop: Mapping[str, object]) -> tuple[Mapping[str, object], int | None]:
    """Resolve a ``scalar | list[scalar]`` union property to (scalar, list_max).

    NE-KG-MULTIKEY generates ``anyOf: [<scalar>, {type: array, items: <scalar>,
    maxItems: N}]``; both branches carry the identical per-element constraints, so
    reading the scalar branch loses nothing. A plain property returns itself with
    no cardinality cap.
    """
    branches = prop.get("anyOf")
    if not _is_sequence(branches):
        return prop, None
    scalar: Mapping[str, object] | None = None
    list_max: int | None = None
    for branch in branches:
        if not isinstance(branch, Mapping):
            continue
        if branch.get("type") == "array":
            max_items = branch.get("maxItems")
            if isinstance(max_items, int) and not isinstance(max_items, bool) and max_items >= 1:
                list_max = max_items
        elif scalar is None:
            scalar = branch
    return (scalar if scalar is not None else prop), list_max


def _tool_arguments(schema: object) -> tuple[ToolArgumentCapability, ...] | None:
    """Project a ToolSpec input schema's properties to safe argument metadata.

    ``None`` — the schema states no property map, so the argument surface is
    UNKNOWN. An empty tuple — the schema states ``properties: {}``, so the tool
    truly takes no arguments. Anything unparseable inside a single property is
    dropped to ``None`` on that field rather than guessed.
    """
    if not isinstance(schema, Mapping):
        return None
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        return None
    required_value = schema.get("required")
    required = (
        frozenset(name for name in required_value if isinstance(name, str))
        if _is_sequence(required_value)
        else frozenset()
    )
    rows: list[ToolArgumentCapability] = []
    for name, prop in properties.items():
        safe_name = _safe_label(name)
        if safe_name is None or not isinstance(prop, Mapping):
            continue
        scalar, list_max = _scalar_branch(prop)
        enum: tuple[str, ...] | None = None
        enum_value = scalar.get("enum")
        if _is_sequence(enum_value):
            members = tuple(
                member
                for member in enum_value
                if isinstance(member, str) and _safe_label(member) is not None
            )
            if members and len(members) == len(enum_value):
                enum = members
        pattern_value = scalar.get("pattern")
        pattern = pattern_value if isinstance(pattern_value, str) and pattern_value else None
        max_length_value = scalar.get("maxLength")
        max_length = (
            max_length_value
            if isinstance(max_length_value, int)
            and not isinstance(max_length_value, bool)
            and max_length_value >= 1
            else None
        )
        format_value = scalar.get("format")
        type_value = scalar.get("type")
        if format_value in ("date", "date-time"):
            type_label: str | None = str(format_value)
        elif isinstance(type_value, str) and type_value:
            type_label = type_value
        else:
            type_label = None
        rows.append(
            ToolArgumentCapability(
                name=safe_name,
                type_label=type_label,
                required=safe_name in required,
                description=_safe_text(prop.get("description")),
                enum=enum,
                pattern=pattern,
                max_length=max_length,
                list_max=list_max,
            )
        )
    return tuple(rows)


def _health(value: object) -> CapabilityHealth:
    if value is True:
        return "healthy"
    if value is False:
        return "unhealthy"
    return "unknown"


def _validated_effective_tier(spec: ToolSpec) -> ApprovalTier | None:
    """Reject an impossible under-tier spec instead of laundering it upward."""

    try:
        classified_tier = tier_for_action(spec.name)
    except ToolRegistrationError:
        return None
    if int(spec.tier) < int(classified_tier):
        return None
    return ApprovalTier(max(int(spec.tier), int(classified_tier)))


def _unique_by[T](items: Sequence[T], keys: Sequence[str]) -> dict[str, T]:
    """Return only rows whose supplied configured key occurs exactly once."""

    unique: dict[str, T] = {}
    duplicates: set[str] = set()
    for item, key in zip(items, keys, strict=True):
        if key in unique:
            duplicates.add(key)
        else:
            unique[key] = item
    for key in duplicates:
        del unique[key]
    return unique


def project_agent_capabilities(
    *,
    agent_id: str,
    agent_version: int,
    agent_definition_hash: str,
    principal: Principal,
    registered_specs: Sequence[ToolSpec],
    tool_bindings: Sequence[ToolCapabilityBinding],
    agent_tool_grants: Sequence[AgentToolGrant],
    static_tool_visibility: Mapping[str, StaticCapabilityVisibility],
    composed_tool_health: Mapping[str, bool | None],
    connector_bindings: Sequence[ConnectorCapabilityBinding],
    agent_source_grants: Sequence[AgentSourceGrant],
    static_connector_visibility: Mapping[str, StaticCapabilityVisibility],
) -> AgentCapabilityInventory:
    """Project deterministic, principal-visible Tool and Connector metadata."""

    if not _valid_agent_identity(agent_id, agent_version, agent_definition_hash):
        raise ValueError("Agent id/version/definition hash has an invalid shape")

    exact_identity = (agent_id, agent_version, agent_definition_hash)
    specs = _unique_by(registered_specs, [spec.name for spec in registered_specs])
    tool_metadata = _unique_by(tool_bindings, [binding.tool_name for binding in tool_bindings])
    exact_tool_grants = [
        grant
        for grant in agent_tool_grants
        if (grant.agent_id, grant.agent_version, grant.agent_definition_hash) == exact_identity
    ]
    tool_grants = _unique_by(exact_tool_grants, [grant.tool_name for grant in exact_tool_grants])

    tools: list[EligibleToolCapability] = []
    for name, grant in tool_grants.items():
        spec = specs.get(name)
        binding = tool_metadata.get(name)
        visibility = static_tool_visibility.get(name)
        if spec is None or binding is None or visibility is None:
            continue
        if not _is_statically_visible(visibility, principal):
            continue
        safe_name = _safe_label(name)
        label = _safe_label(binding.safe_label)
        connection = _safe_label(binding.connection_identity_label)
        operation_class = binding.read_write_class
        effective_tier = _validated_effective_tier(spec)
        if (
            safe_name is None
            or label is None
            or connection is None
            or operation_class not in _READ_WRITE_CLASSES
            or effective_tier is None
        ):
            continue
        answer_columns: tuple[str, ...] | None = None
        if binding.answer_columns is not None:
            columns = tuple(
                column for column in binding.answer_columns if _safe_label(column) is not None
            )
            if columns and len(columns) == len(binding.answer_columns):
                answer_columns = columns
        tools.append(
            EligibleToolCapability(
                name=safe_name,
                label=label,
                read_write_class=operation_class,
                declared_tier=spec.tier,
                effective_tier=effective_tier,
                connection_identity_label=connection,
                health=_health(composed_tool_health.get(name)),
                governing_grant=grant,
                description=_safe_text(spec.description) or "",
                arguments=_tool_arguments(spec.input_schema),
                answer_columns=answer_columns,
            )
        )

    source_mapping = _unique_by(
        connector_bindings,
        [binding.source_id for binding in connector_bindings],
    )
    connector_mapping = _unique_by(
        connector_bindings,
        [binding.connection_identity_label for binding in connector_bindings],
    )
    exact_source_grants = [
        grant
        for grant in agent_source_grants
        if (grant.agent_id, grant.agent_version, grant.agent_definition_hash) == exact_identity
        and _valid_tiers(grant.tiers)
    ]
    source_grants = _unique_by(
        exact_source_grants,
        [grant.source_id for grant in exact_source_grants],
    )

    connectors: list[EligibleConnectorCapability] = []
    for source_id, grant in source_grants.items():
        binding = source_mapping.get(source_id)
        if (
            binding is None
            or connector_mapping.get(binding.connection_identity_label) is not binding
        ):
            continue
        connection = _safe_label(binding.connection_identity_label)
        canonical_source_id = _safe_label(grant.source_id)
        visibility = static_connector_visibility.get(binding.connection_identity_label)
        if visibility is None or not _is_statically_visible(visibility, principal):
            continue
        label = _safe_label(binding.safe_label)
        kind = _safe_label(binding.kind)
        owner = _safe_label(binding.sanitized_credential_owner_label)
        if (
            connection is None
            or canonical_source_id is None
            or label is None
            or kind is None
            or owner is None
            or not _valid_tiers(binding.composed_tiers)
            or not set(grant.tiers).issubset(binding.composed_tiers)
        ):
            continue
        connectors.append(
            EligibleConnectorCapability(
                connection_identity_label=connection,
                label=label,
                kind=kind,
                credential_owner_label=owner,
                health=_health(binding.composed_health),
                governing_grant=EligibleSourceGrantMetadata(
                    agent_id=grant.agent_id,
                    agent_version=grant.agent_version,
                    agent_definition_hash=grant.agent_definition_hash,
                    source_id=canonical_source_id,
                    tiers=tuple(sorted(grant.tiers)),
                ),
            )
        )

    return AgentCapabilityInventory(
        agent_id=agent_id,
        agent_version=agent_version,
        agent_definition_hash=agent_definition_hash,
        tools=tuple(sorted(tools, key=lambda row: row.name)),
        connectors=tuple(sorted(connectors, key=lambda row: row.connection_identity_label)),
    )
