# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The AIR-GAP DEMO BEAT, proven at the layer that enforces it.

``test_network_airgap.py`` proves the toggle itself. This module proves the
BEAT — on the serving deployment's own model/task shape — as an ordered
sequence:

* (a) engage while tasks route frontier -> refused, and the refusal is a 409
      NAMING the offending routes (it used to be a generic wrapped 500);
* (b) ``adjudicate`` is legally repointable in-VPC: since NE-CTX-IN-FORCE
      ``refusal_for_target`` carries NO input-budget floor, so a repoint down to
      a narrower in-VPC model is permitted and the trim guard sizes itself to
      that model at request time instead;
* (c) the whole path — three in-VPC repoints + the engage — runs through the
      HTTP surface on the HONEST governed route: each repoint parks for a
      named human approval, a DIFFERENT human grants it, and only the engage
      (Tier 1 by tiers.rego) applies without one;
* (d) the demo-only ``NE_DEMO_UNAPPROVED_ROUTE_REPOINT`` switch
      (NE-AIRGAP-NO-APPROVAL, owner-authorized for the 2026-08-28 demo ONLY)
      is RETIRED (POST_DEMO.md item 20): exporting it changes NOTHING — the
      in-VPC repoint declares Tier 2 and parks for approval regardless;
* (e) under the engaged gap the private GPU lane still resolves and is
      permitted, and the frontier lane is refused BEFORE transport (proved by
      a host-resolver spy that is never called).

Every guard is bidirectional per docs/testing/vacuous-guards.md: each blocking
assertion sits next to a passing control on the SAME real enforcement object.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
import sqlalchemy as sa

from ne.api.model_routing import (
    ROUTE_EXTERNAL_ACTION,
    ROUTE_INTERNAL_ACTION,
    refusal_for_target,
)
from ne.api.network_egress import (
    EGRESS_EXPAND_ACTION,
    EGRESS_RESTRICT_ACTION,
    EgressPostureStore,
    FrontierRoutedTasksError,
    ensure_egress_posture_schema,
)
from ne.audit.store import ensure_audit_schema
from ne.contracts import Lane, freeze_config_hash
from ne.contracts.types import ApprovalTier
from ne.gateway.config import ModelDef, TaskRoute
from ne.gateway.egress import EgressPolicy, EgressSettings
from ne.gateway.errors import AirGapViolationError
from ne.runtime.actions import tier_for_action
from ne.runtime.approval_store import ne_approval_requests
from ne.runtime.config import RuntimeSettings

from .conftest import AppBuilder, Harness, get_state, run

FRONTIER_HOST = "api.anthropic.com"

#: The private GPU rig the serving build routes at: an RFC1918 LITERAL, so it is
#: in-VPC by address and not egress at all (``EgressPolicy._is_in_vpc`` returns
#: on the literal without a DNS lookup). Under an engaged gap it must keep
#: working — that is the whole content of the beat's second half.
GPU_API_BASE = "http://10.77.0.2:10200/v1"


def private_default() -> ModelDef:
    """``private-default`` exactly as ``ne demo up --gpu-vllm`` emits it
    (tools/ne-cli/src/ne/cli/demo/live.py ``_gateway_lane_env``)."""
    return ModelDef(
        name="private-default",
        litellm_model="hosted_vllm/qwen3.6-35b-a3b",
        provider="hosted_vllm",
        lane=Lane.PRIVATE_VLLM,
        api_base=GPU_API_BASE,
        context_window=262144,
        max_output_tokens=24576,
    )


def frontier_default() -> ModelDef:
    """``frontier-default`` as ``--frontier anthropic`` emits it: NO api_base,
    so the egress host is derived from the provider."""
    return ModelDef(
        name="frontier-default",
        litellm_model="anthropic/claude-sonnet-5",
        provider="anthropic",
        lane=Lane.FRONTIER_API,
        context_window=262144,
        max_output_tokens=24576,
    )


