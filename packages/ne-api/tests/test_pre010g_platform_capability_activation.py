# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa

from ne.api.agent_catalog import effective_agent_tool_grants, reconcile_agent_catalog
from ne.api.agent_manifest import load_agent_manifest
from ne.api.agent_preflight import AgentCapabilityPreflightService
from ne.api.graph_tools import GRAPH_PATH_TOOL_NAME
from ne.api.platform import (
    _EMAIL_DRAFT_TOOL,
    _MERIDIAN_AGENT_ID,
    AgentCapabilityMetadata,
    _admitted_meridian_knowledge_sources,
    _compose_workflow_execution_bindings,
    _ComposedAgentCapabilityInventory,
    _has_admitted_m365_graph_population,
    _production_agent_capability_metadata,
    _production_agent_dependency_snapshot,
)
from ne.api.source_authority_descriptors import (
    GovernedLocalT3SourceAuthorityDescriptor,
    M365SourceAuthorityDescriptor,
)
from ne.api.tools import TOOL_NAME
from ne.api.workflows import (
    MERIDIAN_CUSTOMERS_TOOL,
    MERIDIAN_DISPUTES_DIRECTORY_TOOL,
    MERIDIAN_LOANS_TOOL,
    MERIDIAN_NOTES_TOOL,
    MERIDIAN_OPEN_DISPUTES_TOOL,
    MERIDIAN_OVERDUE_CREDIT_TOOL,
    MERIDIAN_PAYMENTS_TOOL,
)
from ne.contracts.runtime import ToolSpec
from ne.contracts.types import ApprovalTier, Principal
from ne.skills.tools import VIEW_TOOL_NAME

_ROOT = Path(__file__).parents[3]
_REGISTRY = _ROOT / "deploy/agents/registry.toml"
_SOURCE = "meridian-servicing"
_LOCAL_SOURCE = "meridian-governed-local-t3"
_SELECTED_RELEASE = "c" * 64
_LOCAL_T3_CONNECTION = f"governed-local-t3:selected-release:{_SELECTED_RELEASE}"


def _authority(*, resolved: bool = True) -> M365SourceAuthorityDescriptor:
    return M365SourceAuthorityDescriptor(
        source_kind="m365-sharepoint",
        source_name="meridian-servicing",
        connector_instance_id="m365-primary",
        source_generation=1,
        contract_epoch=3,
        contract_identity_hash="a" * 64,
        descriptor_schema_version=1,
        descriptor_hash="b" * 64,
        head_revision="head-1",
        freshness_sla_seconds=Decimal("900"),
        resolved=resolved,
    )


def _local_t3_authority(*, resolved: bool = True) -> GovernedLocalT3SourceAuthorityDescriptor:
    return GovernedLocalT3SourceAuthorityDescriptor(
        source_kind="governed_local_t3",
        dataset_name="meridian-v1",
        dataset_instance_id="local-t3-instance",
        source_generation=1,
        contract_epoch=2,
        contract_identity_hash="d" * 64,
        descriptor_schema_version=1,
        descriptor_hash="e" * 64,
        contract_head_revision="local-head-1",
        selected_release_id=_SELECTED_RELEASE,
        selected_release_algorithm_version=1,
        resolved=resolved,
    )


def _admit(**changes: bool):
    values = {
        "current_search_evidence_bound": True,
        "graph_host_bound": True,
        "graph_authority_provider_bound": True,
        "graph_population_admitted": True,
        "graph_path_bound": True,
    }
    values.update(changes)
    return _admitted_meridian_knowledge_sources(source_authority=_authority(), **values)


def test_t1_and_t2_admission_require_the_complete_current_evidence_chain() -> None:
    assert _admit() == frozenset({(_SOURCE, "t1"), (_SOURCE, "t2")})
    assert (
        _admitted_meridian_knowledge_sources(
            source_authority=None,
            current_search_evidence_bound=True,
            graph_host_bound=True,
            graph_authority_provider_bound=True,
            graph_population_admitted=True,
            graph_path_bound=True,
        )
        == frozenset()
    )
    assert (
        _admitted_meridian_knowledge_sources(
            source_authority=_authority(resolved=False),
            current_search_evidence_bound=True,
            graph_host_bound=True,
            graph_authority_provider_bound=True,
            graph_population_admitted=True,
            graph_path_bound=True,
        )
        == frozenset()
    )
    assert _admit(current_search_evidence_bound=False) == frozenset()
    for missing in (
        "graph_host_bound",
        "graph_authority_provider_bound",
        "graph_population_admitted",
        "graph_path_bound",
    ):
        assert _admit(**{missing: False}) == frozenset({(_SOURCE, "t1")})


