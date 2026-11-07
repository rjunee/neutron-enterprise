# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-MODEL-ROUTING — the governed LIVE model-route change, end to end.

Every guard here is bidirectional per docs/testing/vacuous-guards.md: a refusal
is asserted against the REAL object that would have been changed (the route
register the enforcing provider reads), never against a settings value alone,
and every blocking assertion sits beside a control on the same object.

The properties under test, in the order the design doc argues them:

* the console cannot change a route without a PDP consult — the route drives THE
  governed executor and nothing else can write the register (§4);
* the change is LIVE — the provider the gateway reads returns the new model with
  no restart and no re-composition (§3);
* separation of duties — an approval granted by the requester is refused (§4);
* the direction rule — a frontier target cannot ride the internal action (§4);
* an override may only ever name a BOOT-REGISTERED model (§2.1);
* the provider fails closed to the BOOT routes (§5);
* the change does not move the frozen ``config_hash`` (§3).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
import sqlalchemy as sa
from pydantic import SecretStr
from sqlalchemy.exc import OperationalError

from ne.api.model_routing import (
    MODEL_ROUTING_CHANGED_ACTION,
    ROUTE_EXTERNAL_ACTION,
    ROUTE_INTERNAL_ACTION,
    ModelRouteStore,
    build_route_provider,
    ensure_model_route_schema,
    ne_model_route_overrides,
    refusal_for_selection,
    refusal_for_target,
)
from ne.audit import ensure_audit_schema
from ne.audit.store import ne_audit_log
from ne.audit.verify import verify_chain
from ne.contracts import Lane, freeze_config_hash
from ne.gateway.config import GatewaySettings, ModelDef, TaskRoute

from .conftest import AppBuilder, Harness, get_state, run

# Two in-VPC models and one frontier model — a superset of the demo rig's shape
# (`ne demo up` composes private-default / frontier-best; the second in-VPC lane
# here exists so explicit-selection mechanics have a same-lane target).
PRIVATE_DEFAULT = ModelDef(
    name="private-default",
    litellm_model="hosted_vllm/qwen",
    provider="hosted_vllm",
    lane=Lane.PRIVATE_VLLM,
    api_base="http://vllm:8000/v1",
    context_window=32768,
)
AUX_MODEL = ModelDef(
    name="aux-model",
    litellm_model="hosted_vllm/qwen-small",
    provider="hosted_vllm",
    lane=Lane.PRIVATE_VLLM,
    api_base="http://vllm:8000/v1",
    context_window=32768,
)
AUX_MODEL_2 = ModelDef(
    name="aux-model-2",
    litellm_model="hosted_vllm/qwen-small-2",
    provider="hosted_vllm",
    lane=Lane.PRIVATE_VLLM,
    api_base="http://vllm:8000/v1",
    context_window=32768,
)
FRONTIER = ModelDef(
    name="frontier-best",
    litellm_model="anthropic/claude-sonnet-4-5",
    provider="anthropic",
    lane=Lane.FRONTIER_API,
    context_window=200_000,
)
SMALL_WINDOW = ModelDef(
    name="tiny-model",
    litellm_model="hosted_vllm/tiny",
    provider="hosted_vllm",
    lane=Lane.PRIVATE_VLLM,
    api_base="http://vllm:8000/v1",
    context_window=4096,
)

#: Narrower than the boot primary (input budget 6144): the legal down-routing
#: target the deleted budget-width floor would have refused, and the narrow
#: geometry the snapshot-fit rule measures against.
NARROW_FIT = ModelDef(
    name="narrow-fit",
    litellm_model="hosted_vllm/qwen-narrow",
    provider="hosted_vllm",
    lane=Lane.PRIVATE_VLLM,
    api_base="http://vllm:8000/v1",
    context_window=8192,
)

MODELS = [PRIVATE_DEFAULT, AUX_MODEL, FRONTIER, SMALL_WINDOW, NARROW_FIT]
TASKS = {"agent": TaskRoute(model="private-default")}


def make_routing_app(make_app: AppBuilder, **kw: Any) -> Harness:
    return make_app(
        ops_roles=("ops",),
        approver_roles=("approvers",),
        gateway_models=kw.pop("gateway_models", MODELS),
        gateway_tasks=kw.pop("gateway_tasks", TASKS),
        **kw,
    )


def chain_events(harness: Harness, action: str) -> list[Any]:
    engine = sa.create_engine(harness.audit_url)
    try:
        with engine.connect() as conn:
            return list(
                conn.execute(
                    sa.select(ne_audit_log).where(ne_audit_log.c.action == action)
                ).fetchall()
            )
    finally:
        engine.dispose()


def override_rows(harness: Harness) -> list[Any]:
    """Read the register DIRECTLY — the object a bypass would have written."""
    engine = sa.create_engine(harness.audit_url)
    try:
        with engine.connect() as conn:
            return list(conn.execute(sa.text("SELECT * FROM ne_model_route_overrides")).fetchall())
    finally:
        engine.dispose()