def frontier_best() -> ModelDef:
    """``frontier-best`` — the ``adjudicate`` governed-DENY exhibit.

    NE-CTX-IN-FORCE: the emitter declares this row at the window its model
    actually serves (1M), NOT the private lane's — every model uses its own
    maximum context. Mirrored here because the in-VPC repoint below must be
    legal against the WIDEST realistic primary, which is exactly the case the
    retired input-budget floor made impossible. The NE-161 runtime gate still
    refuses this lane at call time — the DENY exhibit is a runtime property, not
    a config-window property."""
    return ModelDef(
        name="frontier-best",
        litellm_model="anthropic/claude-fable-5",
        provider="anthropic",
        lane=Lane.FRONTIER_API,
        context_window=1_000_000,
        max_output_tokens=24576,
    )


SERVING_MODELS = [private_default(), frontier_best(), frontier_default()]

#: The serving build's boot task table (``--gpu-vllm`` + ``--frontier
#: anthropic``). Mirrors ``_gateway_lane_env``; the CLI-side test
#: ``tools/ne-cli/tests/test_air_gap_beat_routes.py`` pins that this literal and
#: the emitter agree, so this copy cannot drift silently.
SERVING_TASKS: dict[str, Any] = {
    "agent": {"model": "frontier-default"},
    "agent.meridian-analyst.v2": {"model": "frontier-default", "fallbacks": ["private-default"]},
    "agent.meridian-hardship-review.v3": {
        "model": "private-default",
        "fallbacks": ["frontier-default"],
    },
    "triage": {"model": "private-default", "fallbacks": ["frontier-default"]},
    "reason": {"model": "private-default", "fallbacks": ["frontier-default"]},
    "adjudicate": {"model": "frontier-best"},
    "skill-authoring": {"model": "private-default", "fallbacks": ["frontier-default"]},
    "grounding-assist": {"model": "private-default", "fallbacks": ["frontier-default"]},
    "graph-extract": {"model": "private-default", "fallbacks": ["frontier-default"]},
}

#: The frontier-routed PRIMARIES of that table — the beat's repoint list. The
#: air-gap guard reads ``route.model`` only: a frontier FALLBACK does not block
#: engagement (and would not egress under an engaged gap either).
SERVING_FRONTIER_PRIMARIES = ("adjudicate", "agent", "agent.meridian-analyst.v2")

#: What the 409 must NAME, exactly as ``platform.py``'s provider renders them.
SERVING_FRONTIER_ROUTES = (
    "adjudicate -> frontier-best",
    "agent -> frontier-default",
    "agent.meridian-analyst.v2 -> frontier-default",
)


@pytest.fixture
def allowlisted_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Boot with the frontier host allowlisted and the gap OFF — the live
    serving posture, in which the frontier lane works until the flip."""
    monkeypatch.setenv("NE_EGRESS_ALLOWED_FRONTIER_HOSTS", json.dumps([FRONTIER_HOST]))
    monkeypatch.delenv("NE_EGRESS_AIR_GAP", raising=False)
    monkeypatch.delenv("AIR_GAP", raising=False)


async def wait_until(predicate: Any, *, timeout_s: float = 20.0) -> bool:
    """Poll with a deadline (loaded-box discipline: never assert immediately
    after an async wait)."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return bool(predicate())


class ResolverSpy:
    """A DNS resolver that records every host it was asked about.

    Used to prove "refused BEFORE transport": ``EgressPolicy._check_frontier``
    checks the posture FIRST, so under an engaged gap it never reaches
    ``_resolve_frontier_host``. Zero recorded lookups is a positive proof that
    no name resolution — the step immediately preceding any socket — happened.
    """

    def __init__(self) -> None:
        self.hosts: list[str] = []

    def __call__(self, host: str) -> tuple[str, ...]:
        self.hosts.append(host)
        return ("203.0.113.7",)