def test_graph_population_fence_requires_current_m365_entity_evidence() -> None:
    engine = sa.create_engine("sqlite://")
    assert _has_admitted_m365_graph_population(engine) is False
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE ne_document_graph_evidence_artifacts "
            "(document_id TEXT, version_seq INTEGER, kind TEXT)"
        )
        conn.exec_driver_sql(
            "CREATE TABLE ne_document_source_authority_receipts "
            "(document_id TEXT, version_seq INTEGER, state TEXT)"
        )
        conn.exec_driver_sql(
            "CREATE TABLE ne_document_source_snapshots "
            "(document_id TEXT, version_seq INTEGER, source_system TEXT)"
        )
        conn.exec_driver_sql(
            "INSERT INTO ne_document_graph_evidence_artifacts VALUES ('doc', 1, 'entity')"
        )
        conn.exec_driver_sql(
            "INSERT INTO ne_document_source_authority_receipts VALUES ('doc', 1, 'present')"
        )
        conn.exec_driver_sql("INSERT INTO ne_document_source_snapshots VALUES ('doc', 1, 'other')")
    assert _has_admitted_m365_graph_population(engine) is False
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "UPDATE ne_document_source_snapshots SET source_system = 'm365-sharepoint'"
        )
    assert _has_admitted_m365_graph_population(engine) is True
    engine.dispose()


def test_production_metadata_is_closed_sanitized_and_never_invents_health() -> None:
    workflow_tools = frozenset(
        {
            MERIDIAN_CUSTOMERS_TOOL,
            MERIDIAN_LOANS_TOOL,
            MERIDIAN_PAYMENTS_TOOL,
            MERIDIAN_NOTES_TOOL,
            MERIDIAN_DISPUTES_DIRECTORY_TOOL,
            MERIDIAN_OPEN_DISPUTES_TOOL,
        }
    )
    tools = frozenset(
        {VIEW_TOOL_NAME, TOOL_NAME, GRAPH_PATH_TOOL_NAME, _EMAIL_DRAFT_TOOL, *workflow_tools}
    )
    t1_only = frozenset({(_SOURCE, "t1"), (_LOCAL_SOURCE, "t3")})
    metadata = _production_agent_capability_metadata(
        registered_tools=tools,
        admitted_sources=t1_only,
        source_authority=_authority(),
        governed_local_t3_source_authority=_local_t3_authority(),
        skill_ready=True,
        graph_host_bound=False,
    )
    assert metadata is not None
    by_name = {binding.tool_name: binding for binding in metadata.tool_bindings}
    assert set(by_name) == tools
    assert by_name[VIEW_TOOL_NAME].read_write_class == "read"
    assert by_name[TOOL_NAME].read_write_class == "read"
    assert by_name[GRAPH_PATH_TOOL_NAME].read_write_class == "read"
    assert by_name[_EMAIL_DRAFT_TOOL].read_write_class == "write"
    assert {by_name[name].read_write_class for name in workflow_tools} == {"read"}
    assert by_name[VIEW_TOOL_NAME].connection_identity_label == "ne-skills:run-surface"
    assert {
        by_name[name].connection_identity_label
        for name in (TOOL_NAME, GRAPH_PATH_TOOL_NAME, _EMAIL_DRAFT_TOOL)
    } == {"m365:meridian-servicing"}
    assert {by_name[name].connection_identity_label for name in workflow_tools} == {
        _LOCAL_T3_CONNECTION
    }
    connectors = {binding.source_id: binding for binding in metadata.connector_bindings}
    assert set(connectors) == {_SOURCE, _LOCAL_SOURCE}
    assert connectors[_SOURCE].composed_tiers == ("t1",)
    assert "secret" not in connectors[_SOURCE].sanitized_credential_owner_label.lower()
    assert connectors[_LOCAL_SOURCE].connection_identity_label == _LOCAL_T3_CONNECTION
    assert connectors[_LOCAL_SOURCE].composed_tiers == ("t3",)
    assert connectors[_LOCAL_SOURCE].composed_health is None
    assert connectors[_LOCAL_SOURCE].sanitized_credential_owner_label.startswith("Credential-free ")
    assert {metadata.static_tool_visibility[name].groups for name in workflow_tools} == {("ops",)}
    assert metadata.static_connector_visibility[_LOCAL_T3_CONNECTION].groups == ("ops",)
    assert metadata.static_connector_visibility["m365:meridian-servicing"].all_authenticated
    assert metadata.tool_health == {
        VIEW_TOOL_NAME: True,
        TOOL_NAME: True,
        GRAPH_PATH_TOOL_NAME: None,
        _EMAIL_DRAFT_TOOL: None,
        # NE-CS-SEND: no connector runtime-health signal exists for the send
        # either — construction is never laundered into a green status.
        "write.external.email.send": None,
    }

    incomplete_graph = _production_agent_capability_metadata(
        registered_tools=tools,
        admitted_sources=t1_only,
        source_authority=_authority(),
        governed_local_t3_source_authority=_local_t3_authority(),
        skill_ready=True,
        graph_host_bound=True,
    )
    assert incomplete_graph is not None
    assert incomplete_graph.tool_health[GRAPH_PATH_TOOL_NAME] is False