def parked_count(harness: Harness) -> int:
    engine = sa.create_engine(harness.runtime_url)
    try:
        with engine.connect() as conn:
            return int(
                conn.execute(sa.text("SELECT COUNT(*) FROM ne_approval_requests")).scalar() or 0
            )
    finally:
        engine.dispose()


async def wait_until(predicate: Any, *, timeout_s: float = 20.0) -> bool:
    """Poll with a deadline (loaded-box discipline: never assert immediately
    after an async wait)."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return bool(predicate())


# --- the demo beat: propose -> a DIFFERENT human approves -> live ------------


def test_repoint_parks_for_approval_then_takes_effect_live_on_grant(
    make_app: AppBuilder,
) -> None:
    """THE beat. One human asks; a DIFFERENT human approves; the route the
    ENFORCING provider reads changes with no restart and no re-composition.

    The assertion is made against ``build_route_provider(state.route_store)`` —
    the very function ``build_platform`` hands to ``LiteLLMGateway`` — not
    against a settings object, so it cannot pass while the gateway would still
    route to the old model.
    """
    harness = make_routing_app(make_app)
    state = get_state(harness.app)
    assert state.route_store is not None
    provider = build_route_provider(state.route_store)

    # CONTROL: before any change the provider serves the deployment default.
    assert provider()["agent"].model == "private-default"

    async def flow() -> None:
        async with harness.client() as client:
            ops = harness.bearer("user:ops-olga", groups=("ops",))
            resp = await client.post(
                "/api/v1/models/routing",
                json={"task": "agent", "model": "aux-model"},
                headers=ops,
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["status"] == "pending", body
            request_id = body["request_id"]
            assert request_id

            # Parked, NOT pre-applied: the provider still serves the old model.
            assert provider()["agent"].model == "private-default"
            assert override_rows(harness) == []

            approve = await client.post(
                f"/api/v1/approvals/{request_id}/approve",
                json={},
                headers=harness.bearer("user:approver-ada", groups=("approvers",)),
            )
            assert approve.status_code == 200, approve.text

            applied = await wait_until(lambda: provider()["agent"].model == "aux-model")
            assert applied, "the approved repoint never reached the enforcing provider"

            # The console read reports the SAME thing the provider enforces.
            catalog = await client.get("/api/v1/models/routing", headers=ops)
            assert catalog.status_code == 200, catalog.text
            row = next(r for r in catalog.json()["tasks"] if r["task"] == "agent")
            assert row["model"] == "aux-model"
            assert row["default_model"] == "private-default"
            assert row["source"] == "override"
            assert row["changed_by"] == "user:ops-olga"
            assert row["approved_by"] == "user:approver-ada"

    run(flow())

    # C10: the change is on the chain, naming the requester AND the approver.
    events = chain_events(harness, "model.routing.changed")
    assert len(events) == 1
    detail = json.loads(events[0].payload["detail"])
    assert detail["task"] == "agent"
    assert detail["model"] == {"old": "private-default", "new": "aux-model"}
    assert detail["approved_by"] == "user:approver-ada"
    assert events[0].actor == "user:ops-olga"
    assert events[0].payload["provenance"]["changed_via"] == ROUTE_INTERNAL_ACTION

    # The B14 join: the executed tool call carries the consumed approval id.
    executed = [
        r
        for r in chain_events(harness, "tool.execute")
        if r.resource == f"tool:{ROUTE_INTERNAL_ACTION}"
    ]
    assert len(executed) == 1
    assert executed[0].outcome == "success"
    assert executed[0].payload["provenance"]["approval_id"] == detail["approval_id"]

    audit_engine = sa.create_engine(harness.audit_url)
    try:
        verification = verify_chain(audit_engine)
    finally:
        audit_engine.dispose()
    assert verification.valid, verification.reason


# --- C6: the route cannot change without a PDP consult ----------------------


def test_route_change_is_driven_through_the_governed_executor(make_app: AppBuilder) -> None:
    """The change reaches the register ONLY via the one governed executor.

    Asserted two ways, because either alone is satisfiable by a bypass: the PDP
    RECORDED a decision for this action name, and the tool-call audit trail for
    it exists. A route that wrote the register directly would produce neither.
    """
    harness = make_routing_app(make_app)

    async def flow() -> None:
        async with harness.client() as client:
            resp = await client.post(
                "/api/v1/models/routing",
                json={"task": "agent", "model": "aux-model"},
                headers=harness.bearer("user:ops-olga", groups=("ops",)),
            )
            assert resp.status_code == 200, resp.text

    run(flow())

    decisions = [
        r
        for r in chain_events(harness, "pdp.decide")
        if r.resource == f"tool:{ROUTE_INTERNAL_ACTION}"
    ]
    assert len(decisions) == 1, (
        "the routing change must consult the PDP exactly once, through the one "
        "governed executor — a route that wrote the register directly records nothing"
    )
    requested = [
        r
        for r in chain_events(harness, "approval.requested")
        if r.resource == f"tool:{ROUTE_INTERNAL_ACTION}"
    ]
    assert len(requested) == 1, "the Tier-2 human gate must have been entered"


def test_a_pdp_denial_refuses_the_change_and_writes_no_row(make_app: AppBuilder) -> None:
    """CONTROL PAIR for the test above: with the PDP denying this action, the
    route answers 403 and the register is untouched — so the executor's verdict,
    not the route's own opinion, is what decides."""
    from ne.contracts.governance import DecisionEffect, PolicyDecision

    class DenyRoutingPdp:
        def __init__(self, *a: Any, **kw: Any) -> None: ...

        async def decide(self, request: Any) -> PolicyDecision:
            if request.action in (ROUTE_INTERNAL_ACTION, ROUTE_EXTERNAL_ACTION):
                return PolicyDecision(
                    effect=DecisionEffect.DENY,
                    reason="denied by test policy",
                    policy_version="test",
                )
            return PolicyDecision(effect=DecisionEffect.ALLOW, policy_version="test")

        async def aclose(self) -> None: ...

    harness = make_routing_app(make_app, pdp_factory=lambda **_kw: DenyRoutingPdp())
    state = get_state(harness.app)
    assert state.route_store is not None
    provider = build_route_provider(state.route_store)

    async def flow() -> None:
        async with harness.client() as client:
            resp = await client.post(
                "/api/v1/models/routing",
                json={"task": "agent", "model": "aux-model"},
                headers=harness.bearer("user:ops-olga", groups=("ops",)),
            )
            assert resp.status_code == 403, resp.text

    run(flow())
    assert override_rows(harness) == []
    assert provider()["agent"].model == "private-default"
    assert parked_count(harness) == 0


