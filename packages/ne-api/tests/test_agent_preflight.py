# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""PRE-011E principal-scoped Agent capability preflight: wire, order and hash.

The REAL ``AgentCatalog`` over the REAL mounted ``deploy/agents/registry.toml``
definitions, the REAL PRE-011A
:func:`~ne.runtime.agent_capabilities.project_agent_capabilities`, the REAL wire
projection and the REAL FastAPI route all run here.  Only the composition inputs
011A is entitled to be HANDED -- registered Tool specs, safe display bindings,
Agent grants, static visibility and composed health -- are fixtures, because a
composition root that supplies them is PRE-011E's successor unit, not this one.

Where a test needs a row ORDER the 011A projector would never emit (it sorts),
it injects a hand-built inventory through the same provider seam and says so in
its name.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ne.api.agent_catalog import AgentCatalog, CatalogAgent
from ne.api.agent_manifest import AgentManifestDefinition, load_agent_manifest
from ne.api.agent_model_preflight import AgentModelPreflight
from ne.api.agent_preflight import (
    AgentCapabilityPreflightService,
    AgentConnectorCapability,
    AgentSkillDetail,
    AgentToolCapability,
    _capability_hash,
)
from ne.api.deps import current_principal
from ne.api.model_selection import ModelSelectionEvidenceError
from ne.api.routers import agents
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

from .conftest import run

_ROOT = Path(__file__).parents[3]
_REGISTRY = _ROOT / "deploy/agents/registry.toml"

ANALYST_ID = "agt_01k3meridiananalyst000000000"
HARDSHIP_ID = "agt_01k3hardshipreview0000000000"
UNKNOWN_ID = "agt_01k3nosuchagentanywhere00000"

#: Meridian Hardship Review is group-scoped to ``ops``; the Analyst is visible
#: to every authenticated principal.  That pair is the natural visibility axis.
OPS = Principal(id="user:ops", groups=("ops",))
STAFF = Principal(id="user:staff", groups=("users",))

SEARCH = "read.knowledge.search"
DRAFT = "draft.email.create"
CONNECTION = "conn:meridian-core"
SOURCE = "meridian-servicing"

#: The complete field set of each wire object.  Exact-equality over the key set
#: beats a disjointness check over a guessed deny-list: a new "evidence_locator",
#: "run_id" or "refusal_reason" field fails here however it is spelled.
PREFLIGHT_FIELDS = {
    "agent_id",
    "version",
    "definition_hash",
    "available",
    "availability_reasons",
    "capabilities",
    # NE-CAPTAB-SKILL: additive principal-scoped pinned-Skill detail. `None` when
    # no Skill detail producer is composed (the default in these tests).
    "skills",
    "model",
}
MODEL_PREFLIGHT_FIELDS = {
    "task",
    "allowed_strategies",
    "default_strategy",
    "max_steps",
    "max_tool_calls",
    "max_input_tokens",
    "max_output_tokens",
    "candidates",
    "default_model",
}
CAPABILITY_SET_FIELDS = {
    "agent_version",
    "agent_definition_hash",
    "tools",
    "connectors",
    "capability_hash",
}
TOOL_FIELDS = {
    "name",
    "label",
    "read_write_class",
    "declared_tier",
    "effective_tier",
    "connection_identity_label",
    "health",
    # NE-CAPTAB: the tool's sanitized advertised contract rides on the wire —
    # the identical metadata the in-band model catalog already ships.
    "description",
    "arguments",
    "answer_columns",
}
CONNECTOR_FIELDS = {
    "connection_identity_label",
    "label",
    "kind",
    "credential_owner_label",
    "health",
    "source_id",
    "tiers",
}


# --- real registry definitions -------------------------------------------------------


def _definitions() -> dict[str, AgentManifestDefinition]:
    loaded = load_agent_manifest(_REGISTRY)
    return {definition.agent_id: definition for definition in loaded.manifest.agents}


def _catalog(
    *definitions: AgentManifestDefinition,
    available: bool = False,
    availability_reasons: tuple[str, ...] = ("tool-unavailable",),
) -> AgentCatalog:
    """Availability is a PARAMETER: the boot composition ships it ``False``."""
    return AgentCatalog(
        tuple(
            CatalogAgent(
                definition=definition,
                available=available,
                availability_reasons=availability_reasons,
                present=True,
            )
            for definition in definitions
        )
    )