def test_frozen_m365_keeps_its_catalog_but_never_claims_live_health() -> None:
    """The retained T1/T2 catalog stays VISIBLE; its health goes UNKNOWN, never True.

    Deleting the frozen source would lose the retained Meridian corpus the demo
    reads. Reporting it healthy would be the fabrication the handoff forbids: a
    frozen snapshot performs no provider pull, so composition/boot success is not
    evidence of current provider health. The only truthful third state is unknown.
    """
    tools = frozenset({VIEW_TOOL_NAME, TOOL_NAME, GRAPH_PATH_TOOL_NAME, _EMAIL_DRAFT_TOOL})
    admitted = frozenset({(_SOURCE, "t1"), (_SOURCE, "t2")})

    def build(*, m365_snapshot: bool) -> AgentCapabilityMetadata | None:
        return _production_agent_capability_metadata(
            registered_tools=tools,
            admitted_sources=admitted,
            source_authority=_authority(),
            governed_local_t3_source_authority=None,
            skill_ready=True,
            graph_host_bound=True,
            m365_snapshot=m365_snapshot,
        )

    frozen = build(m365_snapshot=True)
    assert frozen is not None
    # RETAINED: the catalog still carries the M365 tools and its connector row.
    assert {b.tool_name for b in frozen.tool_bindings} == tools
    connectors = {b.source_id: b for b in frozen.connector_bindings}
    assert set(connectors) == {_SOURCE}
    assert connectors[_SOURCE].composed_tiers == ("t1", "t2")
    # UNKNOWN, never live/healthy — for the connector AND both M365-backed tools.
    assert connectors[_SOURCE].composed_health is None
    assert connectors[_SOURCE].safe_label == "Meridian servicing Microsoft 365 (frozen snapshot)"
    assert frozen.tool_health[TOOL_NAME] is None
    assert frozen.tool_health[GRAPH_PATH_TOOL_NAME] is None
    # The skills surface is NOT an M365 claim, so freezing must not blank it.
    assert frozen.tool_health[VIEW_TOOL_NAME] is True

    # COMPLEMENT: the very same admitted inputs DO report healthy when the source
    # is live, so the None above is the posture talking, not an absent admission.
    live = build(m365_snapshot=False)
    assert live is not None
    live_connectors = {b.source_id: b for b in live.connector_bindings}
    assert live_connectors[_SOURCE].composed_health is True
    assert live_connectors[_SOURCE].safe_label == "Meridian servicing Microsoft 365"
    assert live.tool_health[TOOL_NAME] is True
    assert live.tool_health[GRAPH_PATH_TOOL_NAME] is True