def serving_app(make_app: AppBuilder, tasks: dict[str, Any]) -> Harness:
    return make_app(
        ops_roles=("ops",),
        approver_roles=("approvers",),
        gateway_models=SERVING_MODELS,
        gateway_tasks={k: TaskRoute(**v) for k, v in tasks.items()},
    )


def guard_of(app: Any, *, host_resolver: Any = None) -> EgressPolicy:
    """The REAL enforcement object over the platform's OWN provider — the same
    read the production gateway performs per model call."""
    provider = get_state(app).egress_settings_provider
    assert provider is not None
    return EgressPolicy(settings_provider=provider, host_resolver=host_resolver)


def approval_row_count(app: Any) -> int:
    """EVERY approval row ever minted — pending, granted, rejected, expired —
    on the same engine the durable gate writes. ``list_pending()`` alone would
    miss a row that was minted and auto-decided; "no approval" means NO ROW."""
    gate = get_state(app).gate
    engine: sa.Engine = gate._store._engine
    with engine.connect() as conn:
        return int(
            conn.execute(sa.select(sa.func.count()).select_from(ne_approval_requests)).scalar_one()
        )


# --- (a) the refusal is legible: 409, naming the routes to clear -----------------


def test_engage_refusal_is_typed_and_names_the_routes(tmp_path: Any) -> None:
    """Store-level: the guard raises ``FrontierRoutedTasksError`` carrying the
    offending routes, is still a ``ValueError`` (the old contract), and writes
    NOTHING. Control on the same engine: an empty route list applies."""
    freeze_config_hash(RuntimeSettings())
    engine = sa.create_engine(f"sqlite:///{tmp_path}/posture.db")
    try:
        ensure_audit_schema(engine)
        ensure_egress_posture_schema(engine)

        routed_frontier = EgressPostureStore(
            engine, EgressSettings(air_gap=False), lambda: SERVING_FRONTIER_ROUTES
        )
        with pytest.raises(FrontierRoutedTasksError) as excinfo:
            routed_frontier.apply(
                air_gap=True, actor="user:ops-olga", via_action=EGRESS_RESTRICT_ACTION
            )
        assert isinstance(excinfo.value, ValueError)  # the pre-existing contract
        assert excinfo.value.routes == SERVING_FRONTIER_ROUTES
        for named in SERVING_FRONTIER_ROUTES:
            assert named in str(excinfo.value)
        assert "repoint them in-VPC first" in str(excinfo.value)
        # The refusal happens BEFORE any state is written: nothing on the chain,
        # no posture row.
        assert routed_frontier.read_state().air_gap is False
        assert routed_frontier.read_state().version == 0

        # CONTROL, same engine and same boot settings: with no frontier route in
        # force the identical call applies. So the refusal above is the route
        # guard, not a broken store.
        no_frontier = EgressPostureStore(engine, EgressSettings(air_gap=False), lambda: ())
        applied = json.loads(
            no_frontier.apply(
                air_gap=True, actor="user:ops-olga", via_action=EGRESS_RESTRICT_ACTION
            )
        )
        assert applied["applied"] is True
        assert no_frontier.read_state().air_gap is True
    finally:
        engine.dispose()


def test_engage_answers_409_naming_the_tasks_while_frontier_routed(
    allowlisted_env: None, make_app: AppBuilder
) -> None:
    """On the SERVING route table, engaging the gap fails as a 409 whose detail
    names every offending route — the on-stage-legible refusal that replaced
    the generic 500 (NE-AIRGAP-NO-APPROVAL's honesty fix). Nothing moves."""
    harness = serving_app(make_app, SERVING_TASKS)
    policy = guard_of(harness.app)

    async def flow() -> None:
        async with harness.client() as client:
            # CONTROL — the gap is off and the frontier lane is live.
            policy(frontier_default())  # no raise

            resp = await client.post(
                "/api/v1/network/air-gap",
                json={"air_gap": True},
                headers=harness.bearer("user:ops-olga", groups=("ops",)),
            )
            assert resp.status_code == 409, resp.text
            detail = resp.json()["detail"]
            for named in SERVING_FRONTIER_ROUTES:
                assert named in detail, detail
            assert "repoint them in-VPC first" in detail

            # NOTHING moved: the posture surface still reports disengaged and the
            # SAME enforcement object still permits the SAME frontier model.
            posture = await client.get(
                "/api/v1/posture", headers=harness.bearer("user:ops-olga", groups=("ops",))
            )
            assert posture.status_code == 200, posture.text
            assert posture.json()["air_gap"] is False
            policy(frontier_default())  # still no raise

    run(flow())