# --- the injected 011A seam ----------------------------------------------------------


@dataclass
class RealInventoryProvider:
    """The REAL 011A projector; only its composition inputs are fixtures."""

    tool_grants: tuple[AgentToolGrant, ...]
    source_grants: tuple[AgentSourceGrant, ...]
    specs: tuple[ToolSpec, ...]
    tool_bindings: tuple[ToolCapabilityBinding, ...]
    connector_bindings: tuple[ConnectorCapabilityBinding, ...]
    static_tool_visibility: dict[str, StaticCapabilityVisibility]
    static_connector_visibility: dict[str, StaticCapabilityVisibility]
    composed_tool_health: dict[str, bool | None]
    calls: int = 0

    async def __call__(
        self,
        *,
        agent_id: str,
        agent_version: int,
        agent_definition_hash: str,
        principal: Principal,
    ) -> AgentCapabilityInventory | None:
        self.calls += 1
        return project_agent_capabilities(
            agent_id=agent_id,
            agent_version=agent_version,
            agent_definition_hash=agent_definition_hash,
            principal=principal,
            registered_specs=self.specs,
            tool_bindings=self.tool_bindings,
            agent_tool_grants=self.tool_grants,
            static_tool_visibility=self.static_tool_visibility,
            composed_tool_health=self.composed_tool_health,
            connector_bindings=self.connector_bindings,
            agent_source_grants=self.source_grants,
            static_connector_visibility=self.static_connector_visibility,
        )


@dataclass
class ScriptedInventoryProvider:
    """Returns an exact inventory (or ``None``) without consulting 011A."""

    inventory: AgentCapabilityInventory | None = None
    calls: int = 0

    async def __call__(self, **_kwargs: Any) -> AgentCapabilityInventory | None:
        self.calls += 1
        return self.inventory


@dataclass
class TripwireInventoryProvider:
    """Fails loudly if a refusal that must precede it reaches the producer."""

    calls: int = 0

    async def __call__(self, **_kwargs: Any) -> AgentCapabilityInventory | None:
        self.calls += 1
        raise AssertionError("a refused preflight must not consult the inventory")


@dataclass
class RaisingInventoryProvider:
    """A provider that fails; failing is not the same as answering 'unknown'."""

    error: Exception = field(default_factory=lambda: RuntimeError("inventory read failed"))

    async def __call__(self, **_kwargs: Any) -> AgentCapabilityInventory | None:
        raise self.error


@dataclass
class ScriptedModelPreflightProvider:
    """HTTP-adapter edge; 011D's service semantics have their own focused suite."""

    definition: AgentManifestDefinition
    error: Exception | None = None
    calls: list[Principal] = field(default_factory=list)

    async def preflight(self, *, agent_id: str, principal: Principal) -> AgentModelPreflight | None:
        self.calls.append(principal)
        if self.error is not None:
            raise self.error
        if agent_id != self.definition.agent_id:
            return None
        budgets = self.definition.budgets
        return AgentModelPreflight(
            agent_id=self.definition.agent_id,
            agent_version=self.definition.version,
            definition_hash=self.definition.definition_hash,
            available=False,
            availability_reasons=("tool-unavailable",),
            task=self.definition.model_policy_id,
            allowed_strategies=tuple(self.definition.allowed_strategies),
            default_strategy=self.definition.default_strategy,
            max_steps=budgets.max_steps,
            max_tool_calls=budgets.max_tool_calls,
            max_input_tokens=budgets.max_input_tokens,
            max_output_tokens=budgets.max_output_tokens,
            candidates=(),
            default_model=None,
        )