@pytest.mark.parametrize("local_authority", [None, _local_t3_authority(resolved=False)])
def test_workflow_metadata_is_absent_without_admitted_local_t3(
    local_authority: GovernedLocalT3SourceAuthorityDescriptor | None,
) -> None:
    workflow_tools = frozenset(
        {
            MERIDIAN_CUSTOMERS_TOOL,
            MERIDIAN_LOANS_TOOL,
            MERIDIAN_PAYMENTS_TOOL,
            MERIDIAN_NOTES_TOOL,
            MERIDIAN_DISPUTES_DIRECTORY_TOOL,
            MERIDIAN_OPEN_DISPUTES_TOOL,
        }
    )
    metadata = _production_agent_capability_metadata(
        registered_tools=frozenset({TOOL_NAME, *workflow_tools}),
        admitted_sources=frozenset({(_SOURCE, "t1"), (_LOCAL_SOURCE, "t3")}),
        source_authority=_authority(),
        governed_local_t3_source_authority=local_authority,
        skill_ready=False,
        graph_host_bound=False,
    )

    assert metadata is not None
    assert {binding.tool_name for binding in metadata.tool_bindings} == {TOOL_NAME}


def test_workflow_metadata_does_not_depend_on_m365_authority() -> None:
    workflow_tools = frozenset(
        {
            MERIDIAN_CUSTOMERS_TOOL,
            MERIDIAN_LOANS_TOOL,
            MERIDIAN_PAYMENTS_TOOL,
            MERIDIAN_NOTES_TOOL,
            MERIDIAN_DISPUTES_DIRECTORY_TOOL,
            MERIDIAN_OPEN_DISPUTES_TOOL,
        }
    )
    metadata = _production_agent_capability_metadata(
        registered_tools=frozenset({TOOL_NAME, *workflow_tools}),
        admitted_sources=frozenset({(_LOCAL_SOURCE, "t3")}),
        source_authority=None,
        governed_local_t3_source_authority=_local_t3_authority(),
        skill_ready=False,
        graph_host_bound=False,
    )

    assert metadata is not None
    assert {binding.tool_name for binding in metadata.tool_bindings} == workflow_tools
    assert {binding.connection_identity_label for binding in metadata.tool_bindings} == {
        _LOCAL_T3_CONNECTION
    }
    assert len(metadata.connector_bindings) == 1
    connector = metadata.connector_bindings[0]
    assert connector.source_id == _LOCAL_SOURCE
    assert connector.connection_identity_label == _LOCAL_T3_CONNECTION
    assert connector.kind == "governed-local-t3"
    assert connector.composed_tiers == ("t3",)
    assert connector.composed_health is None