# --- separation of duties ----------------------------------------------------


def test_a_change_approved_by_its_own_requester_is_refused_and_writes_nothing(
    make_app: AppBuilder,
) -> None:
    """The requester never gets a grant to spend: the DECISION itself is refused.

    The Tier-2 gate used to grant on ANY single named approver, so the requester
    could approve its own request and only this action's handler stood between
    that grant and the register. Separation of duties now lives on the decide
    path (``ne.runtime.approvals.refuse_self_approval``, applied by the gate and
    on both branches of ``POST /v1/approvals/{id}/approve``), so the 403
    arrives BEFORE any grant exists — nothing is granted, nothing executes, the
    register is untouched. The handler's own guard survives as defence in depth
    and is killed directly by the ``distinct_approver`` case below.
    """
    harness = make_routing_app(make_app)
    state = get_state(harness.app)
    assert state.route_store is not None
    provider = build_route_provider(state.route_store)

    async def flow() -> None:
        async with harness.client() as client:
            # One principal holding BOTH roles — the exact hazard.
            both = harness.bearer("user:ops-olga", groups=("ops", "approvers"))
            resp = await client.post(
                "/api/v1/models/routing",
                json={"task": "agent", "model": "aux-model"},
                headers=both,
            )
            assert resp.status_code == 200, resp.text
            request_id = resp.json()["request_id"]
            assert request_id

            approve = await client.post(
                f"/api/v1/approvals/{request_id}/approve", json={}, headers=both
            )
            assert approve.status_code == 403, approve.text
            assert "separation of duties" in approve.json()["detail"], approve.text

            # Give the waiter a chance to (wrongly) apply, then assert it did not.
            applied = await wait_until(
                lambda: provider()["agent"].model == "aux-model", timeout_s=3.0
            )
            assert not applied, (
                "a route change approved by the principal who requested it must be "
                "refused — separation of duties"
            )

    run(flow())
    assert override_rows(harness) == [], "the register must be untouched"
    assert chain_events(harness, "model.routing.changed") == []
    # Nothing was granted, so the handler never ran: no execution at all, and the
    # refusal is a named authorization denial rather than silence.
    assert chain_events(harness, "approval.granted") == []
    executed = [
        r
        for r in chain_events(harness, "tool.execute")
        if r.resource == f"tool:{ROUTE_INTERNAL_ACTION}"
    ]
    assert executed == []
    denials = [
        r
        for r in chain_events(harness, "api.approval.unauthorized")
        if json.loads(r.payload["detail"]).get("reason") == "requester-may-not-decide"
    ]
    assert len(denials) == 1
    assert denials[0].actor == "user:ops-olga"