def _real_provider(definition: AgentManifestDefinition) -> RealInventoryProvider:
    """Composition inputs where the two Tools differ in visibility AND health.

    ``read.knowledge.search`` is visible to everyone and composed healthy;
    ``draft.email.create`` is ``ops``-only and has NO composed health row, which
    is how a missing fact is meant to reach the surface.
    """
    identity = {
        "agent_id": definition.agent_id,
        "agent_version": definition.version,
        "agent_definition_hash": definition.definition_hash,
    }
    return RealInventoryProvider(
        tool_grants=(
            AgentToolGrant(**identity, tool_name=SEARCH),
            AgentToolGrant(**identity, tool_name=DRAFT),
        ),
        source_grants=(AgentSourceGrant(**identity, source_id=SOURCE, tiers=("t1", "t2")),),
        specs=(
            ToolSpec(name=SEARCH, description="Search governed knowledge.", tier=ApprovalTier(0)),
            ToolSpec(name=DRAFT, description="Draft a customer email.", tier=ApprovalTier(0)),
        ),
        tool_bindings=(
            ToolCapabilityBinding(
                tool_name=SEARCH,
                safe_label="Knowledge Search",
                read_write_class="read",
                connection_identity_label=CONNECTION,
            ),
            ToolCapabilityBinding(
                tool_name=DRAFT,
                safe_label="Email Draft",
                read_write_class="write",
                connection_identity_label=CONNECTION,
            ),
        ),
        connector_bindings=(
            ConnectorCapabilityBinding(
                source_id=SOURCE,
                connection_identity_label=CONNECTION,
                safe_label="Meridian Servicing Core",
                kind="postgres",
                sanitized_credential_owner_label="Meridian Servicing Operations",
                composed_tiers=("t1", "t2"),
                composed_health=True,
            ),
        ),
        static_tool_visibility={
            SEARCH: StaticCapabilityVisibility(all_authenticated=True),
            DRAFT: StaticCapabilityVisibility(groups=("ops",)),
        },
        static_connector_visibility={
            CONNECTION: StaticCapabilityVisibility(all_authenticated=True)
        },
        composed_tool_health={SEARCH: True},
    )


# --- the app under test --------------------------------------------------------------


@dataclass
class Harness:
    client: TestClient
    provider: Any
    model_provider: ScriptedModelPreflightProvider
    definition: AgentManifestDefinition

    def get(self, agent_id: str, *, capability_hash: str | None = None) -> Any:
        params = {} if capability_hash is None else {"capability_hash": capability_hash}
        return self.client.get(f"/v1/agents/{agent_id}/preflight", params=params)


def _harness(
    principal: Principal,
    *,
    provider: Any | None = None,
    definitions: Mapping[str, AgentManifestDefinition] | None = None,
    model_error: Exception | None = None,
    skill_detail_provider: Any | None = None,
) -> Harness:
    loaded = _definitions() if definitions is None else dict(definitions)
    definition = loaded[ANALYST_ID]
    inventory_provider = _real_provider(definition) if provider is None else provider
    app = FastAPI()
    app.include_router(agents.router)
    app.dependency_overrides[current_principal] = lambda: principal
    model_provider = ScriptedModelPreflightProvider(definition, error=model_error)
    app.dependency_overrides[agents.agent_model_preflight_service] = lambda: model_provider
    setattr(
        app.state,
        agents.CAPABILITY_PREFLIGHT_STATE_KEY,
        AgentCapabilityPreflightService(
            agent_catalog=_catalog(*loaded.values()),
            inventory_provider=inventory_provider,
            skill_detail_provider=skill_detail_provider,
        ),
    )
    return Harness(
        client=TestClient(app),
        provider=inventory_provider,
        model_provider=model_provider,
        definition=definition,
    )


def _ok(harness: Harness, agent_id: str, *, capability_hash: str | None = None) -> Any:
    response = harness.get(agent_id, capability_hash=capability_hash)
    assert response.status_code == 200, response.text
    return response.json()


def _tool_names(body: Any) -> list[str]:
    return [tool["name"] for tool in body["capabilities"]["tools"]]