def test_production_preflight_projects_exact_workflow_reads_without_widening_visibility() -> None:
    loaded = load_agent_manifest(_REGISTRY)
    profiles, bindings = _compose_workflow_execution_bindings(loaded)
    workflow_expected = {
        "agt_01k3hardshipreview0000000000": (
            MERIDIAN_LOANS_TOOL,
            MERIDIAN_CUSTOMERS_TOOL,
            MERIDIAN_PAYMENTS_TOOL,
            MERIDIAN_NOTES_TOOL,
        ),
        "agt_01k3currentsopreview000000000": (
            MERIDIAN_DISPUTES_DIRECTORY_TOOL,
            MERIDIAN_OPEN_DISPUTES_TOOL,
        ),
        "agt_01k3fabricationrefusal000000": (MERIDIAN_DISPUTES_DIRECTORY_TOOL,),
        "agt_01k3overduecreditreviewdet00": (MERIDIAN_OVERDUE_CREDIT_TOOL,),
    }
    all_tools = frozenset(
        {
            VIEW_TOOL_NAME,
            TOOL_NAME,
            GRAPH_PATH_TOOL_NAME,
            _EMAIL_DRAFT_TOOL,
            *(name for names in workflow_expected.values() for name in names),
        }
    )
    sources = frozenset({(_SOURCE, "t1"), (_SOURCE, "t2"), (_LOCAL_SOURCE, "t3")})
    pins = frozenset(
        (grant.skill_id, grant.version, grant.content_hash)
        for definition in loaded.manifest.agents
        for grant in definition.skills
    )
    dependencies = _production_agent_dependency_snapshot(
        registered_tools=all_tools,
        knowledge_sources=sources,
        skill_pins=pins,
        workflow_profiles=profiles,
        model_policies=frozenset(
            definition.model_policy_id for definition in loaded.manifest.agents
        ),
        agent_execution_bindings=bindings,
        agent_transport_ready=True,
    )
    engine = sa.create_engine("sqlite://")
    catalog = reconcile_agent_catalog(engine, loaded, dependencies)
    effective_grants = {
        row.definition.agent_id: effective_agent_tool_grants(row.definition, dependencies)
        for row in catalog.agents
    }
    metadata = _production_agent_capability_metadata(
        registered_tools=all_tools,
        admitted_sources=sources,
        source_authority=_authority(),
        governed_local_t3_source_authority=_local_t3_authority(),
        skill_ready=True,
        graph_host_bound=True,
    )
    assert metadata is not None
    inventory = _ComposedAgentCapabilityInventory(
        definitions={row.definition.agent_id: row.definition for row in catalog.agents},
        tool_specs=tuple(
            # ``draft.email.create`` is registered at its REAL declared tier. The
            # action NAME classifies to Tier 0 (the ``draft.`` prefix in
            # ``tier_for_action``/``tiers.rego``); only the DECLARED tier escalates
            # it to 2, exactly as the live composition registers it -- see
            # test_beat3_governed_draft_email_chain.py's
            # ``test_draft_only_platform_registers_the_tool_at_tier_2``.
            ToolSpec(
                name=name,
                description=f"Registered {name}",
                tier=(ApprovalTier.TIER_2 if name == _EMAIL_DRAFT_TOOL else ApprovalTier.TIER_0),
            )
            for name in sorted(all_tools)
        ),
        metadata=metadata,
        config_hash="c" * 64,
        effective_tool_grants=effective_grants,
    )
    preflight = AgentCapabilityPreflightService(
        agent_catalog=catalog,
        inventory_provider=inventory,
    )
    ops = Principal(id="user:ops", groups=("ops",))
    frontline = Principal(id="user:frontline", groups=("all-employees",))

    for agent_id, expected in workflow_expected.items():
        result = asyncio.run(preflight.preflight(agent_id=agent_id, principal=ops))
        assert result is not None and result.available is True
        assert result.capabilities is not None
        assert tuple(tool.name for tool in result.capabilities.tools) == tuple(sorted(expected))
        assert {tool.read_write_class for tool in result.capabilities.tools} == {"read"}
        assert {tool.connection_identity_label for tool in result.capabilities.tools} == {
            _LOCAL_T3_CONNECTION
        }
        assert {tool.health for tool in result.capabilities.tools} == {"unknown"}
        assert len(result.capabilities.connectors) == 1
        connector = result.capabilities.connectors[0]
        assert connector.connection_identity_label == _LOCAL_T3_CONNECTION
        assert connector.source_id == _LOCAL_SOURCE
        assert connector.tiers == ("t3",)
        assert connector.health == "unknown"
        assert asyncio.run(preflight.preflight(agent_id=agent_id, principal=frontline)) is None

    # NE-CS-SPLIT: the Analyst is READ-ONLY now — `draft.email.create` and
    # `read.skills.view` moved to Meridian Customer Support, so the persona gate
    # asserted here is on the verified-answer lane alone: `ops` sees the
    # group-scoped read.kg tools, the frontline persona does not.
    analyst_ops = asyncio.run(preflight.preflight(agent_id=_MERIDIAN_AGENT_ID, principal=ops))
    assert analyst_ops is not None and analyst_ops.available is True
    assert analyst_ops.capabilities is not None
    # NE-ANALYST-VERIFIED-LANE: the agentic Analyst now reaches the SAME governed
    # verified-answer tools the deterministic Meridian agents use, and the
    # projection says so — each carrying the local-T3 release's own connection
    # identity, not the M365 one, because that is the store it actually reads.
    # `read.kg.disaster_declarations` is granted but ships on d691f600, so it is
    # absent from this deployment's registered set and correctly does not project.
    _KG = (
        "read.kg.autoloans_for_customer",
        "read.kg.customers_in_counties",
        "read.kg.disputes_directory",
        "read.kg.open_disputes_by_status",
        "read.kg.payments_due_window",
        "read.kg.servicing_notes_for_loan",
    )
    assert tuple(tool.name for tool in analyst_ops.capabilities.tools) == (
        *_KG,
        GRAPH_PATH_TOOL_NAME,
        TOOL_NAME,
    )
    assert tuple(tool.read_write_class for tool in analyst_ops.capabilities.tools) == (
        *("read",) * len(_KG),
        "read",
        "read",
    )
    assert tuple(tool.connection_identity_label for tool in analyst_ops.capabilities.tools) == (
        *(_LOCAL_T3_CONNECTION,) * len(_KG),
        "m365:meridian-servicing",
        "m365:meridian-servicing",
    )
    # TWO connectors now, and the second is the point of the grant: the Tier-3
    # release the read.kg tools read is a distinct governed connection from the
    # M365 corpus, and the projection shows both rather than folding them.
    assert tuple(
        (connector.connection_identity_label, connector.source_id, connector.tiers)
        for connector in analyst_ops.capabilities.connectors
    ) == (
        (_LOCAL_T3_CONNECTION, _LOCAL_SOURCE, ("t3",)),
        ("m365:meridian-servicing", _SOURCE, ("t1", "t2")),
    )

    analyst = asyncio.run(preflight.preflight(agent_id=_MERIDIAN_AGENT_ID, principal=frontline))
    assert analyst is not None and analyst.available is True
    assert analyst.capabilities is not None
    # THE GRANT DID NOT WIDEN THE PERSONA GATE. `frontline` sees the SAME three
    # tools it saw before the verified-answer lane was granted: the read.kg tools
    # are group-scoped to `ops` by the local-T3 workflow tools' own visibility,
    # and an Agent-level grant does not override a PDP decision. This assertion
    # is the discriminating half — granting the lane to the Agent while leaving
    # this tuple unchanged is exactly the property the grant had to preserve.
    assert tuple(tool.name for tool in analyst.capabilities.tools) == (
        GRAPH_PATH_TOOL_NAME,
        TOOL_NAME,
    )
    assert all(tool.read_write_class == "read" for tool in analyst.capabilities.tools)
    assert tuple(tool.connection_identity_label for tool in analyst.capabilities.tools) == (
        "m365:meridian-servicing",
        "m365:meridian-servicing",
    )
    assert len(analyst.capabilities.connectors) == 1
    assert analyst.capabilities.connectors[0].connection_identity_label == (
        "m365:meridian-servicing"
    )
    assert analyst.capabilities.connectors[0].source_id == _SOURCE
    # The read surface must NOT collapse: the two personas differ by exactly the
    # draft action AND the group-scoped verified-answer lane, so neither "hide it
    # from everyone" nor "show it to everyone" satisfies this pair. The lane is on
    # the ops side of that line, which is where the local-T3 tools' own `ops`
    # visibility puts it — the Agent grant did not move it.
    assert {tool.name for tool in analyst_ops.capabilities.tools} - {
        tool.name for tool in analyst.capabilities.tools
    } == {*_KG}
    engine.dispose()