def test_the_serving_route_table_has_exactly_three_frontier_primaries(
    allowlisted_env: None, make_app: AppBuilder
) -> None:
    """The definitive repoint list, computed by the guard's own expression."""
    harness = serving_app(make_app, SERVING_TASKS)
    store = get_state(harness.app).route_store
    assert store is not None
    by_name = store.models_by_name()
    frontier = sorted(
        task
        for task, route in store.effective_routes().items()
        if by_name[route.model].lane is Lane.FRONTIER_API
    )
    assert tuple(frontier) == SERVING_FRONTIER_PRIMARIES

    # And the control: every OTHER routed task is already in-VPC, so the list
    # above is the complete blocker set rather than an arbitrary subset.
    private = sorted(set(store.effective_routes()) - set(frontier))
    assert private and all(
        by_name[store.effective_routes()[task].model].lane is Lane.PRIVATE_VLLM for task in private
    )


# --- (b) adjudicate is now legally repointable -----------------------------------


def test_adjudicate_is_repointable_in_vpc_from_a_wider_primary(
    allowlisted_env: None, make_app: AppBuilder
) -> None:
    """THE FORMER BLOCKER, cleared. ``frontier-best`` declares a 1M window — far
    WIDER than the private lane's — and ``refusal_for_target``, the ONE rule the
    console's ``selectable`` flag, the 409 pre-flight and ``validate_args`` all
    read, still permits the repoint, because NE-CTX-IN-FORCE removed its
    input-budget floor."""
    harness = serving_app(make_app, SERVING_TASKS)
    store = get_state(harness.app).route_store
    assert store is not None
    boot = store.boot_settings

    for task in SERVING_FRONTIER_PRIMARIES:
        current = boot.tasks[task].model
        assert refusal_for_target(boot, task, "private-default", current=current) is None, task

    # The budget arithmetic behind it, stated as the condition that USED to make
    # this impossible: the target's input budget is far BELOW the primary's, and
    # the repoint is permitted anyway.
    by_name = {m.name: m for m in boot.models}
    best_budget = boot.context_budget_for(by_name["frontier-best"]).input_budget
    private_budget = boot.context_budget_for(by_name["private-default"]).input_budget
    assert private_budget < best_budget

    # CONTROL — the rule still refuses SOMETHING: under an engaged gap no task
    # may be pointed AT a frontier model.
    refusal = refusal_for_target(
        boot, "agent", "frontier-default", air_gap=True, current="private-default"
    )
    assert refusal is not None and "air gap is engaged" in refusal


# --- (c) the beat, end to end, through the Tier-2 approval gate ------------------