def _inventory(
    definition: AgentManifestDefinition,
    *tool_names: str,
    connectors: tuple[str, ...] = (),
    definition_hash: str | None = None,
    agent_id: str | None = None,
    version: int | None = None,
) -> AgentCapabilityInventory:
    """A hand-built inventory in the EXACT order given, sorted or not.

    Each of the three identity fields can be overridden ON ITS OWN, which is what
    lets the identity control below vary exactly one at a time.
    """
    agent_hash = definition.definition_hash if definition_hash is None else definition_hash
    row_id = definition.agent_id if agent_id is None else agent_id
    row_version = definition.version if version is None else version
    identity = {
        "agent_id": row_id,
        "agent_version": row_version,
        "agent_definition_hash": agent_hash,
    }
    return AgentCapabilityInventory(
        agent_id=row_id,
        agent_version=row_version,
        agent_definition_hash=agent_hash,
        tools=tuple(
            EligibleToolCapability(
                name=name,
                label=f"label:{name}",
                read_write_class="read",
                declared_tier=ApprovalTier(0),
                effective_tier=ApprovalTier(0),
                connection_identity_label=CONNECTION,
                health="unknown",
                governing_grant=AgentToolGrant(**identity, tool_name=name),
            )
            for name in tool_names
        ),
        connectors=tuple(
            EligibleConnectorCapability(
                connection_identity_label=label,
                label=f"label:{label}",
                kind="postgres",
                credential_owner_label="Meridian Servicing Operations",
                health="unknown",
                governing_grant=EligibleSourceGrantMetadata(
                    **identity, source_id=f"src:{label}", tiers=("t1",)
                ),
            )
            for label in connectors
        ),
    )


# --- 1. the projected surface --------------------------------------------------------


def test_preflight_carries_the_registry_identity_and_the_exact_eligible_rows() -> None:
    harness = _harness(OPS)
    body = _ok(harness, ANALYST_ID)

    assert set(body) == PREFLIGHT_FIELDS
    assert (body["agent_id"], body["version"], body["definition_hash"]) == (
        harness.definition.agent_id,
        harness.definition.version,
        harness.definition.definition_hash,
    )
    # Copied from the catalog row, not re-decided here.
    assert (body["available"], body["availability_reasons"]) == (False, ["tool-unavailable"])

    model = body["model"]
    assert set(model) == MODEL_PREFLIGHT_FIELDS
    assert model == {
        "task": harness.definition.model_policy_id,
        "allowed_strategies": list(harness.definition.allowed_strategies),
        "default_strategy": harness.definition.default_strategy,
        "max_steps": harness.definition.budgets.max_steps,
        "max_tool_calls": harness.definition.budgets.max_tool_calls,
        "max_input_tokens": harness.definition.budgets.max_input_tokens,
        "max_output_tokens": harness.definition.budgets.max_output_tokens,
        "candidates": [],
        "default_model": None,
    }
    assert harness.model_provider.calls == [OPS]

    capabilities = body["capabilities"]
    assert set(capabilities) == CAPABILITY_SET_FIELDS
    assert (capabilities["agent_version"], capabilities["agent_definition_hash"]) == (
        harness.definition.version,
        harness.definition.definition_hash,
    )
    assert [set(tool) for tool in capabilities["tools"]] == [TOOL_FIELDS, TOOL_FIELDS]
    assert capabilities["tools"] == [
        {
            "name": DRAFT,
            "label": "Email Draft",
            "read_write_class": "write",
            "declared_tier": 0,
            "effective_tier": 0,
            "connection_identity_label": CONNECTION,
            "health": "unknown",
            "description": "Draft a customer email.",
            "arguments": None,
            "answer_columns": None,
        },
        {
            "name": SEARCH,
            "label": "Knowledge Search",
            "read_write_class": "read",
            "declared_tier": 0,
            "effective_tier": 0,
            "connection_identity_label": CONNECTION,
            "health": "healthy",
            "description": "Search governed knowledge.",
            "arguments": None,
            "answer_columns": None,
        },
    ]
    assert [set(row) for row in capabilities["connectors"]] == [CONNECTOR_FIELDS]
    assert capabilities["connectors"] == [
        {
            "connection_identity_label": CONNECTION,
            "label": "Meridian Servicing Core",
            "kind": "postgres",
            "credential_owner_label": "Meridian Servicing Operations",
            "health": "healthy",
            "source_id": SOURCE,
            "tiers": ["t1", "t2"],
        }
    ]
    assert len(capabilities["capability_hash"]) == 64


def test_a_served_preflight_is_private_no_store() -> None:
    """The 200 specifically: every refusal carries the header too, so asserting
    it without pinning the status would pass against a route that only 404s."""
    response = _harness(OPS).get(ANALYST_ID)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"


def test_model_evidence_failure_returns_no_partial_capability_response() -> None:
    response = _harness(
        OPS,
        model_error=ModelSelectionEvidenceError("private serving details must stay hidden"),
    ).get(ANALYST_ID)

    assert response.status_code == 503
    assert response.json() == {"detail": "Agent preflight is unavailable"}
    assert response.headers["cache-control"] == "private, no-store"
    assert b"capabilities" not in response.content
    assert b"private serving details" not in response.content