def test_local_only_production_preflight_keeps_workflows_available_without_m365() -> None:
    loaded = load_agent_manifest(_REGISTRY)
    profiles, bindings = _compose_workflow_execution_bindings(loaded)
    local_sources = frozenset({(_LOCAL_SOURCE, "t3")})
    all_tools = frozenset(
        {
            MERIDIAN_CUSTOMERS_TOOL,
            MERIDIAN_LOANS_TOOL,
            MERIDIAN_PAYMENTS_TOOL,
            MERIDIAN_NOTES_TOOL,
            MERIDIAN_DISPUTES_DIRECTORY_TOOL,
            MERIDIAN_OPEN_DISPUTES_TOOL,
            MERIDIAN_OVERDUE_CREDIT_TOOL,
        }
    )
    dependencies = _production_agent_dependency_snapshot(
        registered_tools=all_tools,
        knowledge_sources=local_sources,
        skill_pins=frozenset(),
        workflow_profiles=profiles,
        model_policies=frozenset(
            definition.model_policy_id for definition in loaded.manifest.agents
        ),
        agent_execution_bindings=bindings,
        agent_transport_ready=True,
    )
    engine = sa.create_engine("sqlite://")
    catalog = reconcile_agent_catalog(engine, loaded, dependencies)
    effective_grants = {
        row.definition.agent_id: effective_agent_tool_grants(row.definition, dependencies)
        for row in catalog.agents
    }
    metadata = _production_agent_capability_metadata(
        registered_tools=all_tools,
        admitted_sources=local_sources,
        source_authority=None,
        governed_local_t3_source_authority=_local_t3_authority(),
        skill_ready=False,
        graph_host_bound=False,
    )
    assert metadata is not None
    preflight = AgentCapabilityPreflightService(
        agent_catalog=catalog,
        inventory_provider=_ComposedAgentCapabilityInventory(
            definitions={row.definition.agent_id: row.definition for row in catalog.agents},
            tool_specs=tuple(
                ToolSpec(name=name, description=f"Registered {name}", tier=ApprovalTier.TIER_0)
                for name in sorted(all_tools)
            ),
            metadata=metadata,
            config_hash="c" * 64,
            effective_tool_grants=effective_grants,
        ),
    )
    ops = Principal(id="user:ops", groups=("ops",))
    for agent_id in (
        "agt_01k3hardshipreview0000000000",
        "agt_01k3currentsopreview000000000",
        "agt_01k3fabricationrefusal000000",
        "agt_01k3overduecreditreviewdet00",
    ):
        result = asyncio.run(preflight.preflight(agent_id=agent_id, principal=ops))
        assert result is not None and result.available is True
        assert result.capabilities is not None
        assert {row.connection_identity_label for row in result.capabilities.tools} == {
            _LOCAL_T3_CONNECTION
        }
        assert tuple(row.source_id for row in result.capabilities.connectors) == (_LOCAL_SOURCE,)
    engine.dispose()


