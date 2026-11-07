# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Focused controls for the PRE-011A metadata-only capability inventory."""

from __future__ import annotations

import ast
import dataclasses
import inspect
from collections.abc import Callable

from ne.contracts.runtime import ToolSpec
from ne.contracts.types import ApprovalTier, Principal
from ne.runtime.agent_capabilities import (
    AgentCapabilityInventory,
    AgentSourceGrant,
    AgentToolGrant,
    ConnectorCapabilityBinding,
    EligibleConnectorCapability,
    EligibleSourceGrantMetadata,
    EligibleToolCapability,
    StaticCapabilityVisibility,
    ToolCapabilityBinding,
    project_agent_capabilities,
)

_AGENT_ID = "agt_meridian_case_assistant"
_HASH_V1 = "1" * 64
_HASH_V2 = "2" * 64
_VISIBLE = StaticCapabilityVisibility(all_authenticated=True)


def _spec(name: str, *, tier: ApprovalTier = ApprovalTier.TIER_0) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=f"raw description for {name}",
        tier=tier,
        input_schema={"type": "object", "secret-shaped-field": {"type": "string"}},
    )


def _tool_binding(
    name: str,
    *,
    label: str | None = None,
    read_write_class: str = "read",
    connection: str = "Meridian Knowledge Service",
) -> ToolCapabilityBinding:
    return ToolCapabilityBinding(
        tool_name=name,
        safe_label=label or f"Safe {name}",
        read_write_class=read_write_class,  # type: ignore[arg-type]
        connection_identity_label=connection,
    )


def _connector_binding(
    connection: str,
    *,
    source_id: str = "meridian-servicing",
    label: str | None = None,
    kind: str = "document-service",
    credential_owner: str = "Meridian Connector Operations",
    tiers: tuple[str, ...] = ("t1",),
    health: bool | None = True,
) -> ConnectorCapabilityBinding:
    return ConnectorCapabilityBinding(
        source_id=source_id,
        connection_identity_label=connection,
        safe_label=label or f"Safe {connection}",
        kind=kind,
        sanitized_credential_owner_label=credential_owner,
        composed_tiers=tiers,  # type: ignore[arg-type]
        composed_health=health,
    )


def _tool_grant(
    tool_name: str,
    *,
    agent_id: str = _AGENT_ID,
    version: int = 1,
    definition_hash: str = _HASH_V1,
) -> AgentToolGrant:
    return AgentToolGrant(
        agent_id=agent_id,
        agent_version=version,
        agent_definition_hash=definition_hash,
        tool_name=tool_name,
    )


def _source_grant(
    source_id: str = "meridian-servicing",
    *,
    agent_id: str = _AGENT_ID,
    version: int = 1,
    definition_hash: str = _HASH_V1,
    tiers: tuple[str, ...] = ("t1",),
) -> AgentSourceGrant:
    return AgentSourceGrant(
        agent_id=agent_id,
        agent_version=version,
        agent_definition_hash=definition_hash,
        source_id=source_id,
        tiers=tiers,  # type: ignore[arg-type]
    )


def _project(
    *,
    principal: Principal | None = None,
    agent_id: str = _AGENT_ID,
    version: int = 1,
    definition_hash: str = _HASH_V1,
    specs: tuple[ToolSpec, ...] = (),
    tool_bindings: tuple[ToolCapabilityBinding, ...] = (),
    tool_grants: tuple[AgentToolGrant, ...] = (),
    tool_visibility: dict[str, StaticCapabilityVisibility] | None = None,
    tool_health: dict[str, bool | None] | None = None,
    connector_bindings: tuple[ConnectorCapabilityBinding, ...] = (),
    source_grants: tuple[AgentSourceGrant, ...] = (),
    connector_visibility: dict[str, StaticCapabilityVisibility] | None = None,
) -> AgentCapabilityInventory:
    return project_agent_capabilities(
        agent_id=agent_id,
        agent_version=version,
        agent_definition_hash=definition_hash,
        principal=principal or Principal(id="alice"),
        registered_specs=specs,
        tool_bindings=tool_bindings,
        agent_tool_grants=tool_grants,
        static_tool_visibility=tool_visibility or {},
        composed_tool_health=tool_health or {},
        connector_bindings=connector_bindings,
        agent_source_grants=source_grants,
        static_connector_visibility=connector_visibility or {},
    )