def test_a_tool_with_no_composed_health_row_reports_unknown_rather_than_healthy() -> None:
    harness = _harness(OPS)
    healthy = _ok(harness, ANALYST_ID)["capabilities"]["tools"]
    assert {tool["name"]: tool["health"] for tool in healthy} == {
        DRAFT: "unknown",
        SEARCH: "healthy",
    }

    harness.provider.composed_tool_health[SEARCH] = False
    flipped = _ok(harness, ANALYST_ID)["capabilities"]["tools"]
    assert {tool["name"]: tool["health"] for tool in flipped} == {
        DRAFT: "unknown",
        SEARCH: "unhealthy",
    }


# --- 2. no existence oracle ----------------------------------------------------------


def test_hidden_and_unknown_agents_are_byte_identical_404s_before_the_inventory_is_read() -> None:
    harness = _harness(STAFF, provider=TripwireInventoryProvider())
    hidden = harness.get(HARDSHIP_ID)
    unknown = harness.get(UNKNOWN_ID)

    assert hidden.status_code == 404
    assert hidden.json() == {"detail": "Agent not found"}
    assert (hidden.status_code, hidden.content, hidden.headers["cache-control"]) == (
        unknown.status_code,
        unknown.content,
        unknown.headers["cache-control"],
    )
    assert harness.provider.calls == 0


# --- 3. principal scoping ------------------------------------------------------------


def test_ops_and_staff_see_different_tools_and_different_capability_hashes_on_one_agent() -> None:
    ops = _ok(_harness(OPS), ANALYST_ID)
    staff = _ok(_harness(STAFF), ANALYST_ID)

    assert _tool_names(ops) == [DRAFT, SEARCH]
    assert _tool_names(staff) == [SEARCH]
    assert ops["capabilities"]["capability_hash"] != staff["capabilities"]["capability_hash"]
    # Same Agent definition on both sides: the hash moved because the SURFACE
    # differs, not because a different Agent was read.
    assert ops["definition_hash"] == staff["definition_hash"]


def test_model_preflight_uses_the_authenticated_principal_not_query_text() -> None:
    harness = _harness(STAFF)

    response = harness.client.get(
        f"/v1/agents/{ANALYST_ID}/preflight",
        params={"principal": OPS.id, "principal_id": OPS.id},
    )

    assert response.status_code == 200, response.text
    assert harness.model_provider.calls == [STAFF]


def test_a_capability_hash_earned_on_a_different_surface_is_refused() -> None:
    ops_hash = _ok(_harness(OPS), ANALYST_ID)["capabilities"]["capability_hash"]
    staff = _harness(STAFF)
    # NOT a claim that the digest is principal-BOUND -- it carries no principal
    # identity, and two principals with the same surface legitimately share one.
    # What is refused here is a hash earned on a DIFFERENT surface.

    assert staff.get(ANALYST_ID, capability_hash=ops_hash).status_code == 404
    # The control: staff's OWN hash is served, so the refusal above is the
    # principal binding and not a route that refuses every hash.
    own = _ok(staff, ANALYST_ID)["capabilities"]["capability_hash"]
    assert _ok(staff, ANALYST_ID, capability_hash=own)["capabilities"]["capability_hash"] == own


# --- 4. determinism and order --------------------------------------------------------


def test_repeated_reads_of_an_unchanged_surface_return_byte_identical_bodies() -> None:
    harness = _harness(OPS)
    first = harness.get(ANALYST_ID)
    second = harness.get(ANALYST_ID)
    assert first.content == second.content


def test_projection_preserves_the_producers_row_order_instead_of_sorting_it() -> None:
    definition = _definitions()[ANALYST_ID]
    unsorted_tools = ("zeta.read", "alpha.read", "mu.read")
    unsorted_connectors = ("conn:zulu", "conn:alfa", "conn:mike")
    harness = _harness(
        OPS,
        provider=ScriptedInventoryProvider(
            _inventory(definition, *unsorted_tools, connectors=unsorted_connectors)
        ),
    )
    body = _ok(harness, ANALYST_ID)

    assert _tool_names(body) == list(unsorted_tools)
    assert _tool_names(body) != sorted(unsorted_tools)
    connectors = [row["connection_identity_label"] for row in body["capabilities"]["connectors"]]
    assert connectors == list(unsorted_connectors)
    assert connectors != sorted(unsorted_connectors)