def test_the_full_beat_runs_through_named_human_approvals(
    allowlisted_env: None, make_app: AppBuilder
) -> None:
    """The post-demo sequence, through the HTTP surface, on the FULL serving
    table (``adjudicate`` included): each in-VPC repoint PARKS for a Tier-2
    approval and applies only when a DIFFERENT named human grants it; the
    engage — Tier 1 by tiers.rego — then applies immediately and mints no
    approval row of its own. The chain records the real approver."""
    harness = serving_app(make_app, SERVING_TASKS)
    state = get_state(harness.app)
    route_store = state.route_store
    assert route_store is not None
    spy = ResolverSpy()
    policy = guard_of(harness.app, host_resolver=spy)

    async def flow() -> None:
        async with harness.client() as client:
            ops = harness.bearer("user:ops-olga", groups=("ops",))
            approver = harness.bearer("user:approver-ada", groups=("approvers",))

            # STEP 1 — repoint all three frontier primaries in-VPC, over HTTP.
            # Each parks (fail closed: the register does NOT move) and applies
            # only on the named approval by a different human.
            for expected_rows, task in enumerate(SERVING_FRONTIER_PRIMARIES, start=1):
                resp = await client.post(
                    "/api/v1/models/routing",
                    json={"task": task, "model": "private-default"},
                    headers=ops,
                )
                assert resp.status_code == 200, resp.text
                body = resp.json()
                assert body["status"] == "pending", (task, body)
                assert body["request_id"], task
                assert approval_row_count(harness.app) == expected_rows, task
                assert route_store.effective_routes()[task].model != "private-default"

                approve = await client.post(
                    f"/api/v1/approvals/{body['request_id']}/approve",
                    json={},
                    headers=approver,
                )
                assert approve.status_code == 200, approve.text
                applied = await wait_until(
                    lambda task=task: (
                        route_store.effective_routes()[task].model == "private-default"
                    )
                )
                assert applied, f"approved repoint of {task!r} never reached the register"

            live = route_store.read_state()
            for task in SERVING_FRONTIER_PRIMARIES:
                assert live[task].model == "private-default"
                assert live[task].changed_by == "user:ops-olga"
                assert live[task].approved_by == "user:approver-ada"
            by_name = route_store.models_by_name()
            assert not any(
                by_name[route.model].lane is Lane.FRONTIER_API
                for route in route_store.effective_routes().values()
            )

            # STEP 2 — the SAME submit that answered 409 on the unrepointed
            # table now applies: Tier 1, immediately, and the approval-row
            # count still shows ONLY the three repoints (the engage minted none).
            resp = await client.post("/api/v1/network/air-gap", json={"air_gap": True}, headers=ops)
            assert resp.status_code == 200, resp.text
            assert resp.json() == {"status": "applied", "request_id": None}
            assert approval_row_count(harness.app) == len(SERVING_FRONTIER_PRIMARIES)

            posture = await client.get("/api/v1/posture", headers=ops)
            assert posture.json()["air_gap"] is True

            # (e-1) THE PRIVATE GPU LANE SURVIVES. Same enforcement object, same
            # engaged posture: the in-VPC literal is permitted, and no DNS was
            # needed to decide that.
            policy(private_default())  # no raise
            assert spy.hosts == []

            # (e-2) THE FRONTIER LANE DIES BEFORE TRANSPORT. The refusal arrives
            # without a single name resolution, which is the step immediately
            # before any socket would open.
            for model in (frontier_default(), frontier_best()):
                with pytest.raises(AirGapViolationError):
                    policy(model)
            assert spy.hosts == []

            # CONTROL for the spy itself: it IS wired and DOES get called when
            # the posture permits a frontier host lookup. Without this the
            # `spy.hosts == []` assertions above would be vacuous.
            open_policy = EgressPolicy(
                settings=EgressSettings(air_gap=False, allowed_frontier_hosts=(FRONTIER_HOST,)),
                host_resolver=spy,
            )
            open_policy(
                ModelDef(
                    name="named-private",
                    litellm_model="hosted_vllm/x",
                    provider="hosted_vllm",
                    lane=Lane.PRIVATE_VLLM,
                    api_base="http://gpu-rig.internal:10200/v1",
                    context_window=32768,
                )
            )
            assert spy.hosts == ["gpu-rig.internal"]

    run(flow())


