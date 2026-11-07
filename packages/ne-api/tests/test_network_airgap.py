# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-AIRGAP-TOGGLE — the governed runtime air-gap toggle, end to end.

Every guard here is BIDIRECTIONAL per docs/testing/vacuous-guards.md: the flip
is asserted by what it does to the REAL enforcement object (``EgressPolicy``
over the platform's own provider raising ``AirGapViolationError``), never by
reading a settings value alone; each blocking assertion sits next to a
passing control on the same object; and the chain/actor/verify_chain claims are
asserted against the real audit store, not a fake sink.

The whole flow of a test runs inside ONE ``run(...)`` coroutine: the Tier-2
expand waiter is an asyncio task on that loop (the NE-WORKFLOWS same-session
decision discipline — in production the server loop is long-lived).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
import sqlalchemy as sa

from ne.audit.store import ne_audit_log
from ne.audit.verify import verify_chain
from ne.contracts import Lane
from ne.gateway.config import ModelDef
from ne.gateway.egress import EgressPolicy
from ne.gateway.errors import AirGapViolationError

from .conftest import AppBuilder, Harness, get_state, run

FRONTIER_HOST = "api.openai.com"

RESTRICT = "write.internal.network.egress.restrict"
EXPAND = "write.external.network.egress.expand"


def frontier_model(name: str = "frontier-best") -> ModelDef:
    return ModelDef(
        name=name,
        litellm_model="openai/gpt-x",
        provider="openai",
        lane=Lane.FRONTIER_API,
        context_window=32768,
    )


@pytest.fixture
def allowlisted_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Boot the platform with the frontier host allowlisted and air gap OFF —
    the demo rig's ``--frontier`` posture (the model works until the flip)."""
    monkeypatch.setenv("NE_EGRESS_ALLOWED_FRONTIER_HOSTS", json.dumps([FRONTIER_HOST]))
    monkeypatch.delenv("NE_EGRESS_AIR_GAP", raising=False)
    monkeypatch.delenv("AIR_GAP", raising=False)


def make_toggle_app(make_app: AppBuilder) -> Harness:
    return make_app(ops_roles=("ops",), approver_roles=("approvers",))


def guard_of(app: Any) -> EgressPolicy:
    """The REAL enforcement object over the platform's OWN provider — the same
    read the production gateway guard performs per call."""
    provider = get_state(app).egress_settings_provider
    assert provider is not None
    return EgressPolicy(settings_provider=provider)


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


async def wait_until(predicate: Any, *, timeout_s: float = 20.0) -> bool:
    """Poll ``predicate`` with a deadline (loaded-box discipline: never assert
    immediately after an async wait — poll until true or timed out)."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return bool(predicate())


# --- the demo beat, both directions ---------------------------------------


def test_engage_air_gap_applies_immediately_and_blocks_the_frontier_call(
    allowlisted_env: None, make_app: AppBuilder
) -> None:
    harness = make_toggle_app(make_app)
    policy = guard_of(harness.app)

    async def flow() -> None:
        async with harness.client() as client:
            # CONTROL — before the flip the allowlisted frontier call passes the
            # real guard (the "model working" half of the beat).
            policy(frontier_model())  # no raise

            resp = await client.post(
                "/api/v1/network/air-gap",
                json={"air_gap": True},
                headers=harness.bearer("user:ops-olga", groups=("ops",)),
            )
            assert resp.status_code == 200, resp.text
            assert resp.json() == {"status": "applied", "request_id": None}

            # The SAME call is now refused by the SAME enforcement object.
            with pytest.raises(AirGapViolationError):
                policy(frontier_model())

            # C7: the posture read reflects the new state (read-back, not cache).
            posture = await client.get("/api/v1/posture", headers=harness.bearer("user:any"))
            assert posture.status_code == 200
            body = posture.json()
            assert body["air_gap"] is True
            assert body["egress_allowlist"] == []
            assert body["air_gap_pending"] is None

    run(flow())

    # C10: the flip is on the chain, with the actor, old -> new.
    events = chain_events(harness, "network.egress.changed")
    assert len(events) == 1
    row = events[0]
    assert row.actor == "user:ops-olga"
    detail = json.loads(row.payload["detail"])
    assert detail == {"air_gap": {"old": False, "new": True}}
    assert row.payload["provenance"]["changed_via"] == RESTRICT
    # Tier asymmetry, restrict side: applied with NO approval row parked.
    engine = sa.create_engine(harness.runtime_url)
    try:
        with engine.connect() as conn:
            parked = conn.execute(sa.text("SELECT COUNT(*) FROM ne_approval_requests")).scalar()
    finally:
        engine.dispose()
    assert parked == 0
    # The chain survives verification across the flip.
    audit_engine = sa.create_engine(harness.audit_url)
    try:
        verification = verify_chain(audit_engine)
    finally:
        audit_engine.dispose()
    assert verification.valid, verification.reason


def test_disengage_parks_for_approval_then_applies_on_grant(
    allowlisted_env: None, make_app: AppBuilder
) -> None:
    harness = make_toggle_app(make_app)
    policy = guard_of(harness.app)
    state = get_state(harness.app)
    provider = state.egress_settings_provider
    assert provider is not None

    async def flow() -> None:
        async with harness.client() as client:
            ops = harness.bearer("user:ops-olga", groups=("ops",))
            # Engage first (proven applied in the sibling test).
            engaged = await client.post(
                "/api/v1/network/air-gap", json={"air_gap": True}, headers=ops
            )
            assert engaged.status_code == 200

            # Disengage: Tier-2 — parks, answers pending with the durable row id.
            resp = await client.post(
                "/api/v1/network/air-gap", json={"air_gap": False}, headers=ops
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["status"] == "pending"
            request_id = body["request_id"]
            assert request_id

            # §2.5: pending renders on the object via the posture read-back, and
            # the DISPLAYED posture remains the in-force one (still engaged).
            posture = (
                await client.get("/api/v1/posture", headers=harness.bearer("user:any"))
            ).json()
            assert posture["air_gap"] is True
            assert posture["air_gap_pending"] == {
                "request_id": request_id,
                "requested_by": "user:ops-olga",
            }
            # Guard still refuses while parked — the change is NOT pre-applied.
            with pytest.raises(AirGapViolationError):
                policy(frontier_model())

            # A second identical submission reattaches to the SAME pending row —
            # no duplicate approval is minted.
            again = await client.post(
                "/api/v1/network/air-gap", json={"air_gap": False}, headers=ops
            )
            assert again.status_code == 200
            assert again.json() == {"status": "pending", "request_id": request_id}

            # A human approves (distinct principal, approver role).
            approve = await client.post(
                f"/api/v1/approvals/{request_id}/approve",
                json={},
                headers=harness.bearer("user:approver-ada", groups=("approvers",)),
            )
            assert approve.status_code == 200, approve.text
            assert approve.json()["status"] == "approved"

            # The parked waiter applies the change: the provider (the guard's own
            # read) goes loose within the poll window.
            applied = await wait_until(lambda: provider().air_gap is False)
            assert applied, "approved disengage was never applied by the waiter"

            # CONTROL — the same frontier call now passes the same guard.
            policy(frontier_model())  # no raise

            posture2 = (
                await client.get("/api/v1/posture", headers=harness.bearer("user:any"))
            ).json()
            assert posture2["air_gap"] is False
            assert posture2["egress_allowlist"] == [FRONTIER_HOST]
            assert posture2["air_gap_pending"] is None

    run(flow())

    # C10 both directions: engage AND disengage are on the chain with actors.
    events = chain_events(harness, "network.egress.changed")
    directions = [
        (
            json.loads(r.payload["detail"])["air_gap"]["old"],
            json.loads(r.payload["detail"])["air_gap"]["new"],
            r.actor,
        )
        for r in events
    ]
    assert directions == [
        (False, True, "user:ops-olga"),
        (True, False, "user:ops-olga"),
    ]
    # The approval trail exists and joins: requested + granted for this action.
    granted = chain_events(harness, "approval.granted")
    assert len(granted) == 1
    assert granted[0].payload["provenance"]["approver"] == "user:approver-ada"
    executed = [r for r in chain_events(harness, "tool.execute") if r.resource == f"tool:{EXPAND}"]
    assert len(executed) == 1
    assert executed[0].outcome == "success"
    assert executed[0].actor == "user:ops-olga"
    audit_engine = sa.create_engine(harness.audit_url)
    try:
        verification = verify_chain(audit_engine)
    finally:
        audit_engine.dispose()
    assert verification.valid, verification.reason


# --- refusals --------------------------------------------------------------


def test_requester_holding_the_approver_role_cannot_lift_its_own_air_gap(
    allowlisted_env: None, make_app: AppBuilder
) -> None:
    """The claim ``docs/ops/air-gap-recovery.md`` §2 step 1 makes, tested.

    NOT vacuous: ``user:ops-olga`` here holds ``ops`` AND ``approvers``, so the
    fail-closed ``_require_approver`` role gate PASSES for it — which is exactly
    what the e2e S5 assertion could never distinguish (the live realm's ``ops``
    user is simply not in ``/approver``, so its 403 came from
    ``ne.api.authz.require_role``, never from any requester-identity rule).
    The only thing that can refuse this call is separation of duties.

    Bidirectional: the SAME lift, submitted the SAME way, is granted on the very
    next call by a DISTINCT approver, and only then does the posture move.
    """

    harness = make_toggle_app(make_app)
    policy = guard_of(harness.app)
    state = get_state(harness.app)
    provider = state.egress_settings_provider
    assert provider is not None

    async def flow() -> None:
        async with harness.client() as client:
            # BOTH capabilities on one principal — the demo box's role split
            # is an assignment, not a rule; this is the principal that rule is for.
            both = harness.bearer("user:ops-olga", groups=("ops", "approvers"))
            engaged = await client.post(
                "/api/v1/network/air-gap", json={"air_gap": True}, headers=both
            )
            assert engaged.status_code == 200
            assert provider().air_gap is True

            parked = await client.post(
                "/api/v1/network/air-gap", json={"air_gap": False}, headers=both
            )
            assert parked.status_code == 200, parked.text
            request_id = parked.json()["request_id"]
            assert request_id

            own = await client.post(
                f"/api/v1/approvals/{request_id}/approve", json={}, headers=both
            )
            assert own.status_code == 403, own.text
            assert "separation of duties" in own.json()["detail"], own.text

            # The refusal DECIDED NOTHING: the gap is still engaged, the guard
            # still refuses, and the row is still there to be decided properly.
            assert provider().air_gap is True
            with pytest.raises(AirGapViolationError):
                policy(frontier_model())

            # CONTROL — a DISTINCT approver, same row, same instant: granted.
            granted = await client.post(
                f"/api/v1/approvals/{request_id}/approve",
                json={},
                headers=harness.bearer("user:approver-ada", groups=("approvers",)),
            )
            assert granted.status_code == 200, granted.text
            assert await wait_until(lambda: provider().air_gap is False)
            policy(frontier_model())  # no raise

    run(flow())

    # The refusal is AUDITED as an authorization denial naming its own reason,
    # and the posture chain carries exactly the engage + the granted lift.
    denials = [
        r
        for r in chain_events(harness, "api.approval.unauthorized")
        if json.loads(r.payload["detail"]).get("reason") == "requester-may-not-decide"
    ]
    assert len(denials) == 1, [r.payload["detail"] for r in denials]
    assert denials[0].actor == "user:ops-olga"
    assert denials[0].outcome == "denied"
    granted_events = chain_events(harness, "approval.granted")
    assert [e.payload["provenance"]["approver"] for e in granted_events] == ["user:approver-ada"]
    flips = [
        (
            json.loads(r.payload["detail"])["air_gap"]["old"],
            json.loads(r.payload["detail"])["air_gap"]["new"],
        )
        for r in chain_events(harness, "network.egress.changed")
    ]
    assert flips == [(False, True), (True, False)]


def test_unauthorized_principal_is_refused_and_the_refusal_is_audited(
    allowlisted_env: None, make_app: AppBuilder
) -> None:
    harness = make_toggle_app(make_app)

    async def flow() -> None:
        async with harness.client() as client:
            resp = await client.post(
                "/api/v1/network/air-gap",
                json={"air_gap": True},
                headers=harness.bearer("user:mallory", groups=("employees",)),
            )
            assert resp.status_code == 403
            # CONTROL — the same request from an ops principal is not refused.
            ok = await client.post(
                "/api/v1/network/air-gap",
                json={"air_gap": True},
                headers=harness.bearer("user:ops-olga", groups=("ops",)),
            )
            assert ok.status_code == 200

    run(flow())

    refusals = chain_events(harness, "api.network.airgap.unauthorized")
    assert len(refusals) == 1
    assert refusals[0].actor == "user:mallory"
    assert refusals[0].outcome == "denied"
    # The refusal changed nothing: no flip event for mallory, exactly one for olga.
    flips = chain_events(harness, "network.egress.changed")
    assert [r.actor for r in flips] == ["user:ops-olga"]


def test_noop_submission_is_a_409_and_mints_nothing(
    allowlisted_env: None, make_app: AppBuilder
) -> None:
    harness = make_toggle_app(make_app)

    async def flow() -> None:
        async with harness.client() as client:
            ops = harness.bearer("user:ops-olga", groups=("ops",))
            # air gap is OFF at boot: disengaging is a no-op conflict...
            off = await client.post("/api/v1/network/air-gap", json={"air_gap": False}, headers=ops)
            assert off.status_code == 409
            assert "already disengaged" in off.json()["detail"]
            # ...and after engaging, engaging again conflicts too (bidirectional).
            assert (
                await client.post("/api/v1/network/air-gap", json={"air_gap": True}, headers=ops)
            ).status_code == 200
            on = await client.post("/api/v1/network/air-gap", json={"air_gap": True}, headers=ops)
            assert on.status_code == 409
            assert "already engaged" in on.json()["detail"]

    run(flow())

    # No approval rows, exactly ONE chain flip (the real engage between the 409s).
    assert len(chain_events(harness, "network.egress.changed")) == 1


# --- fail-closed -----------------------------------------------------------


def test_failed_loosen_apply_leaves_the_air_gap_engaged(
    allowlisted_env: None, make_app: AppBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A disengage whose apply transaction fails (chain append raises) must leave
    the deployment in the MORE restrictive state — and the failure is loud."""
    harness = make_toggle_app(make_app)
    policy = guard_of(harness.app)
    state = get_state(harness.app)
    provider = state.egress_settings_provider
    assert provider is not None

    async def flow() -> None:
        async with harness.client() as client:
            ops = harness.bearer("user:ops-olga", groups=("ops",))
            assert (
                await client.post("/api/v1/network/air-gap", json={"air_gap": True}, headers=ops)
            ).status_code == 200

            # Break the chain append INSIDE the apply transaction.
            import ne.api.network_egress as mod

            def broken_append(conn: Any, event: Any) -> Any:
                raise RuntimeError("chain append unavailable")

            monkeypatch.setattr(mod, "append_record", broken_append)

            resp = await client.post(
                "/api/v1/network/air-gap", json={"air_gap": False}, headers=ops
            )
            assert resp.json()["status"] == "pending"
            request_id = resp.json()["request_id"]
            approve = await client.post(
                f"/api/v1/approvals/{request_id}/approve",
                json={},
                headers=harness.bearer("user:approver-ada", groups=("approvers",)),
            )
            assert approve.status_code == 200

            # The waiter ran and FAILED: the posture must still be engaged.
            executed_error = await wait_until(
                lambda: any(
                    r.outcome == "error"
                    for r in chain_events(harness, "tool.execute")
                    if r.resource == f"tool:{EXPAND}"
                )
            )
            assert executed_error, "the failed apply never surfaced as tool.execute[error]"
            assert provider().air_gap is True
            with pytest.raises(AirGapViolationError):
                policy(frontier_model())

            # CONTROL — with the append restored, a fresh request applies.
            monkeypatch.undo()
            resp2 = await client.post(
                "/api/v1/network/air-gap", json={"air_gap": False}, headers=ops
            )
            request_id2 = resp2.json()["request_id"]
            assert request_id2 and request_id2 != request_id
            assert (
                await client.post(
                    f"/api/v1/approvals/{request_id2}/approve",
                    json={},
                    headers=harness.bearer("user:approver-ada", groups=("approvers",)),
                )
            ).status_code == 200
            applied = await wait_until(lambda: provider().air_gap is False)
            assert applied
            policy(frontier_model())  # no raise

    run(flow())


def test_unreadable_posture_store_fails_closed_to_air_gap(
    allowlisted_env: None, make_app: AppBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = make_toggle_app(make_app)
    policy = guard_of(harness.app)
    state = get_state(harness.app)
    provider = state.egress_settings_provider
    assert provider is not None

    # CONTROL — readable store, boot posture: loose, frontier passes.
    assert provider().air_gap is False
    policy(frontier_model())

    from ne.api.network_egress import EgressPostureStore

    def broken_read(self: Any) -> Any:
        raise RuntimeError("posture store unreachable")

    monkeypatch.setattr(EgressPostureStore, "read_state", broken_read)
    settings = provider()
    assert settings.air_gap is True
    assert settings.allowed_frontier_hosts == ()
    with pytest.raises(AirGapViolationError):
        policy(frontier_model())


# --- the config_hash decision, pinned --------------------------------------


def test_flip_does_not_move_config_hash_and_the_chain_carries_the_timeline(
    allowlisted_env: None, make_app: AppBuilder
) -> None:
    """The runtime posture lives OUTSIDE the frozen config_hash (the platform's
    pre-existing, deliberate design: EgressSettings is not stamped into
    extra_modules). The honest timeline is the chain: the flip event carries the
    SAME frozen hash (no new history span appears), while the posture read
    provably changed — so the hash-stability assertion is measured against a
    co-observed state change, not vacuously green."""
    harness = make_toggle_app(make_app)
    state = get_state(harness.app)
    hash_before = state.config_hash

    async def flow() -> None:
        async with harness.client() as client:
            before = (
                await client.get("/api/v1/posture", headers=harness.bearer("user:any"))
            ).json()
            assert before["air_gap"] is False
            resp = await client.post(
                "/api/v1/network/air-gap",
                json={"air_gap": True},
                headers=harness.bearer("user:ops-olga", groups=("ops",)),
            )
            assert resp.status_code == 200
            after = (await client.get("/api/v1/posture", headers=harness.bearer("user:any"))).json()
            # The co-observed change (the control that this test measures a real
            # flip): posture moved...
            assert after["air_gap"] is True
            # ...while the attested configuration hash did not, and the history
            # gained no second span.
            assert after["config_hash"] == hash_before
            assert {span["hash"] for span in after["config_hash_history"]} == {hash_before}

    run(flow())

    # The flip's own chain record is stamped with the SAME frozen hash — the
    # timeline lives on the chain, under the hash in force.
    events = chain_events(harness, "network.egress.changed")
    assert len(events) == 1
    assert events[0].config_hash == hash_before