def test_manifest_bounds_and_readiness_gate_the_executable_dependency_snapshot() -> None:
    loaded = load_agent_manifest(_REGISTRY)
    profiles, bindings = _compose_workflow_execution_bindings(loaded)
    definition = next(
        item for item in loaded.manifest.agents if item.agent_id == _MERIDIAN_AGENT_ID
    )
    binding = bindings[_MERIDIAN_AGENT_ID]
    adapter = binding.agentic_adapter
    assert adapter is not None
    assert adapter.max_tool_calls == 34
    # `max_input_tokens + max_output_tokens`, re-derived for the 75-request
    # ceiling NE-MULTIHOP-FANOUT's 34 tool calls plus the output-retry pool
    # produce (see deploy/agents/registry.toml).
    # NE-CS-SPLIT re-derived the budget DOWN for the smaller prompt (S 3132 ->
    # 2916), then NE-ANALYST-INSTRUCTIONS re-derived it UP for the zero-pin
    # Analyst's own manifest instructions (S 2916 -> 3090), and
    # NE-ANSWER-THE-QUESTION + NE-GRAPH-ANCHOR further UP for the grown
    # answer-transport tail and catalog entries (S 3090 -> 3358; see
    # deploy/agents/registry.toml), and the read-only Analyst has zero
    # non-read grants, so it may park zero writes. NE-SHAPE-ALLOWANCE-4 then
    # re-derived it UP once more for the sixth output retry the measured live run
    # needed (1256283 -> 1331813), and NE-WRAPPED-CALL-FUNDED again for the
    # ladder-sized `wrapped-call` class, which makes the request ceiling
    # `2*max_tool_calls + 7` = 75 (1331813 -> 3666637): the retry pool is a
    # budget input in exactly the way prompt text is.
    assert adapter.total_tokens_limit == 3_666_637
    assert adapter.wall_clock_seconds == 400.0
    assert adapter.max_parked_writes == 0

    tools = frozenset(definition.tool_grants)
    sources = frozenset((_SOURCE, tier) for tier in ("t1", "t2"))
    pins = frozenset(
        (grant.skill_id, grant.version, grant.content_hash) for grant in definition.skills
    )
    complete = _production_agent_dependency_snapshot(
        registered_tools=tools,
        knowledge_sources=sources,
        skill_pins=pins,
        workflow_profiles=profiles,
        model_policies=frozenset({definition.model_policy_id}),
        agent_execution_bindings=bindings,
        agent_transport_ready=True,
    )
    assert definition.model_policy_id == "agent.meridian-analyst.v2"
    assert complete.tools == tools
    assert complete.knowledge_sources == sources
    assert complete.skills == pins
    assert complete.model_policies == {definition.model_policy_id}

    for change in (
        {"agent_transport_ready": False},
        {"agent_execution_bindings": {}},
    ):
        values = {
            "registered_tools": tools,
            "knowledge_sources": sources,
            "skill_pins": pins,
            "workflow_profiles": profiles,
            "model_policies": frozenset({definition.model_policy_id}),
            "agent_execution_bindings": bindings,
            "agent_transport_ready": True,
        }
        values.update(change)
        unavailable = _production_agent_dependency_snapshot(**values)
        assert unavailable.tools == frozenset()
        assert unavailable.knowledge_sources == sources
        assert unavailable.skills == pins
        assert unavailable.model_policies == {definition.model_policy_id}