def test_both_repoint_actions_declare_tier_2(make_app: AppBuilder) -> None:
    """Asserted against the LIVE registered tool specs: BOTH routing actions
    declare Tier 2 (the internal one as an escalation over its tiers.rego
    class), the engage stays Tier 1 and the disengage Tier 2 — the one-way
    property, with no demo carve-out left anywhere in the registry."""
    assert tier_for_action(ROUTE_INTERNAL_ACTION) is ApprovalTier.TIER_1  # the CLASS
    assert tier_for_action(ROUTE_EXTERNAL_ACTION) is ApprovalTier.TIER_2

    harness = serving_app(make_app, SERVING_TASKS)
    specs = {spec.name: spec for spec in get_state(harness.app).executor.tool_specs}
    for action in (ROUTE_INTERNAL_ACTION, ROUTE_EXTERNAL_ACTION):
        assert specs[action].tier is ApprovalTier.TIER_2, action
    # CONTROL on the same registry: the engage really is the cheap one.
    assert specs[EGRESS_RESTRICT_ACTION].tier is ApprovalTier.TIER_1
    assert specs[EGRESS_EXPAND_ACTION].tier is ApprovalTier.TIER_2


# --- (d) the demo switch is RETIRED: exporting it changes nothing ----------------


def test_the_retired_demo_switch_is_inert(
    make_app: AppBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST_DEMO.md item 20, pinned: ``NE_DEMO_UNAPPROVED_ROUTE_REPOINT=1`` in
    the environment — the exact export the 2026-08-28 deploy script carried —
    no longer reaches ANYTHING. The app registers the same Tier-2 declarations
    it registers without it. If someone reintroduces a read of that variable,
    this test is the tripwire."""
    monkeypatch.setenv("NE_DEMO_UNAPPROVED_ROUTE_REPOINT", "1")
    harness = serving_app(make_app, SERVING_TASKS)
    specs = {spec.name: spec for spec in get_state(harness.app).executor.tool_specs}
    for action in (ROUTE_INTERNAL_ACTION, ROUTE_EXTERNAL_ACTION):
        assert specs[action].tier is ApprovalTier.TIER_2, action
    assert specs[EGRESS_EXPAND_ACTION].tier is ApprovalTier.TIER_2


def test_a_repoint_parks_for_approval_even_with_the_retired_switch_set(
    allowlisted_env: None, make_app: AppBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The behavioral half of the tripwire, observed through the HTTP surface:
    with the retired env var exported, a PERMITTED in-VPC repoint still answers
    ``pending`` with a durable approval row, not ``applied`` — no approval
    bypass survives, whatever the deployment shell exports."""
    monkeypatch.setenv("NE_DEMO_UNAPPROVED_ROUTE_REPOINT", "1")
    harness = serving_app(make_app, SERVING_TASKS)

    async def flow() -> None:
        async with harness.client() as client:
            headers = harness.bearer("user:ops-olga", groups=("ops",))
            resp = await client.post(
                "/api/v1/models/routing",
                json={"task": "agent", "model": "private-default"},
                headers=headers,
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["status"] == "pending"
            assert body["request_id"]
            assert approval_row_count(harness.app) == 1

            # The register did NOT move while the row is undecided (fail closed).
            store = get_state(harness.app).route_store
            assert store is not None
            assert store.effective_routes()["agent"].model == "frontier-default"

    run(flow())


# --- the one-way property, unchanged ---------------------------------------------


def test_the_egress_reversal_is_tier_2_and_the_engage_is_tier_1() -> None:
    """The asymmetry the beat's ONE-WAY WARNING rests on, from the tier rule
    itself (``ne.runtime.actions.tier_for_action``, the Python mirror of
    ``packages/ne-govern/policies/ne/govern/tiers.rego``)."""
    assert EGRESS_RESTRICT_ACTION == "write.internal.network.egress.restrict"
    assert EGRESS_EXPAND_ACTION == "write.external.network.egress.expand"
    assert tier_for_action(EGRESS_RESTRICT_ACTION) is ApprovalTier.TIER_1
    assert tier_for_action(EGRESS_EXPAND_ACTION) is ApprovalTier.TIER_2