@pytest.mark.parametrize("missing", ["principal", "approval", "distinct_approver"])
def test_route_binding_guards_fail_closed_without_executor_reachability_assumptions(
    make_app: AppBuilder, missing: str
) -> None:
    """Directly kill each handler guard even if executor tiering changes."""
    from types import SimpleNamespace

    from ne.api.model_routing import RouteChangeRefused, build_model_routing_bindings
    from ne.contracts.types import Principal

    harness = make_routing_app(make_app)
    state = get_state(harness.app)
    assert state.route_store is not None
    principal = Principal(id="user:requester", groups=("ops",), claims={})
    bound_principal = None if missing == "principal" else principal
    if missing == "approval":
        approval = None
    elif missing == "distinct_approver":
        approval = SimpleNamespace(approver=principal.id, approval_id="approval-self")
    else:
        approval = SimpleNamespace(approver="user:other", approval_id="approval-other")
    binding = next(
        item
        for item in build_model_routing_bindings(
            state.route_store, lambda: bound_principal, lambda: approval
        )
        if item.spec.name == ROUTE_INTERNAL_ACTION
    )

    async def flow() -> None:
        with pytest.raises(RouteChangeRefused):
            await binding.handler({"task": "agent", "model": "aux-model"})

    run(flow())
    assert override_rows(harness) == []


def test_concurrent_identical_route_submissions_share_one_approval_id(
    make_app: AppBuilder,
) -> None:
    """A double-click atomically converges on one owned approval row."""
    harness = make_routing_app(make_app)

    async def flow() -> None:
        async with harness.client() as client:
            ops = harness.bearer("user:ops-olga", groups=("ops",))
            first, second = await asyncio.gather(
                client.post(
                    "/api/v1/models/routing",
                    json={"task": "agent", "model": "aux-model"},
                    headers=ops,
                ),
                client.post(
                    "/api/v1/models/routing",
                    json={"task": "agent", "model": "aux-model"},
                    headers=ops,
                ),
            )
            assert first.status_code == second.status_code == 200
            assert first.json()["request_id"] == second.json()["request_id"]
            assert parked_count(harness) == 1
            catalog = await client.get("/api/v1/models/routing", headers=ops)
            pending = catalog.json()["tasks"][0]["pending_change"]
            assert pending == {
                "request_id": first.json()["request_id"],
                "requested_by": "user:ops-olga",
                "model": "aux-model",
            }

    run(flow())


# --- the direction rule ------------------------------------------------------


