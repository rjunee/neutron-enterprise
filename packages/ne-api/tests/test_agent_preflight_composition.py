# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""PRE-011E production composition of the Agents capability preflight.

The REAL ``build_platform`` composition runs here, over the REAL mounted
``deploy/agents/registry.toml``, the REAL frozen governed executor catalog, the
REAL PRE-011A ``project_agent_capabilities`` and the REAL OIDC-mounted route.
Only the ordinary conftest edges (gateway, embedder, chunk store, token
verifier, OPA wire) are faked, and they are the SAME edges every other
composition test in this package uses.

The composed deployment deliberately has NO capability-metadata producer: the
safe labels, read/write classes, connection identities and static visibility
PRE-011A needs are authored by composition and this repository authors none of
them yet (``ToolSpec.description`` is a model-facing prompt string, and
``tier_for_action`` answers a TIER, not a read/write class -- ``draft.`` is tier
0 and is a write). So the honest production answer today is "capabilities
unavailable", and the discriminating half of this file composes a producer
explicitly and proves the real projector then answers over real composed truth.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import HTTPException
from starlette.routing import Mount

from ne.api.deps import get_agent_run_store
from ne.api.model_routing import action_for_lane
from ne.api.platform import (
    AgentCapabilityMetadata,
    Platform,
    _ComposedAgentCapabilityInventory,
    build_platform,
)
from ne.api.routers.agents import CAPABILITY_PREFLIGHT_STATE_KEY, MODEL_PREFLIGHT_STATE_KEY
from ne.contracts import reset_config_hash_for_testing
from ne.contracts.types import Lane, Principal
from ne.gateway.config import TaskRoute
from ne.runtime.agent_capabilities import StaticCapabilityVisibility, ToolCapabilityBinding

from .conftest import AppBuilder, get_state, run
from .test_model_selection import PRIVATE, PRIVATE_PEER, StubServingStatus, _row
from .test_skills_platform import _api_settings, _seams

_ROOT = Path(__file__).parents[3]
_REGISTRY = _ROOT / "deploy/agents/registry.toml"

ANALYST_ID = "agt_01k3meridiananalyst000000000"
#: Meridian Hardship Review is group-scoped to ``ops`` in the mounted registry.
HARDSHIP_ID = "agt_01k3hardshipreview0000000000"
UNKNOWN_ID = "agt_01k3nosuchagentanywhere00000"

#: The ONE tool the Analyst is granted that this composition actually registers.
#: The other grants are real and are deliberately NOT registered by the default
#: recipe (no graph tool, verified answers off), so the registered-specs
#: intersection is exercised by production truth rather than by a fixture that
#: arranges for it. NE-CS-SPLIT: `draft.email.create` and `read.skills.view`
#: moved to Meridian Customer Support (still granted-not-registered here — Skills
#: and write actions are off in this recipe).
GRANTED_AND_REGISTERED = "read.knowledge.search"
GRANTED_NOT_REGISTERED = ("draft.email.create", "read.knowledge.graph_path", "read.skills.view")
#: Registered by this composition, granted to NOBODY in the mounted registry.
REGISTERED_NOT_GRANTED = "read.trace"

OPS_GROUP = "ops"


def _metadata() -> AgentCapabilityMetadata:
    """A producer covering MORE tools than any Agent may end up eligible for.

    Deliberately over-broad: it authors metadata and visibility for every Analyst
    grant AND for a registered tool no Agent is granted, so a projection that
    leaked either the ungranted tool or an unregistered grant would show up as a
    row rather than as a silent absence.
    """
    names = (GRANTED_AND_REGISTERED, *GRANTED_NOT_REGISTERED, REGISTERED_NOT_GRANTED)
    return AgentCapabilityMetadata(
        tool_bindings=tuple(
            ToolCapabilityBinding(
                tool_name=name,
                safe_label=f"Label for {name}",
                read_write_class="read" if name.startswith("read.") else "write",
                connection_identity_label="conn:governed-core",
            )
            for name in names
        ),
        # ``ops``-only across the board, so one composed producer serves both the
        # eligible and the ineligible principal without a second recipe.
        static_tool_visibility={
            name: StaticCapabilityVisibility(groups=(OPS_GROUP,)) for name in names
        },
    )