def test_reordering_the_same_rows_moves_the_capability_hash() -> None:
    definition = _definitions()[ANALYST_ID]
    provider = ScriptedInventoryProvider(_inventory(definition, "zeta.read", "alpha.read"))
    harness = _harness(OPS, provider=provider)
    first = _ok(harness, ANALYST_ID)["capabilities"]["capability_hash"]

    provider.inventory = _inventory(definition, "alpha.read", "zeta.read")
    assert _ok(harness, ANALYST_ID)["capabilities"]["capability_hash"] != first


# --- 5. the current-hash recomputation seam ------------------------------------------


def test_a_hash_stale_by_one_health_flip_is_refused_while_the_recomputed_one_is_served() -> None:
    harness = _harness(OPS)
    presented = _ok(harness, ANALYST_ID)["capabilities"]["capability_hash"]
    assert _ok(harness, ANALYST_ID, capability_hash=presented)["capabilities"]["tools"]

    harness.provider.composed_tool_health[SEARCH] = False
    assert harness.get(ANALYST_ID, capability_hash=presented).status_code == 404

    current = _ok(harness, ANALYST_ID)["capabilities"]["capability_hash"]
    assert current != presented
    assert _ok(harness, ANALYST_ID, capability_hash=current)["capabilities"]["capability_hash"] == (
        current
    )


def test_a_hash_stale_by_one_revoked_tool_grant_is_refused() -> None:
    harness = _harness(OPS)
    presented = _ok(harness, ANALYST_ID)["capabilities"]["capability_hash"]

    harness.provider.tool_grants = harness.provider.tool_grants[:1]
    assert _tool_names(_ok(harness, ANALYST_ID)) == [SEARCH]
    assert harness.get(ANALYST_ID, capability_hash=presented).status_code == 404


def test_a_stale_hash_refusal_is_byte_identical_to_an_unknown_agent_refusal() -> None:
    harness = _harness(OPS)
    presented = _ok(harness, ANALYST_ID)["capabilities"]["capability_hash"]
    harness.provider.composed_tool_health[SEARCH] = False

    stale = harness.get(ANALYST_ID, capability_hash=presented)
    unknown = harness.get(UNKNOWN_ID)
    assert stale.status_code == 404
    assert (stale.status_code, stale.content, stale.headers["cache-control"]) == (
        unknown.status_code,
        unknown.content,
        unknown.headers["cache-control"],
    )


# --- 6. missing facts are unavailable, never invented --------------------------------


def test_an_unanswerable_inventory_is_a_null_capability_set_not_an_empty_one() -> None:
    definition = _definitions()[ANALYST_ID]
    unanswered = _ok(_harness(OPS, provider=ScriptedInventoryProvider(None)), ANALYST_ID)
    empty = _ok(
        _harness(OPS, provider=ScriptedInventoryProvider(_inventory(definition))), ANALYST_ID
    )

    assert unanswered["capabilities"] is None
    assert empty["capabilities"] is not None
    assert (empty["capabilities"]["tools"], empty["capabilities"]["connectors"]) == ([], [])
    assert len(empty["capabilities"]["capability_hash"]) == 64
    # Identical Agent facts on both sides, so the bodies differ ONLY in how the
    # two states are reported.
    assert unanswered != empty


def test_a_presented_hash_is_refused_when_the_inventory_cannot_be_answered() -> None:
    definition = _definitions()[ANALYST_ID]
    provider = ScriptedInventoryProvider(_inventory(definition, SEARCH))
    harness = _harness(OPS, provider=provider)
    presented = _ok(harness, ANALYST_ID)["capabilities"]["capability_hash"]

    provider.inventory = None
    assert harness.get(ANALYST_ID, capability_hash=presented).status_code == 404
    # Without a presented hash the same unanswerable read is still a 200 report.
    assert _ok(harness, ANALYST_ID)["capabilities"] is None