def test_projects_only_safe_configured_tool_and_connector_metadata() -> None:
    tool = _spec("read.knowledge.search", tier=ApprovalTier.TIER_1)
    connection = "Meridian SharePoint Service"

    result = _project(
        specs=(tool,),
        tool_bindings=(
            _tool_binding(
                tool.name,
                label="Search governed knowledge",
                connection=connection,
            ),
        ),
        tool_grants=(_tool_grant(tool.name),),
        tool_visibility={tool.name: _VISIBLE},
        tool_health={tool.name: True},
        connector_bindings=(
            _connector_binding(
                connection,
                label="Governed servicing documents",
                kind="document-service",
                tiers=("t1", "t2"),
            ),
        ),
        source_grants=(_source_grant(tiers=("t1", "t2")),),
        connector_visibility={connection: _VISIBLE},
    )

    assert dataclasses.asdict(result) == {
        "agent_id": _AGENT_ID,
        "agent_version": 1,
        "agent_definition_hash": _HASH_V1,
        "tools": (
            {
                "name": tool.name,
                "label": "Search governed knowledge",
                "read_write_class": "read",
                "declared_tier": ApprovalTier.TIER_1,
                "effective_tier": ApprovalTier.TIER_1,
                "connection_identity_label": connection,
                "health": "healthy",
                "governing_grant": {
                    "agent_id": _AGENT_ID,
                    "agent_version": 1,
                    "agent_definition_hash": _HASH_V1,
                    "tool_name": tool.name,
                },
                # NE-CAPTAB: the spec's model-facing description IS projected now
                # (it already ships to the model in the in-band catalog). The
                # fixture schema has no "properties" map, so the argument surface
                # is honestly UNKNOWN (None) — never invented; and no binding
                # declared answer columns.
                "description": f"raw description for {tool.name}",
                "arguments": None,
                "answer_columns": None,
            },
        ),
        "connectors": (
            {
                "connection_identity_label": connection,
                "label": "Governed servicing documents",
                "kind": "document-service",
                "credential_owner_label": "Meridian Connector Operations",
                "health": "healthy",
                "governing_grant": {
                    "agent_id": _AGENT_ID,
                    "agent_version": 1,
                    "agent_definition_hash": _HASH_V1,
                    "source_id": "meridian-servicing",
                    "tiers": ("t1", "t2"),
                },
            },
        ),
    }


def test_two_principals_and_same_role_missing_scope_are_indistinguishable() -> None:
    tool = _spec("read.knowledge.search")
    connection = "Restricted Knowledge Service"
    visibility = StaticCapabilityVisibility(
        roles=("case-worker",), required_scopes=("knowledge:read",)
    )
    common = {
        "specs": (tool,),
        "tool_bindings": (_tool_binding(tool.name, connection=connection),),
        "tool_grants": (_tool_grant(tool.name),),
        "tool_visibility": {tool.name: visibility},
        "tool_health": {tool.name: True},
        "connector_bindings": (_connector_binding(connection),),
        "source_grants": (_source_grant(),),
        "connector_visibility": {connection: visibility},
    }
    visible_principal = Principal(
        id="worker", groups=("role:case-worker",), claims={"scope": "knowledge:read"}
    )
    wrong_role = Principal(
        id="observer", groups=("role:observer",), claims={"scope": "knowledge:read"}
    )
    missing_scope = Principal(id="worker-2", groups=("role:case-worker",))

    visible = _project(principal=visible_principal, **common)
    role_hidden = _project(principal=wrong_role, **common)
    scope_hidden = _project(principal=missing_scope, **common)
    unknown = _project(principal=wrong_role)

    assert len(visible.tools) == len(visible.connectors) == 1
    assert role_hidden.tools == scope_hidden.tools == unknown.tools == ()
    assert role_hidden.connectors == scope_hidden.connectors == unknown.connectors == ()