@pytest.fixture
def composed(tmp_path: Path) -> Iterator[Platform]:
    """A platform composed WITH a capability-metadata producer."""
    reset_config_hash_for_testing()
    platform = build_platform(
        _api_settings(agent_manifest_path=str(_REGISTRY)),
        dataclasses.replace(
            _seams(tmp_path, key="preflight-composed"),
            agent_capability_metadata=_metadata(),
        ),
    )
    try:
        yield platform
    finally:
        for engine in platform.engines:
            engine.dispose()


def _inner(app: Any) -> Any:
    """The inner FastAPI app behind the ``/api`` OIDC mount -- what carries state."""
    for route in app.routes:
        if isinstance(route, Mount) and route.path == "/api":
            return cast(Any, route.app)._app
    raise AssertionError("no /api mount found")


def _preflight(platform: Platform, agent_id: str, principal: Principal) -> Any:
    service = platform.agent_preflight
    assert service is not None
    return run(service.preflight(agent_id=agent_id, principal=principal))


def _required(platform: Platform, agent_id: str, principal: Principal) -> Any:
    result = _preflight(platform, agent_id, principal)
    assert result is not None
    return result


# --- 1. the route is composed, not 503 ------------------------------------------------


def test_composed_app_serves_the_preflight_route_instead_of_the_not_configured_503(
    make_app: AppBuilder,
) -> None:
    harness = make_app(agent_manifest_path=str(_REGISTRY))
    ada = harness.bearer("u-ada")

    async def exercise() -> None:
        async with harness.client() as client:
            response = await client.get(f"/api/v1/agents/{ANALYST_ID}/preflight", headers=ada)
            assert response.status_code == 200, response.text
            assert response.headers["cache-control"] == "private, no-store"
            body = response.json()
            assert body["agent_id"] == ANALYST_ID
            assert body["version"] == 16
            # The catalog's own verdict, copied -- this composition satisfies
            # neither the Knowledge nor the Skill dependency.
            assert body["available"] is False
            assert "knowledge-unavailable" in body["availability_reasons"]
            # No capability-metadata producer is composed, so the honest answer is
            # "cannot answer" -- NOT an empty set of capabilities.
            assert body["capabilities"] is None
            # PRE-011D is a required half of this ONE response, even while the
            # deployment has no live candidates for the unrouted policy.
            assert body["model"] == {
                "task": "agent.meridian-analyst.v2",
                "allowed_strategies": ["agentic"],
                "default_strategy": "agentic",
                "max_steps": 40,
                "max_tool_calls": 34,
                "max_input_tokens": 3662637,
                "max_output_tokens": 4000,
                "candidates": [],
                "default_model": None,
            }

    run(exercise())


def test_the_typed_api_state_field_and_the_routers_app_state_key_are_one_composed_object(
    make_app: AppBuilder,
) -> None:
    """One value behind both accessors, so they cannot drift into disagreeing."""
    app = make_app(agent_manifest_path=str(_REGISTRY)).app
    state = get_state(app)
    assert state.agent_preflight is not None
    assert getattr(_inner(app).state, CAPABILITY_PREFLIGHT_STATE_KEY) is state.agent_preflight


def test_model_preflight_reuses_the_exact_catalog_selection_and_pdp_instances(
    make_app: AppBuilder,
) -> None:
    app = make_app(
        agent_manifest_path=str(_REGISTRY),
        gateway_models=(PRIVATE, PRIVATE_PEER),
        gateway_tasks={"agent.meridian-analyst.v2": TaskRoute(model=PRIVATE.name)},
    ).app
    state = get_state(app)
    service = getattr(_inner(app).state, MODEL_PREFLIGHT_STATE_KEY)

    assert state.model_selection is not None
    assert service._agent_catalog is state.agent_catalog
    assert service._selection is state.model_selection
    assert service._pdp is state.model_selection._pdp