def test_a_frontier_target_rides_the_external_action_not_the_internal_one(
    make_app: AppBuilder,
) -> None:
    """The tier a change gets derives from what the change DOES. A frontier
    target is submitted under ``write.external.model.routing.repoint``; the
    internal (cheaper-classed) name is never used for it.

    Bidirectional: the in-VPC control in the sibling tests submits under the
    INTERNAL name, so this assertion distinguishes the two rather than merely
    observing that some action ran.
    """
    harness = make_routing_app(make_app)

    async def flow() -> None:
        async with harness.client() as client:
            resp = await client.post(
                "/api/v1/models/routing",
                json={"task": "agent", "model": "frontier-best"},
                headers=harness.bearer("user:ops-olga", groups=("ops",)),
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["status"] == "pending"

    run(flow())
    external = [
        r
        for r in chain_events(harness, "approval.requested")
        if r.resource == f"tool:{ROUTE_EXTERNAL_ACTION}"
    ]
    internal = [
        r
        for r in chain_events(harness, "approval.requested")
        if r.resource == f"tool:{ROUTE_INTERNAL_ACTION}"
    ]
    assert len(external) == 1
    assert internal == [], (
        "a frontier repoint must never be classed as an internal one — the tier "
        "derives from the change, not from the endpoint the caller picked"
    )


def test_the_internal_action_refuses_a_frontier_target_before_the_human_gate(
    make_app: AppBuilder,
) -> None:
    """Belt to the route's braces: calling the INTERNAL action directly with a
    frontier target — the smuggle a compromised caller would attempt — is
    refused by the binding itself, before any approval row exists.

    The control is the same call with an in-VPC target, which parks normally.
    """
    from ne.runtime.errors import ActionValidationError

    harness = make_routing_app(make_app)
    state = get_state(harness.app)

    async def flow() -> None:
        from ne.contracts.types import Principal

        principal = Principal(id="user:ops-olga", groups=("ops",), claims={})
        # BOUNDED, deliberately. "Before the human gate" is the claim, and
        # without the direction check this call does not fail — it PARKS,
        # blocking on an approval no one will grant, which is precisely the harm
        # (a change that cannot legitimately execute consuming a human
        # decision). An unbounded await would hang instead of reporting that.
        with pytest.raises(ActionValidationError) as excinfo:
            async with asyncio.timeout(20):
                await state.executor.execute(
                    ROUTE_INTERNAL_ACTION,
                    {"task": "agent", "model": "frontier-best"},
                    principal=principal,
                )
        assert "frontier-api" in str(excinfo.value)

    run(flow())
    assert parked_count(harness) == 0, (
        "a change that cannot execute must never consume a human decision"
    )
    assert override_rows(harness) == []


# --- an override may only name a boot-registered model -----------------------


def test_an_unregistered_model_is_a_404_and_mints_no_approval(make_app: AppBuilder) -> None:
    """A name on no list has no lane, so there is no action to derive. The
    honest 404 — and, decisively, no approval row: a routing change may only
    re-select a registered model, never introduce one."""
    harness = make_routing_app(make_app)

    async def flow() -> None:
        async with harness.client() as client:
            resp = await client.post(
                "/api/v1/models/routing",
                json={"task": "agent", "model": "attacker-model"},
                headers=harness.bearer("user:ops-olga", groups=("ops",)),
            )
            assert resp.status_code == 404, resp.text

    run(flow())
    assert parked_count(harness) == 0
    assert override_rows(harness) == []


def test_the_routing_surface_never_carries_an_endpoint_or_a_credential(
    make_app: AppBuilder,
) -> None:
    """C12 on the wire. The catalog names model ALIASES and LANES. The
    ``api_base`` of every composed model is absent from the payload, and so is
    every ``extra_params`` value — a routing surface that leaked one would be
    handing out deployment topology to anyone who can read it.
    """
    models = [
        PRIVATE_DEFAULT.model_copy(
            update={"extra_params": {"api_key": SecretStr("sk-live-SECRET")}}
        ),
        AUX_MODEL,
        FRONTIER,
    ]
    harness = make_routing_app(make_app, gateway_models=models)

    async def flow() -> None:
        async with harness.client() as client:
            resp = await client.get(
                "/api/v1/models/routing", headers=harness.bearer("user:ops-olga", groups=("ops",))
            )
            assert resp.status_code == 200, resp.text
            body = resp.text
            assert "sk-live-SECRET" not in body
            assert "vllm:8000" not in body
            assert "http://" not in body
            # CONTROL: the aliases the console legitimately needs ARE there.
            assert "private-default" in body and "aux-model" in body

    run(flow())


# --- a narrower target (no floor since NE-CTX-IN-FORCE) ----------------------


def test_a_smaller_window_target_is_offered_and_accepted(
    make_app: AppBuilder,
) -> None:
    """NE-CTX-IN-FORCE — the inverse of the rule this test used to pin, end to end.

    There WAS a budget floor here: a target whose input budget fell below the boot
    primary's was marked unselectable and its POST answered 409, because
    ``RuntimeSettings.context_budget`` was derived from that primary once at boot
    and "cannot be re-derived live". It can now — ``GatewayModel._turn_budget``
    asks the gateway for the in-force model's budget on every request — so routing
    DOWN to a narrower model is an ordinary governed repoint: offered in the
    catalog, accepted by the endpoint, and enforced afterwards by a trim guard
    sized to the narrower window.

    The old cure was worse than the disease: to keep any target legal, every lane
    had to declare the NARROWEST lane's window, which is how a 1M-window frontier
    model came to serve 18841 input tokens.

    ``aux-model`` (equal window) stays in the payload as the control that this
    endpoint still distinguishes targets at all. The narrower target is
    ``narrow-fit`` (8192): narrower than the boot primary, which is exactly the
    property the deleted FLOOR would have refused.
    """
    harness = make_routing_app(make_app)

    async def flow() -> None:
        async with harness.client() as client:
            ops = harness.bearer("user:ops-olga", groups=("ops",))
            catalog = await client.get("/api/v1/models/routing", headers=ops)
            row = next(r for r in catalog.json()["tasks"] if r["task"] == "agent")
            by_model = {t["model"]: t for t in row["targets"]}
            assert by_model["narrow-fit"]["selectable"] is True
            assert by_model["narrow-fit"]["reason"] is None
            assert by_model["aux-model"]["selectable"] is True
            assert by_model["aux-model"]["reason"] is None

            resp = await client.post(
                "/api/v1/models/routing",
                json={"task": "agent", "model": "narrow-fit"},
                headers=ops,
            )
            assert resp.status_code != 409, resp.text

    run(flow())


def test_a_target_the_current_snapshot_overflows_is_disabled_in_both_directions() -> None:
    """SPEC PRE-DEMO-REHEARSAL-014: "A model
    whose context window cannot safely accept the current snapshot is
    absent/disabled unless a real governed compaction operation exists. ...
    There is no silent truncation, new Conversation, cache-reset disguise or
    fallback."

    THE FINDING (round 7): the offer/refuse judgement never consulted the
    CURRENT conversation snapshot, so a conversation grown on the wide in-force
    window was offered a switch to a narrow target that could not accept it.
    This test FAILS on 58c294d3: ``snapshot_tokens`` does not exist there — the
    rule cannot even be told about the snapshot, and the same target is offered
    unconditionally. No
    governed compaction operation exists in this deployment, so the SPEC's
    stated remedy is the disable, with a counts-only reason (C12), in BOTH
    directions of the one shared rule.
    """
    boot = GatewaySettings(models=MODELS, tasks=dict(TASKS))
    narrow_budget = boot.context_budget_for(NARROW_FIT).input_budget
    over = narrow_budget + 1

    refusal = refusal_for_selection(boot, "agent", NARROW_FIT.name, snapshot_tokens=over)
    assert refusal is not None
    assert f"snapshot_tokens={over}" in refusal
    assert f"input_budget={narrow_budget}" in refusal
    assert "compaction" in refusal
    # C12: counts only — no message or turn text can ride the refusal.
    assert "TURN" not in refusal and "prompt:" not in refusal

    routing = refusal_for_target(
        boot,
        "agent",
        NARROW_FIT.name,
        current=PRIVATE_DEFAULT.name,
        snapshot_tokens=over,
    )
    assert routing is not None and f"snapshot_tokens={over}" in routing

    # CONTROL 1 — a snapshot the target CAN accept stays offered in BOTH
    # directions: a per-conversation fit rule, not a blanket refusal and not
    # the deleted budget-width floor returning.
    assert (
        refusal_for_selection(boot, "agent", NARROW_FIT.name, snapshot_tokens=narrow_budget) is None
    )
    assert (
        refusal_for_target(
            boot,
            "agent",
            NARROW_FIT.name,
            current=PRIVATE_DEFAULT.name,
            snapshot_tokens=narrow_budget,
        )
        is None
    )

    # CONTROL 2 — no snapshot data (every surface with no conversation in
    # hand): no fit check, the honest reading, never a fabricated zero.
    assert refusal_for_selection(boot, "agent", NARROW_FIT.name) is None

    # CONTROL 3 — the same over-sized snapshot still fits a WIDE target: the
    # gate measures the target's own window, nothing else.
    assert refusal_for_selection(boot, "agent", FRONTIER.name, snapshot_tokens=over) is None


def test_a_repoint_target_the_largest_live_snapshot_overflows_is_refused_by_name(
    tmp_path: Any,
) -> None:
    """NE-CTX-IN-FORCE round 9 — the SPEC PRE-DEMO-014 snapshot gate reaches the
    REPOINT surface. FAILS on 04b88cb2: ``ModelRouteStore`` takes no
    ``snapshot_tokens_provider`` there, and its ``apply``/catalog pass no
    ``snapshot_tokens`` at all, so an operator repointing a task from a wide
    primary to a narrower target while a live conversation's snapshot exceeds
    the target was approved — and the next turn trimmed history silently.

    A task repoint governs every conversation on the task and names no single
    snapshot, so the gate measures the target against the LARGEST live
    conversation snapshot (the store's provider — in production the max over
    the newest boundary receipt per conversation, the same durable counts the
    round-7 selection gate reads). Refused BY NAME, counts-only (C12), BEFORE
    any durable write: no override row and no chain record may exist after the
    refusal. ``None`` still means "no live conversation" and skips the check;
    a FAILING provider raises (fail closed) — never "unknown ⇒ allowed".
    """
    from ne.api.model_routing import RouteChangeRefused, routing_catalog

    engine = sa.create_engine(f"sqlite:///{tmp_path}/routing-snapshot-gate.db")
    ensure_audit_schema(engine)
    ensure_model_route_schema(engine)
    boot = GatewaySettings(models=MODELS, tasks=dict(TASKS))
    # Chain records need the frozen process hash (the conftest autouse fixture
    # resets it around every test).
    freeze_config_hash(boot)
    narrow_budget = boot.context_budget_for(NARROW_FIT).input_budget
    over = narrow_budget + 1
    largest: dict[str, int | None] = {"agent": over}

    store = ModelRouteStore(
        engine,
        boot,
        lambda: False,
        snapshot_tokens_provider=largest.get,
    )

    # OFFER surface: the over-snapshot target is disabled with the counts-only
    # reason, and a wide target judged against the SAME snapshot stays offered.
    [row] = [r for r in routing_catalog(store) if r.task == "agent"]
    by_model = {t.model: t for t in row.targets}
    assert by_model[NARROW_FIT.name].selectable is False
    reason = by_model[NARROW_FIT.name].reason
    assert reason is not None and f"snapshot_tokens={over}" in reason
    assert f"input_budget={narrow_budget}" in reason
    assert by_model[FRONTIER.name].selectable is True

    # APPLY surface: refused by name, BEFORE any durable write.
    with pytest.raises(RouteChangeRefused, match="narrow-fit"):
        store.apply(
            task="agent",
            model=NARROW_FIT.name,
            actor="user:ops-olga",
            approver="user:approver-ada",
            approval_id="apr-snapshot-gate",
            via_action=ROUTE_INTERNAL_ACTION,
        )
    with engine.connect() as conn:
        assert (
            conn.execute(sa.select(sa.func.count()).select_from(ne_model_route_overrides)).scalar()
            == 0
        ), "a refused repoint must write no override row"
        assert (
            conn.execute(
                sa.select(sa.func.count())
                .select_from(ne_audit_log)
                .where(ne_audit_log.c.action == MODEL_ROUTING_CHANGED_ACTION)
            ).scalar()
            == 0
        ), "a refused repoint must append no model.routing.changed record"

    # CONTROL 1 — a snapshot the target holds: the repoint is an ordinary
    # governed apply (down-routing stays the point of the floor's removal).
    largest["agent"] = narrow_budget
    store.apply(
        task="agent",
        model=NARROW_FIT.name,
        actor="user:ops-olga",
        approver="user:approver-ada",
        approval_id="apr-snapshot-fits",
        via_action=ROUTE_INTERNAL_ACTION,
    )
    assert build_route_provider(store)()["agent"].model == NARROW_FIT.name

    # CONTROL 2 — None means NO live conversation, and only that: no check.
    largest["agent"] = None
    [row] = [r for r in routing_catalog(store) if r.task == "agent"]
    assert {t.model: t.selectable for t in row.targets}[AUX_MODEL.name] is True

    # CONTROL 3 — a FAILING snapshot authority refuses loudly (fail closed);
    # it is never read as "unknown ⇒ allowed".
    def _broken(task: str) -> int | None:
        raise RuntimeError("snapshot authority unreadable")

    broken_store = ModelRouteStore(engine, boot, lambda: False, snapshot_tokens_provider=_broken)
    with pytest.raises(RuntimeError, match="snapshot authority unreadable"):
        broken_store.apply(
            task="agent",
            model=AUX_MODEL.name,
            actor="user:ops-olga",
            approver="user:approver-ada",
            approval_id="apr-broken-authority",
            via_action=ROUTE_INTERNAL_ACTION,
        )
    engine.dispose()


def test_the_current_model_is_not_offered_as_a_target(make_app: AppBuilder) -> None:
    """A no-op must never mint an approval row: the model already in force is
    marked unselectable, and submitting it is a 409."""
    harness = make_routing_app(make_app)

    async def flow() -> None:
        async with harness.client() as client:
            ops = harness.bearer("user:ops-olga", groups=("ops",))
            catalog = await client.get("/api/v1/models/routing", headers=ops)
            row = next(r for r in catalog.json()["tasks"] if r["task"] == "agent")
            current = next(t for t in row["targets"] if t["model"] == "private-default")
            assert current["selectable"] is False
            resp = await client.post(
                "/api/v1/models/routing",
                json={"task": "agent", "model": "private-default"},
                headers=ops,
            )
            assert resp.status_code == 409, resp.text

    run(flow())
    assert parked_count(harness) == 0


# --- the air gap ------------------------------------------------------------


def test_a_frontier_target_is_unselectable_while_the_air_gap_is_engaged(
    make_app: AppBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Legibility over enforcement, and the enforcement is elsewhere. With the
    gap engaged, the catalog marks the frontier target unselectable in the
    server's own words and the submit is a 409 — so the console never offers a
    swap the egress guard would refuse.

    The CONTROL is the same payload with the gap disengaged, where the same
    target IS selectable — so this test distinguishes the posture, not the
    frontier lane's mere existence.
    """
    monkeypatch.setenv("NE_EGRESS_ALLOWED_FRONTIER_HOSTS", json.dumps(["api.anthropic.com"]))
    monkeypatch.delenv("NE_EGRESS_AIR_GAP", raising=False)
    monkeypatch.delenv("AIR_GAP", raising=False)
    harness = make_routing_app(make_app)

    async def flow() -> None:
        async with harness.client() as client:
            ops = harness.bearer("user:ops-olga", groups=("ops",))

            # CONTROL — gap open: the frontier target is on offer.
            catalog = await client.get("/api/v1/models/routing", headers=ops)
            row = next(r for r in catalog.json()["tasks"] if r["task"] == "agent")
            frontier = next(t for t in row["targets"] if t["model"] == "frontier-best")
            assert frontier["selectable"] is True
            assert frontier["action"] == ROUTE_EXTERNAL_ACTION

            engaged = await client.post(
                "/api/v1/network/air-gap", json={"air_gap": True}, headers=ops
            )
            assert engaged.status_code == 200, engaged.text

            catalog2 = await client.get("/api/v1/models/routing", headers=ops)
            row2 = next(r for r in catalog2.json()["tasks"] if r["task"] == "agent")
            frontier2 = next(t for t in row2["targets"] if t["model"] == "frontier-best")
            assert frontier2["selectable"] is False
            assert "air gap" in (frontier2["reason"] or "")
            # The in-VPC target is untouched by the gap — the open-weight lane
            # stays alive (SPEC C2).
            assert next(t for t in row2["targets"] if t["model"] == "aux-model")["selectable"]

            resp = await client.post(
                "/api/v1/models/routing",
                json={"task": "agent", "model": "frontier-best"},
                headers=ops,
            )
            assert resp.status_code == 409, resp.text

    run(flow())


# --- authorization -----------------------------------------------------------


def test_an_unauthorized_principal_cannot_change_a_route_and_the_refusal_is_audited(
    make_app: AppBuilder,
) -> None:
    """403 + an audited denial, and — the decisive half — nothing was submitted:
    the role check runs BEFORE the register or the executor is touched."""
    harness = make_routing_app(make_app)

    async def flow() -> None:
        async with harness.client() as client:
            resp = await client.post(
                "/api/v1/models/routing",
                json={"task": "agent", "model": "aux-model"},
                headers=harness.bearer("user:mallory", groups=("readers",)),
            )
            assert resp.status_code == 403, resp.text
            read = await client.get(
                "/api/v1/models/routing",
                headers=harness.bearer("user:mallory", groups=("readers",)),
            )
            assert read.status_code == 403

    run(flow())
    denials = chain_events(harness, "api.models.routing.unauthorized")
    assert len(denials) == 2
    assert all(event.outcome == "denied" for event in denials)
    assert all(event.actor == "user:mallory" for event in denials)
    assert parked_count(harness) == 0
    assert override_rows(harness) == []


def test_empty_ops_roles_refuses_everyone(make_app: AppBuilder) -> None:
    """Fail closed: an unconfigured role tuple is not an open door."""
    harness = make_app(
        ops_roles=(), approver_roles=("approvers",), gateway_models=MODELS, gateway_tasks=TASKS
    )

    async def flow() -> None:
        async with harness.client() as client:
            resp = await client.post(
                "/api/v1/models/routing",
                json={"task": "agent", "model": "aux-model"},
                headers=harness.bearer("user:ops-olga", groups=("ops",)),
            )
            assert resp.status_code == 403, resp.text

    run(flow())
    assert override_rows(harness) == []


# --- the provider fails closed ----------------------------------------------


def test_the_route_provider_fails_closed_to_the_boot_route_not_the_last_seen_one(
    make_app: AppBuilder,
) -> None:
    """An unreadable register must yield the BOOT route — the deployment's own
    assignment, the one the frozen ``config_hash`` attests.

    THE OVERRIDE IS APPLIED FIRST, DELIBERATELY. Asserting "boot route" against
    a store that never held an override would also pass for a provider that
    cached and replayed its last successful read — the plausible wrong design.
    With ``aux-model`` genuinely in force at the moment the store breaks, the
    two behaviours give different answers and this test can tell them apart.

    The control is the read immediately before the break, which must show the
    override actually took effect.
    """
    harness = make_routing_app(make_app)
    state = get_state(harness.app)
    store = state.route_store
    assert store is not None
    provider = build_route_provider(store)
    assert provider()["agent"].model == "private-default"

    async def flow() -> None:
        async with harness.client() as client:
            resp = await client.post(
                "/api/v1/models/routing",
                json={"task": "agent", "model": "aux-model"},
                headers=harness.bearer("user:ops-olga", groups=("ops",)),
            )
            assert resp.status_code == 200, resp.text
            approve = await client.post(
                f"/api/v1/approvals/{resp.json()['request_id']}/approve",
                json={},
                headers=harness.bearer("user:approver-ada", groups=("approvers",)),
            )
            assert approve.status_code == 200, approve.text
            assert await wait_until(lambda: provider()["agent"].model == "aux-model")

    run(flow())

    # CONTROL: the override IS in force through a readable store.
    assert provider()["agent"].model == "aux-model"

    class Broken:
        def __getattr__(self, name: str) -> Any:
            raise OperationalError("select 1", {}, Exception("register unreadable"))

    original = store._engine  # type: ignore[attr-defined]
    store._engine = Broken()  # type: ignore[attr-defined]
    try:
        routes = provider()
    finally:
        store._engine = original  # type: ignore[attr-defined]

    assert routes["agent"].model == "private-default", (
        "an unreadable route register must fail closed to the BOOT route — not "
        "to the override it last managed to read"
    )
    # And the store is readable again, so the honest answer returns.
    assert provider()["agent"].model == "aux-model"


# --- config_hash -------------------------------------------------------------


def test_a_route_change_does_not_move_the_frozen_config_hash(make_app: AppBuilder) -> None:
    """§3. The runtime route is deliberately outside the frozen hash — the same
    decision, for the same reason, as the egress posture. A hash that moved
    under a running process would be worse than one that never claimed to cover
    runtime state."""
    from ne.contracts import get_config_hash

    harness = make_routing_app(make_app)
    before = get_config_hash()

    async def flow() -> None:
        async with harness.client() as client:
            ops = harness.bearer("user:ops-olga", groups=("ops",))
            resp = await client.post(
                "/api/v1/models/routing",
                json={"task": "agent", "model": "aux-model"},
                headers=ops,
            )
            assert resp.status_code == 200, resp.text
            request_id = resp.json()["request_id"]
            approve = await client.post(
                f"/api/v1/approvals/{request_id}/approve",
                json={},
                headers=harness.bearer("user:approver-ada", groups=("approvers",)),
            )
            assert approve.status_code == 200

    run(flow())
    assert get_config_hash() == before