def test_an_inventory_differing_in_any_identity_field_is_not_relabelled_onto_this_agent() -> None:
    """Each of agent_id, version and definition_hash refuses ON ITS OWN.

    Checking the definition hash alone left the other two free, so a provider
    could answer with a foreign id at a foreign version while carrying the
    requested hash and be accepted -- and then have a capability hash minted over
    a foreign grant set. Each variant below changes exactly ONE field, so no
    single field can be dropped from the comparison without this going red.
    """
    definition = _definitions()[ANALYST_ID]
    variants = {
        "definition_hash": _inventory(definition, SEARCH, definition_hash="f" * 64),
        "agent_id": _inventory(definition, SEARCH, agent_id=UNKNOWN_ID),
        "version": _inventory(definition, SEARCH, version=definition.version + 98),
    }
    for field_name, inventory in variants.items():
        harness = _harness(OPS, provider=ScriptedInventoryProvider(inventory))
        body = _ok(harness, ANALYST_ID)
        assert body["definition_hash"] == definition.definition_hash, field_name
        assert body["capabilities"] is None, field_name

    # The control: the SAME projection with the identity left alone is served, so
    # the refusals above are the identity comparison and not a provider that can
    # never answer.
    matched = _harness(OPS, provider=ScriptedInventoryProvider(_inventory(definition, SEARCH)))
    assert _tool_names(_ok(matched, ANALYST_ID)) == [SEARCH]


def test_a_raising_inventory_provider_propagates_instead_of_reporting_no_capabilities() -> None:
    service = AgentCapabilityPreflightService(
        agent_catalog=_catalog(*_definitions().values()),
        inventory_provider=RaisingInventoryProvider(),
    )
    with pytest.raises(RuntimeError, match="inventory read failed"):
        run(service.preflight(agent_id=ANALYST_ID, principal=OPS))


# --- 7. the digest covers every field it projects -------------------------------------


#: One fully-populated row of each kind. Every field carries a distinct value so a
#: variant below cannot accidentally collide with the baseline through a shared one.
_HASH_TOOL = AgentToolCapability(
    name="read.knowledge.search",
    label="Knowledge Search",
    read_write_class="read",
    declared_tier=0,
    effective_tier=0,
    connection_identity_label="conn:core",
    health="healthy",
)
_HASH_CONNECTOR = AgentConnectorCapability(
    connection_identity_label="conn:core",
    label="Meridian Servicing Core",
    kind="postgres",
    credential_owner_label="Meridian Servicing Operations",
    health="unhealthy",
    source_id="meridian-servicing",
    tiers=("t1", "t2"),
)
_HASH_IDENTITY = {
    "agent_id": ANALYST_ID,
    "agent_version": 1,
    "agent_definition_hash": "a" * 64,
}


def test_every_projected_field_and_the_connector_block_move_the_capability_digest() -> None:
    """A field absent from the payload lets the surface change without the hash.

    The adversarial pass found the suite survived deleting ``tool.label``, all
    three identity fields and the entire connectors block from the digest — the
    behavioural controls elsewhere in this file never varied those in isolation.
    This varies exactly ONE thing per entry and requires every digest to be
    distinct, so dropping any one of them from the payload collapses two entries
    onto the same hash and fails here.
    """
    baseline = _capability_hash(
        **_HASH_IDENTITY, tools=(_HASH_TOOL,), connectors=(_HASH_CONNECTOR,)
    )

    variants: dict[str, str] = {}
    for key, changed in (
        ("agent_id", HARDSHIP_ID),
        ("agent_version", 2),
        ("agent_definition_hash", "b" * 64),
    ):
        variants[f"identity.{key}"] = _capability_hash(
            **{**_HASH_IDENTITY, key: changed},
            tools=(_HASH_TOOL,),
            connectors=(_HASH_CONNECTOR,),
        )
    for key, changed in (
        ("name", "read.knowledge.other"),
        ("label", "A Different Label"),
        ("read_write_class", "write"),
        ("declared_tier", 1),
        ("effective_tier", 1),
        ("connection_identity_label", "conn:other"),
        ("health", "unknown"),
    ):
        variants[f"tool.{key}"] = _capability_hash(
            **_HASH_IDENTITY,
            tools=(replace(_HASH_TOOL, **{key: changed}),),
            connectors=(_HASH_CONNECTOR,),
        )
    for key, changed in (
        ("connection_identity_label", "conn:other"),
        ("label", "A Different Connector"),
        ("kind", "sharepoint"),
        ("credential_owner_label", "Someone Else"),
        ("health", "healthy"),
        ("source_id", "another-source"),
        ("tiers", ("t1",)),
    ):
        variants[f"connector.{key}"] = _capability_hash(
            **_HASH_IDENTITY,
            tools=(_HASH_TOOL,),
            connectors=(replace(_HASH_CONNECTOR, **{key: changed}),),
        )
    # The whole block, not just a field inside it: a payload that omits
    # ``connectors`` entirely would keep every connector.* entry above equal to
    # the baseline, and this pins that too.
    variants["connectors.dropped"] = _capability_hash(
        **_HASH_IDENTITY, tools=(_HASH_TOOL,), connectors=()
    )
    variants["tools.dropped"] = _capability_hash(
        **_HASH_IDENTITY, tools=(), connectors=(_HASH_CONNECTOR,)
    )

    for name, digest in variants.items():
        assert digest != baseline, f"{name} does not reach the digest"
    # Pairwise distinct, so no two fields can be folded into one another either.
    assert len(set(variants.values())) == len(variants)

    # The control: recomputing the baseline inputs reproduces the baseline, so the
    # distinctness above is field sensitivity and not a digest that is simply
    # different every call.
    assert (
        _capability_hash(**_HASH_IDENTITY, tools=(_HASH_TOOL,), connectors=(_HASH_CONNECTOR,))
        == baseline
    )