def test_http_preflight_reads_the_live_route_store_instead_of_a_boot_route_snapshot(
    make_app: AppBuilder,
) -> None:
    observed_at = datetime.now(UTC)
    harness = make_app(
        agent_manifest_path=str(_REGISTRY),
        gateway_models=(PRIVATE, PRIVATE_PEER),
        gateway_tasks={"agent.meridian-analyst.v2": TaskRoute(model=PRIVATE.name)},
        serving_probe=StubServingStatus(
            (_row(checked_at=observed_at), _row(PRIVATE_PEER, checked_at=observed_at))
        ),
    )
    state = get_state(harness.app)
    ada = harness.bearer("u-ada")

    async def exercise() -> None:
        async with harness.client() as client:
            first = await client.get(f"/api/v1/agents/{ANALYST_ID}/preflight", headers=ada)
            assert first.status_code == 200, first.text
            assert first.json()["model"]["default_model"] == PRIVATE.name
            assert [row["model"] for row in first.json()["model"]["candidates"]] == [
                PRIVATE.name,
                PRIVATE_PEER.name,
            ]

            assert state.route_store is not None
            state.route_store.apply(
                task="agent.meridian-analyst.v2",
                model=PRIVATE_PEER.name,
                actor="user:operator",
                approver="user:approver",
                approval_id=None,
                via_action=action_for_lane(Lane.PRIVATE_VLLM),
            )

            second = await client.get(f"/api/v1/agents/{ANALYST_ID}/preflight", headers=ada)
            assert second.status_code == 200, second.text
            assert second.json()["model"]["default_model"] == PRIVATE_PEER.name

    run(exercise())


def test_production_model_evidence_failure_returns_no_partial_preflight(
    make_app: AppBuilder,
) -> None:
    harness = make_app(
        agent_manifest_path=str(_REGISTRY),
        gateway_models=(PRIVATE,),
        gateway_tasks={"agent.meridian-analyst.v2": TaskRoute(model=PRIVATE.name)},
        serving_probe=StubServingStatus(failure=RuntimeError("private backend detail")),
    )
    ada = harness.bearer("u-ada")

    async def exercise() -> None:
        async with harness.client() as client:
            response = await client.get(f"/api/v1/agents/{ANALYST_ID}/preflight", headers=ada)
            assert response.status_code == 503
            assert response.json() == {"detail": "Agent preflight is unavailable"}
            assert response.headers["cache-control"] == "private, no-store"
            assert b"capabilities" not in response.content
            assert b"private backend detail" not in response.content

    run(exercise())


def test_preflight_requires_authentication_like_the_rest_of_the_agents_api(
    make_app: AppBuilder,
) -> None:
    harness = make_app(agent_manifest_path=str(_REGISTRY))

    async def exercise() -> None:
        async with harness.client() as client:
            assert (await client.get(f"/api/v1/agents/{ANALYST_ID}/preflight")).status_code == 401

    run(exercise())


def test_a_presented_capability_hash_is_refused_when_the_deployment_cannot_answer(
    make_app: AppBuilder,
) -> None:
    """The Phase-1 refusal seam, end to end through the composed OIDC route."""
    harness = make_app(agent_manifest_path=str(_REGISTRY))
    ada = harness.bearer("u-ada")

    async def exercise() -> None:
        async with harness.client() as client:
            refused = await client.get(
                f"/api/v1/agents/{ANALYST_ID}/preflight",
                params={"capability_hash": "0" * 64},
                headers=ada,
            )
            unknown = await client.get(f"/api/v1/agents/{UNKNOWN_ID}/preflight", headers=ada)
            assert refused.status_code == 404
            assert (refused.status_code, refused.content, refused.headers["cache-control"]) == (
                unknown.status_code,
                unknown.content,
                unknown.headers["cache-control"],
            )

    run(exercise())


def test_hardship_preflight_is_visible_to_ops_and_a_404_for_everyone_else(
    make_app: AppBuilder,
) -> None:
    harness = make_app(agent_manifest_path=str(_REGISTRY))
    ops = harness.bearer("u-ops", groups=(OPS_GROUP,))
    staff = harness.bearer("u-staff", groups=("users",))

    async def exercise() -> None:
        async with harness.client() as client:
            visible = await client.get(f"/api/v1/agents/{HARDSHIP_ID}/preflight", headers=ops)
            hidden = await client.get(f"/api/v1/agents/{HARDSHIP_ID}/preflight", headers=staff)
            unknown = await client.get(f"/api/v1/agents/{UNKNOWN_ID}/preflight", headers=staff)
            assert visible.status_code == 200
            assert visible.json()["agent_id"] == HARDSHIP_ID
            assert (hidden.status_code, hidden.content) == (unknown.status_code, unknown.content)
            assert hidden.status_code == 404

    run(exercise())