def test_protected_run_skill_view_never_falls_back_to_the_current_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ne.api.tools as tools_module
    from ne.api.agent_skills import run_skill_pins
    from ne.contracts.skills import SkillPack, SkillSource
    from ne.contracts.types import Principal
    from ne.skills.config import SkillsSettings
    from ne.skills.errors import SkillNotFoundError
    from ne.skills.registry import SkillPin, SqlAlchemySkillRegistry, ensure_skills_schema

    class _Expander:
        def expand(self, principal: Principal | str) -> frozenset[str]:
            return frozenset()

    engine = sa.create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=sa.pool.StaticPool,
    )
    ensure_skills_schema(engine)
    pack = SkillPack(
        name="run-only-skill",
        version=1,
        description="A bounded Run-only skill",
        triggers=("run only",),
        body=(
            "## When to Use\nFor this test.\n"
            "## Procedure\n1. Stay pinned.\n"
            "## Pitfalls\nDo not fall back.\n"
            "## Verification\nConfirm the pin.\n"
        ),
        source=SkillSource.BUNDLED,
    )
    SqlAlchemySkillRegistry(engine).register_pack(pack, origin="bundle")
    principal = Principal(id="alice")
    monkeypatch.setattr(tools_module, "current_tool_principal", lambda: principal)
    _search, view = tools_module.build_skill_bindings(
        engine,
        _Expander(),
        SkillsSettings(),
        run_pinned=True,
    )

    async def view_skill() -> str:
        return await view.handler({"name": pack.name})

    with pytest.raises(SkillNotFoundError):
        asyncio.run(view_skill())
    pin = SkillPin(name=pack.name, version=pack.version, content_hash=pack.content_hash)
    with run_skill_pins({pack.name: pin}):
        rendered = asyncio.run(view_skill())
    assert "Stay pinned" in rendered
    engine.dispose()


def test_salesforce_crm_tools_project_with_salesforce_identity_only_when_registered() -> None:
    """NE-SF-STAGING: a staged Salesforce leg makes ALL THREE CRM grants eligible.

    Before this unit only the contact lookup carried a `_MERIDIAN_TOOL_LABELS`
    entry, so a fully staged deployment still projected the note write and the
    account lookup as "granted, not eligible" while the executor ran them fine.
    The three tools bind only when the connected-app leg composed, so their
    registration is the staging proof — and their connection identity must name
    Salesforce, never the M365 servicing label of the shared else-branch.
    """
    crm_tools = frozenset(
        {"read.external.crm.account", "read.external.crm.contact", "write.internal.crm.note"}
    )
    base = frozenset({VIEW_TOOL_NAME, TOOL_NAME, _EMAIL_DRAFT_TOOL})

    staged = _production_agent_capability_metadata(
        registered_tools=base | crm_tools,
        admitted_sources=frozenset({(_SOURCE, "t1")}),
        source_authority=_authority(),
        governed_local_t3_source_authority=None,
        skill_ready=True,
        graph_host_bound=False,
    )
    assert staged is not None
    by_name = {binding.tool_name: binding for binding in staged.tool_bindings}
    assert crm_tools <= set(by_name)
    assert {by_name[name].connection_identity_label for name in crm_tools} == {
        "internal:crm-mirror"
    }
    assert by_name["write.internal.crm.note"].read_write_class == "write"
    assert by_name["read.external.crm.account"].read_write_class == "read"
    assert by_name["read.external.crm.contact"].read_write_class == "read"
    # No connector runtime-health signal exists for the connected app; absence
    # from tool_health projects UNKNOWN — construction is never laundered green.
    assert not (crm_tools & set(staged.tool_health))
    # Same static-visibility posture as the contact-lookup precedent: the
    # registry's ops-only Agent visibility is the narrowing, not the tool row.
    assert all(staged.static_tool_visibility[name].all_authenticated for name in crm_tools)

    unstaged = _production_agent_capability_metadata(
        registered_tools=base,
        admitted_sources=frozenset({(_SOURCE, "t1")}),
        source_authority=_authority(),
        governed_local_t3_source_authority=None,
        skill_ready=True,
        graph_host_bound=False,
    )
    assert unstaged is not None
    assert not (crm_tools & {binding.tool_name for binding in unstaged.tool_bindings})