# --- NE-CAPTAB-SKILL: additive principal-scoped pinned-Skill detail ------------------


def test_skills_are_absent_when_no_skill_detail_provider_is_composed() -> None:
    """The default: `skills` is None, the honest 'this deployment cannot answer'."""
    body = _ok(_harness(OPS), ANALYST_ID)
    assert body["skills"] is None


def test_skills_carry_the_pinned_pack_body_and_boundary_when_a_provider_answers() -> None:
    """A composed provider's exact projection reaches the wire, body included."""
    detail = AgentSkillDetail(
        skill_id="meridian-hardship-draft",
        version=1,
        content_hash="a" * 64,
        description="Draft a reviewable disaster-hardship outreach message.",
        requires_tools=("draft.email.create", "read.knowledge.search"),
        acl_labels=("department:servicing",),
        triggers=("draft disaster hardship outreach",),
        category="servicing",
        body="## Procedure\n1. Confirm the served evidence.",
    )

    async def _provider(*, agent_id: str, principal: Principal) -> tuple[AgentSkillDetail, ...]:
        assert principal == OPS
        return (detail,)

    body = _ok(_harness(OPS, skill_detail_provider=_provider), ANALYST_ID)
    assert body["skills"] == [
        {
            "skill_id": "meridian-hardship-draft",
            "version": 1,
            "content_hash": "a" * 64,
            "description": "Draft a reviewable disaster-hardship outreach message.",
            "requires_tools": ["draft.email.create", "read.knowledge.search"],
            "acl_labels": ["department:servicing"],
            "triggers": ["draft disaster hardship outreach"],
            "category": "servicing",
            "body": "## Procedure\n1. Confirm the served evidence.",
        }
    ]


def test_a_provider_answering_empty_is_distinct_from_absent() -> None:
    """`[]` = 'nothing visible to you', a DIFFERENT wire value from None."""

    async def _provider(*, agent_id: str, principal: Principal) -> tuple[AgentSkillDetail, ...]:
        return ()

    body = _ok(_harness(OPS, skill_detail_provider=_provider), ANALYST_ID)
    assert body["skills"] == []


def test_skill_detail_does_not_perturb_the_capability_hash() -> None:
    """Skills ride ALONGSIDE the capability set, never inside its hash."""

    async def _provider(*, agent_id: str, principal: Principal) -> tuple[AgentSkillDetail, ...]:
        return (
            AgentSkillDetail(
                skill_id="meridian-hardship-draft",
                version=1,
                content_hash="a" * 64,
                description="d",
                requires_tools=(),
                acl_labels=(),
                triggers=("t",),
                category="",
                body="b",
            ),
        )

    without = _ok(_harness(OPS), ANALYST_ID)
    withskills = _ok(_harness(OPS, skill_detail_provider=_provider), ANALYST_ID)
    assert (
        without["capabilities"]["capability_hash"] == withskills["capabilities"]["capability_hash"]
    )