# --- 2. a composed producer answers over REAL composed truth ---------------------------


def test_a_composed_producer_projects_only_grants_this_deployment_actually_registered(
    composed: Platform,
) -> None:
    """The intersection is production truth, not a fixture arrangement.

    Meridian Analyst declares four Tool grants. This composition registers exactly
    one of them in the frozen governed executor, and separately registers a tool
    the Analyst is not granted. Both exclusions are asserted by NAME, so the claim
    is falsifiable rather than derived from the same set the code reads.
    """
    registered = {spec.name for spec in composed.executor.tool_specs}
    assert GRANTED_AND_REGISTERED in registered
    assert REGISTERED_NOT_GRANTED in registered
    assert registered.isdisjoint(GRANTED_NOT_REGISTERED)

    result = _required(composed, ANALYST_ID, Principal(id="user:ops", groups=(OPS_GROUP,)))
    capabilities = result.capabilities
    assert capabilities is not None
    assert [tool.name for tool in capabilities.tools] == [GRANTED_AND_REGISTERED]
    row = capabilities.tools[0]
    assert (row.label, row.read_write_class, row.connection_identity_label) == (
        f"Label for {GRANTED_AND_REGISTERED}",
        "read",
        "conn:governed-core",
    )
    # No health producer is composed, and 011A's third value says so rather than
    # defaulting the row green.
    assert row.health == "unknown"
    # Knowledge/Connector truth is genuinely absent from this composition, so no
    # connector row is invented for the Analyst's declared Knowledge grant.
    assert capabilities.connectors == ()
    assert composed.agent_catalog.get_visible(ANALYST_ID, Principal(id="user:ops")) is not None


def test_an_ineligible_principal_gets_an_answered_empty_set_not_a_null_one(
    composed: Platform,
) -> None:
    """Answered-and-empty and cannot-answer stay different values in production."""
    eligible = _required(composed, ANALYST_ID, Principal(id="user:ops", groups=(OPS_GROUP,)))
    ineligible = _required(composed, ANALYST_ID, Principal(id="user:staff", groups=("users",)))

    assert eligible.capabilities is not None
    assert ineligible.capabilities is not None
    assert len(eligible.capabilities.tools) == 1
    assert ineligible.capabilities.tools == ()
    assert eligible.capabilities.capability_hash != ineligible.capabilities.capability_hash
    # Same Agent definition on both sides: the surface moved, not the Agent.
    assert eligible.definition_hash == ineligible.definition_hash


def test_the_composed_provider_carries_the_one_frozen_platform_config_hash(
    composed: Platform,
) -> None:
    """One composed hash, captured after the freeze -- never a second one.

    Reaching into the composed graph is the point of this control: the config hash
    has no effect on a preflight ANSWER, so the only way to pin that this seam was
    composed from the frozen hash (and not from a value captured earlier in
    ``build_platform``) is to read what the composed provider actually holds.
    """
    provider = cast(Any, composed.agent_preflight)._inventory_provider
    assert provider.config_hash == composed.config_hash
    assert composed.effective_agent_tool_grants[ANALYST_ID] == ("read.knowledge.search",)
    assert len(composed.config_hash) == 64


def test_an_absent_metadata_producer_is_the_only_difference_from_the_composed_one(
    tmp_path: Path, composed: Platform
) -> None:
    """The default recipe answers null; the SAME recipe plus a producer answers."""
    reset_config_hash_for_testing()
    bare = build_platform(
        _api_settings(agent_manifest_path=str(_REGISTRY)),
        _seams(tmp_path, key="preflight-bare"),
    )
    try:
        principal = Principal(id="user:ops", groups=(OPS_GROUP,))
        assert _required(bare, ANALYST_ID, principal).capabilities is None
        assert _required(composed, ANALYST_ID, principal).capabilities is not None
    finally:
        for engine in bare.engines:
            engine.dispose()


# --- 3. PRE-009A's dormant readiness boundary is untouched -----------------------------


def test_composing_the_preflight_leaves_agent_run_storage_dormant(composed: Platform) -> None:
    """PRE-011E reads no Run state and activates nothing; 009C owns activation.

    The composed deployment sets neither private-payload key variable, declares no
    deployment signal and retains no private rows, so 009A's three-signal union
    must still read "dormant" with the preflight composed beside it.
    """
    assert composed.agent_run_store is None