def test_grants_bind_agent_id_version_and_definition_hash_independently() -> None:
    tool = _spec("read.records")
    connection = "Records Service"
    common = {
        "specs": (tool,),
        "tool_bindings": (_tool_binding(tool.name, connection=connection),),
        "tool_visibility": {tool.name: _VISIBLE},
        "tool_health": {tool.name: True},
        "connector_bindings": (_connector_binding(connection),),
        "connector_visibility": {connection: _VISIBLE},
    }

    def inventory(
        *, grant_agent_id: str = _AGENT_ID, version: int = 1, definition_hash: str = _HASH_V1
    ) -> AgentCapabilityInventory:
        return _project(
            tool_grants=(
                _tool_grant(
                    tool.name,
                    agent_id=grant_agent_id,
                    version=version,
                    definition_hash=definition_hash,
                ),
            ),
            source_grants=(
                _source_grant(
                    agent_id=grant_agent_id,
                    version=version,
                    definition_hash=definition_hash,
                ),
            ),
            **common,
        )

    assert inventory(grant_agent_id="agt_other_case_assistant").tools == ()
    assert inventory(grant_agent_id="agt_other_case_assistant").connectors == ()
    wrong_version = inventory(version=2)
    wrong_hash = inventory(definition_hash=_HASH_V2)
    assert wrong_version.tools == ()
    assert wrong_version.connectors == ()
    assert wrong_hash.tools == ()
    assert wrong_hash.connectors == ()
    exact = inventory()
    assert exact.agent_id == exact.tools[0].governing_grant.agent_id == _AGENT_ID
    assert exact.agent_version == exact.connectors[0].governing_grant.agent_version == 1
    assert exact.agent_definition_hash == _HASH_V1


def test_composition_authors_closed_read_write_class_including_draft_write() -> None:
    read = _spec("read.records", tier=ApprovalTier.TIER_1)
    write = _spec("draft.email.create", tier=ApprovalTier.TIER_2)

    result = _project(
        specs=(read, write),
        tool_bindings=(
            _tool_binding(read.name, read_write_class="read"),
            _tool_binding(write.name, read_write_class="write"),
        ),
        tool_grants=(_tool_grant(read.name), _tool_grant(write.name)),
        tool_visibility={read.name: _VISIBLE, write.name: _VISIBLE},
        tool_health={read.name: True, write.name: True},
    )

    assert [(row.name, row.read_write_class, row.effective_tier) for row in result.tools] == [
        ("draft.email.create", "write", ApprovalTier.TIER_2),
        ("read.records", "read", ApprovalTier.TIER_1),
    ]
    invalid = _project(
        specs=(read,),
        tool_bindings=(_tool_binding(read.name, read_write_class="execute"),),
        tool_grants=(_tool_grant(read.name),),
        tool_visibility={read.name: _VISIBLE},
    )
    assert invalid.tools == ()


def test_underdeclared_specs_are_rejected_instead_of_normalized() -> None:
    underdeclared = _spec("write.external.crm.update", tier=ApprovalTier.TIER_0)
    escalated = _spec("read.sensitive.records", tier=ApprovalTier.TIER_2)

    result = _project(
        specs=(underdeclared, escalated),
        tool_bindings=(
            _tool_binding(underdeclared.name, read_write_class="write"),
            _tool_binding(escalated.name, read_write_class="read"),
        ),
        tool_grants=(_tool_grant(underdeclared.name), _tool_grant(escalated.name)),
        tool_visibility={underdeclared.name: _VISIBLE, escalated.name: _VISIBLE},
    )

    assert [row.name for row in result.tools] == ["read.sensitive.records"]
    assert result.tools[0].declared_tier is ApprovalTier.TIER_2
    assert result.tools[0].effective_tier is ApprovalTier.TIER_2


def test_health_preserves_true_false_and_unknown_for_tools_and_connectors() -> None:
    tools = tuple(_spec(f"read.health.{suffix}") for suffix in ("true", "false", "unknown"))
    connections = tuple(f"Health {suffix} Service" for suffix in ("true", "false", "unknown"))

    result = _project(
        specs=tools,
        tool_bindings=tuple(
            _tool_binding(tool.name, connection=connection)
            for tool, connection in zip(tools, connections, strict=True)
        ),
        tool_grants=tuple(_tool_grant(tool.name) for tool in tools),
        tool_visibility={tool.name: _VISIBLE for tool in tools},
        tool_health={tools[0].name: True, tools[1].name: False},
        connector_bindings=tuple(
            _connector_binding(
                connection,
                source_id=f"health-{suffix}",
                health=health,
            )
            for connection, suffix, health in zip(
                connections, ("true", "false", "unknown"), (True, False, None), strict=True
            )
        ),
        source_grants=tuple(
            _source_grant(f"health-{suffix}") for suffix in ("true", "false", "unknown")
        ),
        connector_visibility={connection: _VISIBLE for connection in connections},
    )

    assert {row.name: row.health for row in result.tools} == {
        "read.health.false": "unhealthy",
        "read.health.true": "healthy",
        "read.health.unknown": "unknown",
    }
    assert {row.connection_identity_label: row.health for row in result.connectors} == {
        "Health false Service": "unhealthy",
        "Health true Service": "healthy",
        "Health unknown Service": "unknown",
    }


def test_every_tool_requires_safe_connection_identity_and_connector_is_independent() -> None:
    tool = _spec("read.records")
    connection = "Records Service"
    common = {
        "specs": (tool,),
        "tool_grants": (_tool_grant(tool.name),),
        "tool_visibility": {tool.name: _VISIBLE},
    }

    assert _project(tool_bindings=(_tool_binding(tool.name, connection=""),), **common).tools == ()
    tool_only = _project(tool_bindings=(_tool_binding(tool.name, connection=connection),), **common)
    connector_only = _project(
        connector_bindings=(_connector_binding(connection),),
        source_grants=(_source_grant(),),
        connector_visibility={connection: _VISIBLE},
    )

    assert tool_only.tools[0].connection_identity_label == connection
    assert tool_only.connectors == ()
    assert connector_only.tools == ()
    assert connector_only.connectors[0].connection_identity_label == connection

    ungranted = _project(
        specs=(tool,),
        tool_bindings=(_tool_binding(tool.name, connection=connection),),
        tool_visibility={tool.name: _VISIBLE},
    )
    assert ungranted.tools == ()


def test_connector_requires_exact_unambiguous_source_mapping_and_composed_tiers() -> None:
    connection = "Records Service"
    visible = {connection: _VISIBLE}

    missing_mapping = _project(
        source_grants=(_source_grant("source-a"),), connector_visibility=visible
    )
    wrong_mapping = _project(
        connector_bindings=(_connector_binding(connection, source_id="source-b"),),
        source_grants=(_source_grant("source-a"),),
        connector_visibility=visible,
    )
    uncomposed_tier = _project(
        connector_bindings=(_connector_binding(connection, source_id="source-a", tiers=("t1",)),),
        source_grants=(_source_grant("source-a", tiers=("t1", "t2")),),
        connector_visibility=visible,
    )
    ambiguous_source = _project(
        connector_bindings=(
            _connector_binding(connection, source_id="source-a"),
            _connector_binding("Other Records Service", source_id="source-a"),
        ),
        source_grants=(_source_grant("source-a"),),
        connector_visibility={connection: _VISIBLE, "Other Records Service": _VISIBLE},
    )
    ambiguous_connector = _project(
        connector_bindings=(
            _connector_binding(connection, source_id="source-a"),
            _connector_binding(connection, source_id="source-b"),
        ),
        source_grants=(_source_grant("source-a"), _source_grant("source-b")),
        connector_visibility=visible,
    )
    exact = _project(
        connector_bindings=(
            _connector_binding(connection, source_id="source-a", tiers=("t1", "t2")),
        ),
        source_grants=(_source_grant("source-a", tiers=("t1", "t2")),),
        connector_visibility=visible,
    )

    assert missing_mapping.connectors == wrong_mapping.connectors == ()
    assert uncomposed_tier.connectors == ambiguous_source.connectors == ()
    assert ambiguous_connector.connectors == ()
    assert exact.connectors[0].governing_grant.tiers == ("t1", "t2")


def test_canonical_manifest_source_id_is_value_discriminating() -> None:
    connection = "Records Service"

    def inventory(source_id: str) -> AgentCapabilityInventory:
        return _project(
            connector_bindings=(
                _connector_binding(connection, source_id=source_id, label="Records"),
            ),
            source_grants=(_source_grant(source_id),),
            connector_visibility={connection: _VISIBLE},
        )

    source_a = inventory("source-a")
    source_b = inventory("source-b")

    assert source_a.connectors[0].connection_identity_label == connection
    assert source_a.connectors[0].label == source_b.connectors[0].label == "Records"
    assert source_a.connectors[0].governing_grant.source_id == "source-a"
    assert source_b.connectors[0].governing_grant.source_id == "source-b"
    assert source_a != source_b


def test_knowledge_tier_input_order_produces_identical_canonical_inventory() -> None:
    connection = "Records Service"

    def inventory(tiers: tuple[str, ...]) -> AgentCapabilityInventory:
        return _project(
            connector_bindings=(
                _connector_binding(
                    connection,
                    source_id="source-a",
                    tiers=("t2", "t1"),
                ),
            ),
            source_grants=(_source_grant("source-a", tiers=tiers),),
            connector_visibility={connection: _VISIBLE},
        )

    canonical = inventory(("t1", "t2"))
    reversed_input = inventory(("t2", "t1"))

    assert canonical == reversed_input
    assert reversed_input.connectors[0].governing_grant.tiers == ("t1", "t2")