def test_the_composed_apps_run_store_accessor_still_refuses_while_the_preflight_serves(
    make_app: AppBuilder,
) -> None:
    """The two seams are independent: one is live, the other is still dormant.

    Asserting the field is ``None`` would only restate the platform-level control
    above; this asserts the BEHAVIOUR 009A's boundary actually provides -- the
    accessor refuses 503 -- on the same app whose preflight now answers.
    """
    app = make_app(agent_manifest_path=str(_REGISTRY)).app
    request = cast(Any, SimpleNamespace(app=_inner(app)))
    with pytest.raises(HTTPException) as refused:
        get_agent_run_store(request)
    assert refused.value.status_code == 503
    assert get_state(app).agent_preflight is not None


# --- 4. composed authorization metadata cannot be edited after composition -------------


def test_mutating_the_retained_metadata_dicts_after_composition_changes_nothing(
    composed: Platform,
) -> None:
    """``frozen=True`` freezes the bindings, not the mappings behind them.

    Composition hands the visibility and health mappings in as ordinary dicts. A
    caller that keeps its reference could otherwise flip a Tool from deny-default
    to ``all_authenticated``, or a health row from absent to ``True``, AFTER the
    provider was built — and the already-composed provider would project the new
    values. This mutates both retained dicts through the caller's own reference
    and requires the projection not to move.
    """
    retained_visibility = {GRANTED_AND_REGISTERED: StaticCapabilityVisibility(groups=(OPS_GROUP,))}
    retained_health: dict[str, bool | None] = {}
    metadata = AgentCapabilityMetadata(
        tool_bindings=_metadata().tool_bindings,
        static_tool_visibility=retained_visibility,
        tool_health=retained_health,
    )
    definition = composed.agent_catalog.get_visible(ANALYST_ID, Principal(id="user:ops"))
    assert definition is not None
    provider = _ComposedAgentCapabilityInventory(
        definitions={ANALYST_ID: definition.definition},
        tool_specs=tuple(composed.executor.tool_specs),
        metadata=metadata,
        config_hash=composed.config_hash,
        effective_tool_grants=composed.effective_agent_tool_grants,
    )

    def project(principal: Principal) -> Any:
        return run(
            provider(
                agent_id=ANALYST_ID,
                agent_version=definition.definition.version,
                agent_definition_hash=definition.definition.definition_hash,
                principal=principal,
            )
        )

    ops = Principal(id="user:ops", groups=(OPS_GROUP,))
    staff = Principal(id="user:staff", groups=("users",))
    assert [tool.name for tool in project(ops).tools] == [GRANTED_AND_REGISTERED]
    assert project(ops).tools[0].health == "unknown"
    assert project(staff).tools == ()

    # The caller edits the dicts it still holds a reference to.
    retained_visibility[GRANTED_AND_REGISTERED] = StaticCapabilityVisibility(all_authenticated=True)
    retained_health[GRANTED_AND_REGISTERED] = True

    assert project(staff).tools == (), "a retained dict widened composed visibility"
    assert project(ops).tools[0].health == "unknown", "a retained dict rewrote composed health"
    # The control: the snapshot is a copy, not a freeze of the whole projection —
    # a provider composed from the MUTATED dicts does see the new values, so the
    # assertions above are the snapshot and not an inert projection.
    rebuilt = _ComposedAgentCapabilityInventory(
        definitions={ANALYST_ID: definition.definition},
        tool_specs=tuple(composed.executor.tool_specs),
        metadata=AgentCapabilityMetadata(
            tool_bindings=_metadata().tool_bindings,
            static_tool_visibility=retained_visibility,
            tool_health=retained_health,
        ),
        config_hash=composed.config_hash,
        effective_tool_grants=composed.effective_agent_tool_grants,
    )
    widened = run(
        rebuilt(
            agent_id=ANALYST_ID,
            agent_version=definition.definition.version,
            agent_definition_hash=definition.definition.definition_hash,
            principal=staff,
        )
    )
    assert [tool.name for tool in widened.tools] == [GRANTED_AND_REGISTERED]
    assert widened.tools[0].health == "healthy"