def test_output_fields_and_imports_exclude_discovered_identity_schema_and_authority() -> None:
    from ne.runtime import agent_capabilities

    tree = ast.parse(inspect.getsource(agent_capabilities))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)
    forbidden_imports = (
        "ne.api",
        "ne.connect",
        "ne.govern",
        "ne.audit",
        "ne.events",
        "ne.contracts.integration",
    )
    assert not any(module.startswith(forbidden_imports) for module in imported)

    # NE-CAPTAB widened the closed set by exactly three CONTRACT fields —
    # description, arguments (the SANITIZED per-argument projection, never the
    # raw mapping), answer_columns — all derived from schema metadata the model
    # already sees in the in-band catalog. `input_schema` itself stays forbidden.
    assert {field.name for field in dataclasses.fields(EligibleToolCapability)} == {
        "name",
        "label",
        "read_write_class",
        "declared_tier",
        "effective_tier",
        "connection_identity_label",
        "health",
        "governing_grant",
        "description",
        "arguments",
        "answer_columns",
    }
    assert {field.name for field in dataclasses.fields(EligibleConnectorCapability)} == {
        "connection_identity_label",
        "label",
        "kind",
        "credential_owner_label",
        "health",
        "governing_grant",
    }
    assert {field.name for field in dataclasses.fields(EligibleSourceGrantMetadata)} == {
        "agent_id",
        "agent_version",
        "agent_definition_hash",
        "source_id",
        "tiers",
    }
    # "description" left this set with NE-CAPTAB (it is a sanitized projection
    # of an already-model-visible string); the RAW schema/handler surface stays.
    forbidden_fields = {
        "source_name",
        "input_schema",
        "handler",
        "args",
        "pdp_reason",
        "endpoint",
        "credential_ref",
        "connector_instance_id",
        "tenant_id",
        "client_id",
        "secret",
    }
    assert not forbidden_fields.intersection(
        field.name
        for field in (
            *dataclasses.fields(EligibleToolCapability),
            *dataclasses.fields(EligibleConnectorCapability),
        )
    )


def _complete_duplicate_fixture() -> dict[str, object]:
    tool = _spec("read.duplicate")
    connection = "Duplicate Service"
    return {
        "specs": (tool,),
        "tool_bindings": (_tool_binding(tool.name, connection=connection),),
        "tool_grants": (_tool_grant(tool.name),),
        "tool_visibility": {tool.name: _VISIBLE},
        "connector_bindings": (_connector_binding(connection),),
        "source_grants": (_source_grant(),),
        "connector_visibility": {connection: _VISIBLE},
    }


def test_each_tool_duplicate_factor_fails_closed_independently() -> None:
    mutations: tuple[Callable[[dict[str, object]], None], ...] = (
        lambda values: values.update(specs=values["specs"] * 2),  # type: ignore[operator]
        lambda values: values.update(tool_bindings=values["tool_bindings"] * 2),  # type: ignore[operator]
        lambda values: values.update(tool_grants=values["tool_grants"] * 2),  # type: ignore[operator]
    )
    for mutate in mutations:
        values = _complete_duplicate_fixture()
        mutate(values)
        result = _project(**values)  # type: ignore[arg-type]
        assert result.tools == ()
        assert len(result.connectors) == 1


def test_each_connector_duplicate_factor_fails_closed_independently() -> None:
    mutations: tuple[Callable[[dict[str, object]], None], ...] = (
        lambda values: values.update(
            connector_bindings=values["connector_bindings"] * 2  # type: ignore[operator]
        ),
        lambda values: values.update(source_grants=values["source_grants"] * 2),  # type: ignore[operator]
    )
    for mutate in mutations:
        values = _complete_duplicate_fixture()
        mutate(values)
        result = _project(**values)  # type: ignore[arg-type]
        assert len(result.tools) == 1
        assert result.connectors == ()


def test_output_order_is_independent_of_all_input_ordering() -> None:
    alpha = _spec("read.alpha")
    zeta = _spec("read.zeta")
    connection_a = "Alpha Service"
    connection_z = "Zeta Service"

    def ordered(reverse: bool) -> AgentCapabilityInventory:
        tools = (zeta, alpha) if reverse else (alpha, zeta)
        connections = (connection_z, connection_a) if reverse else (connection_a, connection_z)
        source_ids = ("source-z", "source-a") if reverse else ("source-a", "source-z")
        return _project(
            specs=tools,
            tool_bindings=tuple(
                _tool_binding(tool.name, connection=connection)
                for tool, connection in zip(tools, connections, strict=True)
            ),
            tool_grants=tuple(_tool_grant(tool.name) for tool in tools),
            tool_visibility={tool.name: _VISIBLE for tool in tools},
            connector_bindings=tuple(
                _connector_binding(connection, source_id=source_id)
                for connection, source_id in zip(connections, source_ids, strict=True)
            ),
            source_grants=tuple(_source_grant(source_id) for source_id in source_ids),
            connector_visibility={connection: _VISIBLE for connection in connections},
        )

    assert ordered(False) == ordered(True)
    assert [row.name for row in ordered(True).tools] == ["read.alpha", "read.zeta"]
    assert [row.connection_identity_label for row in ordered(True).connectors] == [
        "Alpha Service",
        "Zeta Service",
    ]


# --- NE-CAPTAB: the advertised contract projection ----------------------------------


def _contract_inventory(spec: ToolSpec, **binding_kwargs: object) -> AgentCapabilityInventory:
    binding = dataclasses.replace(_tool_binding(spec.name), **binding_kwargs)  # type: ignore[arg-type]
    return _project(
        specs=(spec,),
        tool_bindings=(binding,),
        tool_grants=(_tool_grant(spec.name),),
        tool_visibility={spec.name: _VISIBLE},
    )


def test_projects_the_advertised_argument_contract_from_the_registered_schema() -> None:
    """The multikey/enum/date shapes the template generator emits all project."""
    spec = ToolSpec(
        name="read.kg.payments_due_window",
        description="Payment rows in an inclusive date window.",
        tier=ApprovalTier.TIER_0,
        input_schema={
            "type": "object",
            "properties": {
                "q_loan_id": {
                    "title": "Q Loan Id",
                    "description": "Loan id, mer-loan-NNN.",
                    "anyOf": [
                        {"type": "string", "pattern": "^mer-loan-[0-9]{3}$", "maxLength": 12},
                        {
                            "type": "array",
                            "items": {"type": "string", "pattern": "^mer-loan-[0-9]{3}$"},
                            "minItems": 1,
                            "maxItems": 16,
                        },
                    ],
                },
                "window_start": {"type": "string", "format": "date"},
                "q_status": {"type": "string", "enum": ["closed", "open", "withdrawn"]},
            },
            "required": ["q_loan_id", "window_start"],
        },
    )
    inventory = _contract_inventory(spec, answer_columns=("payment_id", "due_date"))
    (tool,) = inventory.tools
    assert tool.description == "Payment rows in an inclusive date window."
    assert tool.answer_columns == ("payment_id", "due_date")
    assert tool.arguments is not None
    by_name = {argument.name: argument for argument in tool.arguments}
    loan = by_name["q_loan_id"]
    assert (loan.type_label, loan.required, loan.list_max) == ("string", True, 16)
    assert (loan.pattern, loan.max_length) == ("^mer-loan-[0-9]{3}$", 12)
    assert loan.description == "Loan id, mer-loan-NNN."
    window = by_name["window_start"]
    assert (window.type_label, window.required) == ("date", True)
    # Unstated facts stay None — never a guessed default.
    assert (window.description, window.pattern, window.max_length, window.list_max) == (
        None,
        None,
        None,
        None,
    )
    status = by_name["q_status"]
    assert status.enum == ("closed", "open", "withdrawn")
    assert status.required is False


def test_distinguishes_no_arguments_from_an_unstated_argument_surface() -> None:
    stated_empty = ToolSpec(
        name="read.zero",
        description="Zero-arg tool.",
        tier=ApprovalTier.TIER_0,
        input_schema={"type": "object", "properties": {}},
    )
    inventory = _contract_inventory(stated_empty)
    (tool,) = inventory.tools
    assert tool.arguments == ()  # the schema STATES "no arguments"

    unstated = ToolSpec(name="read.opaque", description="", tier=ApprovalTier.TIER_0)
    inventory = _contract_inventory(unstated)
    (tool,) = inventory.tools
    assert tool.arguments is None  # no property map -> UNKNOWN, not "none"
    assert tool.description == ""
    assert tool.answer_columns is None
