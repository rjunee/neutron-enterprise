# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Approvals HITL surface — the REAL DurableApprovalGate over SQLite (spec §5.3).

The governed spine is NEVER mocked: every route drives the real gate. Proves the
role pre-check (empty roles ⇒ fail closed, audited), the empty-body approver
smuggle → 422 with the gate row untouched, the full Tier-3 distinct-second flow
with the exact policy substring served verbatim, 404/409 mapping, and the
redacted api.approval.submitted surface record.

That claim used to be false for the two RUN-PARKED tests in this file: they
stubbed ``state.gate._run_approval_checkpoints`` and set ``state.source_guard``
to a bare ``SimpleNamespace``, which is why a wholly broken approve path
(RETRYABLE_UNKNOWN → 409 on every run-parked decision) shipped with green CI on
2026-08-28. Both now park a REAL run through
``run_approval_park.park_run`` — the real workflow, the real
``RunApprovalCheckpointStore``, the real ``ProtectedSourceResultGuard`` — and
decide it over the real HTTP route. Nothing in this file stubs a governance
chokepoint; the only double left is the DBOS enqueue transport.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import sqlalchemy as sa

from ne.audit import list_records
from ne.contracts.governance import ApprovalRequest
from ne.contracts.source_authority import (
    CurrentSourceAuthorityReceipt,
    M365SourcePoint,
    ProtectedSourceInvocationRef,
    SourceAuthorityStamp,
    SourceAuthorityState,
    SourceContentArtifactIdentity,
    SourceContractIdentity,
    SourceIdentity,
)
from ne.contracts.types import ApprovalTier, Principal
from ne.runtime.approval_store import ne_approval_requests
from ne.runtime.approvals import PROTECTED_INVOCATION_KEY
from ne.runtime.config import ApprovalSettings
from ne.runtime.run_checkpoint import ne_agent_run_continuation_checkpoints
from ne.runtime.source_results import encode_protected_reference

from .conftest import AppBuilder, get_state, run
from .run_approval_park import (
    APPROVER,
    APPROVER_GROUP,
    OWNER,
    RecordingEnqueuer,
    admit_sources,
    park_run,
)

#: The Tier-2 external effect the run-parked tests park on.
RUN_EFFECT_TOOL = "write.external.email.send"
RUN_ARGUMENTS: dict[str, object] = {"recipient": "cfo@example.test", "subject": "Q3 close"}


def _submit(h, *, tier: ApprovalTier, action: str = "write.external.demo.send") -> str:
    state = get_state(h.app)
    return run(
        state.gate.submit(
            ApprovalRequest(
                tier=tier,
                action=action,
                principal=Principal(id="user:req", groups=(), claims={}),
                payload={},
            )
        )
    )


def test_pending_requires_approver_role(make_app: AppBuilder) -> None:
    h = make_app(approver_roles=("approvers",))

    async def _t() -> None:
        async with h.client() as client:
            ok = h.bearer("user:app", groups=["approvers"])
            r = await client.get("/api/v1/approvals/pending", headers=ok)
            assert r.status_code == 200
            assert set(r.json()) == {"approvals"}

            bad = h.bearer("user:none", groups=["other"])
            r2 = await client.get("/api/v1/approvals/pending", headers=bad)
            assert r2.status_code == 403

    run(_t())


def test_run_approval_decision_uses_exact_join_not_standalone_gate(
    make_app: AppBuilder,
) -> None:
    """A run-parked approval is decided through the JOIN, never the standalone gate.

    Un-stubbed. This test used to install a fake ``_run_approval_checkpoints``
    and a bare ``SimpleNamespace`` source guard and then assert a call-order
    list — which is why an ``authorize`` that returned RETRYABLE_UNKNOWN for
    EVERY run-parked approval (409 on every approve/deny/reveal, run stranded)
    shipped with green CI on 2026-08-28. There is nothing left to stub: the park
    comes from the real workflow and the decision from the real HTTP route over
    the real ``RunApprovalCheckpointStore`` + ``ProtectedSourceResultGuard``.

    The three observations that can ONLY be true of the join branch:

    * the run's continuation checkpoint moves to ``approved`` — the standalone
      gate has no checkpoint to move;
    * a resume is enqueued carrying the run's own ``resume_id`` — the standalone
      gate enqueues nothing;
    * NO ``api.approval.submitted`` surface record is emitted — the join branch
      returns before that emission, the standalone branch always reaches it
      (pinned from the other side by ``test_submitted_audit_record``).
    """

    h = make_app(approver_roles=(APPROVER_GROUP,), protected_transport=True)
    state = get_state(h.app)
    enqueuer = RecordingEnqueuer()
    state.agent_run_enqueuer = enqueuer
    stamps = admit_sources(state, key_prefix="approvals-api-join")
    parked = run(
        park_run(
            state,
            h.runtime_url,
            stamps=stamps,
            action=RUN_EFFECT_TOOL,
            arguments=RUN_ARGUMENTS,
            tier=ApprovalTier.TIER_2,
        )
    )

    async def _t() -> None:
        async with h.client() as client:
            headers = h.bearer(APPROVER, groups=[APPROVER_GROUP])
            response = await client.post(
                f"/api/v1/approvals/{parked.approval_id}/approve", json={}, headers=headers
            )
            assert response.status_code == 200, response.text
            assert response.json()["status"] == "approved"
            assert response.json()["approver"] == APPROVER

    run(_t())

    runtime = sa.create_engine(h.runtime_url)
    try:
        with runtime.connect() as conn:
            approval_status = conn.execute(
                sa.select(ne_approval_requests.c.status).where(
                    ne_approval_requests.c.request_id == parked.approval_id
                )
            ).scalar_one()
            checkpoint_status = conn.execute(
                sa.select(ne_agent_run_continuation_checkpoints.c.status).where(
                    ne_agent_run_continuation_checkpoints.c.run_id == parked.run_id
                )
            ).scalar_one()
    finally:
        runtime.dispose()
    assert (approval_status, checkpoint_status) == ("approved", "approved")

    # The join branch enqueues the run's own resume; the standalone gate cannot.
    assert [request.approval_id for request in enqueuer.requests] == [parked.approval_id]
    assert enqueuer.requests[0].run_id == parked.run_id
    assert enqueuer.requests[0].decision == "approved"

    # ...and it never reaches the standalone branch's surface audit record.
    audit = sa.create_engine(h.audit_url)
    try:
        assert list_records(audit, action="api.approval.submitted") == ()
    finally:
        audit.dispose()


def test_requester_holding_the_approver_role_cannot_approve_its_own_run_write(
    make_app: AppBuilder,
) -> None:
    """The S5 shape, made NON-vacuous: the requester IS an approver here.

    The e2e S5 assertion (``ops`` approving its own Tier-2 send returns 403)
    could only ever have proven the ROLE gate — the live ``ne-pilot`` realm's
    ``ops`` user is not in ``/approver``, so the refusal came from
    ``ne.api.authz.require_role``. Here the run OWNER carries ``APPROVER_GROUP``,
    so ``_require_approver_dep`` PASSES and the only thing left that can refuse
    is separation of duties. Bidirectional: the same row is granted on the next
    call by a distinct approver.

    This is the RUN-JOIN branch, which decides through the checkpoint
    coordinator and never reaches ``DurableApprovalGate.approve`` — so the gate's
    own guard cannot cover it.
    """

    h = make_app(approver_roles=(APPROVER_GROUP,), protected_transport=True)
    state = get_state(h.app)
    enqueuer = RecordingEnqueuer()
    state.agent_run_enqueuer = enqueuer
    stamps = admit_sources(state, key_prefix="approvals-api-sod")
    parked = run(
        park_run(
            state,
            h.runtime_url,
            stamps=stamps,
            action=RUN_EFFECT_TOOL,
            arguments=RUN_ARGUMENTS,
            tier=ApprovalTier.TIER_2,
        )
    )

    async def _t() -> None:
        async with h.client() as client:
            own = await client.post(
                f"/api/v1/approvals/{parked.approval_id}/approve",
                json={},
                headers=h.bearer(OWNER, groups=[APPROVER_GROUP]),
            )
            assert own.status_code == 403, own.text
            assert "separation of duties" in own.json()["detail"], own.text

            # CONTROL — the identical call from a DISTINCT approver is granted,
            # so the 403 above is about WHO decided, not about the row.
            other = await client.post(
                f"/api/v1/approvals/{parked.approval_id}/approve",
                json={},
                headers=h.bearer(APPROVER, groups=[APPROVER_GROUP]),
            )
            assert other.status_code == 200, other.text
            assert other.json()["approver"] == APPROVER

    run(_t())

    # The refused attempt decided NOTHING and enqueued NOTHING; only the
    # distinct approver's decision landed.
    assert [request.approval_id for request in enqueuer.requests] == [parked.approval_id]
    runtime = sa.create_engine(h.runtime_url)
    try:
        with runtime.connect() as conn:
            row = conn.execute(
                sa.select(
                    ne_approval_requests.c.status, ne_approval_requests.c.first_approver
                ).where(ne_approval_requests.c.request_id == parked.approval_id)
            ).one()
    finally:
        runtime.dispose()
    assert (row.status, row.first_approver) == ("approved", APPROVER)

    audit = sa.create_engine(h.audit_url)
    try:
        denials = [
            r
            for r in list_records(audit, action="api.approval.unauthorized")
            if json.loads(r.payload["detail"]).get("reason") == "requester-may-not-decide"
        ]
    finally:
        audit.dispose()
    assert len(denials) == 1
    assert denials[0].actor == OWNER


def test_requester_holding_the_approver_role_cannot_approve_its_own_standalone_request(
    make_app: AppBuilder,
) -> None:
    """The STANDALONE-gate branch (the air-gap lift's shape) refuses the requester.

    Same principal, both capabilities, no run join — the decision goes through
    ``DurableApprovalGate.approve``, so this pins the gate's own guard.
    """

    h = make_app(approver_roles=(APPROVER_GROUP,))
    request_id = _submit(h, tier=ApprovalTier.TIER_2)

    async def _t() -> None:
        async with h.client() as client:
            own = await client.post(
                f"/api/v1/approvals/{request_id}/approve",
                json={},
                headers=h.bearer("user:req", groups=[APPROVER_GROUP]),
            )
            assert own.status_code == 403, own.text
            assert "separation of duties" in own.json()["detail"], own.text

            # CONTROL — a distinct approver decides the very same row.
            other = await client.post(
                f"/api/v1/approvals/{request_id}/approve",
                json={},
                headers=h.bearer(APPROVER, groups=[APPROVER_GROUP]),
            )
            assert other.status_code == 200, other.text

    run(_t())

    runtime = sa.create_engine(h.runtime_url)
    try:
        with runtime.connect() as conn:
            row = conn.execute(
                sa.select(
                    ne_approval_requests.c.status, ne_approval_requests.c.first_approver
                ).where(ne_approval_requests.c.request_id == request_id)
            ).one()
    finally:
        runtime.dispose()
    assert (row.status, row.first_approver) == ("approved", APPROVER)


def test_pending_queue_shows_the_owner_authorization_horizon(make_app: AppBuilder) -> None:
    """The queue serves the REAL decision deadline, and only where one exists.

    ``expires_at`` is the approval REQUEST's own deadline. For a run-bound
    approval that is NOT when the decision stops working: the run owner's frozen
    authorization snapshot is, and on the demo box every stranded run's snapshot
    died ~15 minutes in while this queue showed a day. Approvers were reading the
    wrong clock (docs/architecture/approval-horizon.md).

    Asserted as a VALUE, not merely a present key -- an always-``None`` field
    would satisfy the payload-free key guard in test_approval_payload_api.py and
    tell an approver exactly as little as before.
    """

    h = make_app(approver_roles=(APPROVER_GROUP,), protected_transport=True)
    state = get_state(h.app)
    state.agent_run_enqueuer = RecordingEnqueuer()
    stamps = admit_sources(state, key_prefix="approvals-api-horizon")
    ttl = 1800
    before = datetime.now(UTC)
    parked = run(
        park_run(
            state,
            h.runtime_url,
            stamps=stamps,
            action=RUN_EFFECT_TOOL,
            arguments=RUN_ARGUMENTS,
            tier=ApprovalTier.TIER_2,
            owner_token_ttl_s=ttl,
        )
    )
    # A standalone (non-run-bound) approval in the same queue: it has no owner
    # snapshot, so it must report None -- "no such horizon", never a passed one.
    standalone = _submit(h, tier=ApprovalTier.TIER_2)

    async def _t() -> None:
        async with h.client() as client:
            headers = h.bearer(APPROVER, groups=[APPROVER_GROUP])
            response = await client.get("/api/v1/approvals/pending", headers=headers)
            assert response.status_code == 200, response.text
            rows = {row["request_id"]: row for row in response.json()["approvals"]}

            bound = rows[parked.approval_id]
            horizon = datetime.fromisoformat(bound["authorization_expires_at"])
            # The owner's snapshot, not the request's 24h deadline: it lands within
            # the token's own TTL window, and STRICTLY BEFORE expires_at.
            assert before + timedelta(seconds=ttl - 60) <= horizon
            assert horizon <= datetime.now(UTC) + timedelta(seconds=ttl + 60)
            assert horizon < datetime.fromisoformat(bound["expires_at"])

            assert rows[standalone]["authorization_expires_at"] is None

    run(_t())


def test_empty_approver_roles_fails_closed(make_app: AppBuilder) -> None:
    h = make_app()  # approver_roles default = () ⇒ nobody

    async def _t() -> None:
        async with h.client() as client:
            headers = h.bearer("user:rich", groups=["approvers", "admins", "ops"])
            r = await client.get("/api/v1/approvals/pending", headers=headers)
            assert r.status_code == 403

    run(_t())

    # The unauthorized attempt was audited as api.approval.unauthorized.
    engine = sa.create_engine(h.audit_url)
    try:
        rows = list_records(engine, action="api.approval.unauthorized")
    finally:
        engine.dispose()
    assert len(rows) >= 1
    assert rows[0].actor == "user:rich"


def test_pending_route_persists_orphan_expiry_before_serving_count(
    make_app: AppBuilder,
) -> None:
    """Production binding: HTTP list -> real gate -> real store expiry CAS.

    No waiter/workflow is running for this submitted row. The first queue read
    must persist and omit it; a retry must stay empty without duplicating the
    expiry evidence; and the decision route must observe the same terminal row.
    """
    h = make_app(approver_roles=("approvers",))
    rid = _submit(h, tier=ApprovalTier.TIER_2)
    runtime = sa.create_engine(h.runtime_url)
    try:
        with runtime.begin() as conn:
            conn.execute(
                ne_approval_requests.update()
                .where(ne_approval_requests.c.request_id == rid)
                .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )

        async def _t() -> None:
            async with h.client() as client:
                headers = h.bearer("user:app", groups=["approvers"])
                first = await client.get("/api/v1/approvals/pending", headers=headers)
                retry = await client.get("/api/v1/approvals/pending", headers=headers)
                approve = await client.post(
                    f"/api/v1/approvals/{rid}/approve", json={}, headers=headers
                )
                assert first.status_code == 200
                assert first.json() == {"approvals": []}
                assert retry.json() == {"approvals": []}
                assert approve.status_code == 409
                assert "expired" in approve.json()["detail"]

        run(_t())

        with runtime.connect() as conn:
            status = conn.execute(
                sa.select(ne_approval_requests.c.status).where(
                    ne_approval_requests.c.request_id == rid
                )
            ).scalar_one()
            expired_events = conn.execute(
                sa.text(
                    "SELECT payload FROM ne_outbox_events "
                    "WHERE event_type = 'approval' AND payload LIKE '%expired%'"
                )
            ).all()
        assert status == "expired"
        assert len(expired_events) == 1
    finally:
        runtime.dispose()

    audit = sa.create_engine(h.audit_url)
    try:
        expired_audit = list_records(audit, action="approval.expired")
    finally:
        audit.dispose()
    assert len(expired_audit) == 1


def test_approve_route_persists_expiry_instead_of_granting_stale_pending_row(
    make_app: AppBuilder,
) -> None:
    """A direct HTTP approve cannot win merely because status still says pending."""
    h = make_app(approver_roles=("approvers",))
    rid = _submit(h, tier=ApprovalTier.TIER_2)
    runtime = sa.create_engine(h.runtime_url)
    try:
        with runtime.begin() as conn:
            conn.execute(
                ne_approval_requests.update()
                .where(ne_approval_requests.c.request_id == rid)
                .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )

        async def _t() -> None:
            async with h.client() as client:
                headers = h.bearer("user:app", groups=["approvers"])
                response = await client.post(
                    f"/api/v1/approvals/{rid}/approve", json={}, headers=headers
                )
                assert response.status_code == 409
                assert "expired before approval" in response.json()["detail"]

        run(_t())
        with runtime.connect() as conn:
            assert (
                conn.execute(
                    sa.select(ne_approval_requests.c.status).where(
                        ne_approval_requests.c.request_id == rid
                    )
                ).scalar_one()
                == "expired"
            )
    finally:
        runtime.dispose()


def test_approve_body_smuggle_is_422_and_gate_untouched(make_app: AppBuilder) -> None:
    h = make_app(approver_roles=("approvers",))
    rid = _submit(h, tier=ApprovalTier.TIER_2)

    async def _t() -> None:
        async with h.client() as client:
            headers = h.bearer("user:app", groups=["approvers"])
            # A smuggled approver field is a loud 422 (extra=forbid) — never reaches
            # the gate.
            r = await client.post(
                f"/api/v1/approvals/{rid}/approve",
                json={"approver": "mallory@evil"},
                headers=headers,
            )
            assert r.status_code == 422

            # A legit empty-body approve returns the BEARER as approver — proving
            # the smuggle never decided the request.
            r2 = await client.post(f"/api/v1/approvals/{rid}/approve", json={}, headers=headers)
            assert r2.status_code == 200
            body = r2.json()
            assert body["status"] == "approved"
            assert body["approver"] == "user:app"
            assert body["decided_at"] is not None

    run(_t())


def test_unauthorized_malformed_body_is_403_and_audited(make_app: AppBuilder) -> None:
    # Regression (Codex P2): an authenticated-but-UNAUTHORIZED caller sending a
    # body that fails validation must still get 403 + an audited
    # api.approval.unauthorized record — NOT a 422 that skips the role pre-check
    # (and its audit) entirely. The role check is a route dependency, so it runs
    # before FastAPI validates the body.
    h = make_app()  # approver_roles default = () ⇒ nobody authorized
    rid = _submit(h, tier=ApprovalTier.TIER_2)

    async def _t() -> None:
        async with h.client() as client:
            headers = h.bearer("user:rich", groups=["approvers", "admins", "ops"])
            r = await client.post(
                f"/api/v1/approvals/{rid}/approve",
                json={"approver": "mallory@evil"},  # malformed (extra=forbid)
                headers=headers,
            )
            assert r.status_code == 403  # authz-before-body-validation, not 422

    run(_t())

    engine = sa.create_engine(h.audit_url)
    try:
        rows = list_records(engine, action="api.approval.unauthorized")
    finally:
        engine.dispose()
    assert len(rows) >= 1
    assert rows[0].actor == "user:rich"


def test_tier3_distinct_second_approver_flow(make_app: AppBuilder) -> None:
    h = make_app(
        approver_roles=("approvers",),
        approvals=ApprovalSettings(
            tier3_named_approvers=("user:alice", "user:bob"), poll_interval_s=0.05
        ),
    )
    rid = _submit(h, tier=ApprovalTier.TIER_3, action="finance.wire.send")

    async def _t() -> None:
        async with h.client() as client:
            # A group-authorized but NON-listed approver → 403 with the exact
            # Tier-3 policy substring served verbatim.
            mallory = h.bearer("user:mallory", groups=["approvers"])
            r = await client.post(f"/api/v1/approvals/{rid}/approve", json={}, headers=mallory)
            assert r.status_code == 403
            assert "not in the Tier-3 named-approver list" in r.json()["detail"]

            # First listed approver → pending_second.
            alice = h.bearer("user:alice", groups=["approvers"])
            r2 = await client.post(f"/api/v1/approvals/{rid}/approve", json={}, headers=alice)
            assert r2.status_code == 200
            assert r2.json()["status"] == "pending_second"

            # Same approver again → 403 (distinct-second policy).
            r3 = await client.post(f"/api/v1/approvals/{rid}/approve", json={}, headers=alice)
            assert r3.status_code == 403

            # A distinct second listed approver → approved (approver = the FIRST).
            bob = h.bearer("user:bob", groups=["approvers"])
            r4 = await client.post(f"/api/v1/approvals/{rid}/approve", json={}, headers=bob)
            assert r4.status_code == 200
            body = r4.json()
            assert body["status"] == "approved"
            assert body["approver"] == "user:alice"
            assert body["decided_at"] is not None

    run(_t())


def test_run_bound_tier3_wait_enqueues_only_after_distinct_second_check(
    make_app: AppBuilder,
) -> None:
    """Tier-3 over a REAL run-parked approval: two named humans, one enqueue.

    Un-stubbed. The previous version faked ``gate._store``, the checkpoint store
    AND the source guard, so ``record_first_approval``/``decide`` were asserted
    against a ``SimpleNamespace`` row whose ``status`` the fake itself mutated.
    Here the row, the CAS and the guard are the real ones: the first named
    approver lands ``pending_second`` with NOTHING enqueued and the run still
    parked, the same approver is refused, and only the distinct second approver
    turns it into one resume.
    """

    h = make_app(
        approver_roles=(APPROVER_GROUP,),
        protected_transport=True,
        approvals=ApprovalSettings(
            tier3_named_approvers=("user:alice", "user:bob"), poll_interval_s=0.05
        ),
    )
    state = get_state(h.app)
    enqueuer = RecordingEnqueuer()
    state.agent_run_enqueuer = enqueuer
    stamps = admit_sources(state, key_prefix="approvals-api-tier3")
    parked = run(
        park_run(
            state,
            h.runtime_url,
            stamps=stamps,
            action="finance.wire.send",
            arguments=RUN_ARGUMENTS,
            tier=ApprovalTier.TIER_3,
        )
    )
    rid = parked.approval_id

    def _statuses() -> tuple[str, str]:
        runtime = sa.create_engine(h.runtime_url)
        try:
            with runtime.connect() as conn:
                approval = conn.execute(
                    sa.select(ne_approval_requests.c.status).where(
                        ne_approval_requests.c.request_id == rid
                    )
                ).scalar_one()
                checkpoint = conn.execute(
                    sa.select(ne_agent_run_continuation_checkpoints.c.status).where(
                        ne_agent_run_continuation_checkpoints.c.run_id == parked.run_id
                    )
                ).scalar_one()
            return str(approval), str(checkpoint)
        finally:
            runtime.dispose()

    async def _t() -> None:
        async with h.client() as client:
            # A group-authorized but NON-listed approver → 403, verbatim policy
            # substring, and nothing moves.
            mallory = h.bearer("user:mallory", groups=[APPROVER_GROUP])
            refused = await client.post(
                f"/api/v1/approvals/{rid}/approve", json={}, headers=mallory
            )
            assert refused.status_code == 403
            assert "not in the Tier-3 named-approver list" in refused.json()["detail"]

            alice = h.bearer("user:alice", groups=[APPROVER_GROUP])
            first = await client.post(f"/api/v1/approvals/{rid}/approve", json={}, headers=alice)
            assert first.status_code == 200, first.text
            assert first.json()["status"] == "pending_second"
            assert enqueuer.requests == []

            repeat = await client.post(f"/api/v1/approvals/{rid}/approve", json={}, headers=alice)
            assert repeat.status_code == 403
            assert enqueuer.requests == []

            bob = h.bearer("user:bob", groups=[APPROVER_GROUP])
            second = await client.post(f"/api/v1/approvals/{rid}/approve", json={}, headers=bob)
            assert second.status_code == 200, second.text
            body = second.json()
            assert body["status"] == "approved"
            assert body["approver"] == "user:alice"

    run(_t())

    approval_status, checkpoint_status = _statuses()
    assert (approval_status, checkpoint_status) == ("approved", "approved")
    assert [request.approval_id for request in enqueuer.requests] == [rid]
    assert enqueuer.requests[0].run_id == parked.run_id
    assert enqueuer.requests[0].decision == "approved"


def test_approve_unknown_request_404(make_app: AppBuilder) -> None:
    h = make_app(approver_roles=("approvers",))

    async def _t() -> None:
        async with h.client() as client:
            headers = h.bearer("user:app", groups=["approvers"])
            r = await client.post(
                "/api/v1/approvals/does-not-exist/approve", json={}, headers=headers
            )
            assert r.status_code == 404
            assert isinstance(r.json()["detail"], str)

    run(_t())


def test_approve_already_decided_409(make_app: AppBuilder) -> None:
    h = make_app(approver_roles=("approvers",))
    rid = _submit(h, tier=ApprovalTier.TIER_2)

    async def _t() -> None:
        async with h.client() as client:
            headers = h.bearer("user:app", groups=["approvers"])
            r1 = await client.post(f"/api/v1/approvals/{rid}/approve", json={}, headers=headers)
            assert r1.status_code == 200
            r2 = await client.post(f"/api/v1/approvals/{rid}/approve", json={}, headers=headers)
            assert r2.status_code == 409
            assert isinstance(r2.json()["detail"], str)

    run(_t())


def test_reject_via_http(make_app: AppBuilder) -> None:
    h = make_app(approver_roles=("approvers",))
    rid = _submit(h, tier=ApprovalTier.TIER_2)

    async def _t() -> None:
        async with h.client() as client:
            headers = h.bearer("user:app", groups=["approvers"])
            r = await client.post(
                f"/api/v1/approvals/{rid}/reject", json={"reason": "not now"}, headers=headers
            )
            assert r.status_code == 200
            body = r.json()
            assert body["status"] == "rejected"
            assert body["approver"] == "user:app"
            assert body["decided_at"] is not None

            # A subsequent approve on the rejected request → 409.
            r2 = await client.post(f"/api/v1/approvals/{rid}/approve", json={}, headers=headers)
            assert r2.status_code == 409

    run(_t())


def test_submitted_audit_record(make_app: AppBuilder) -> None:
    h = make_app(approver_roles=("approvers",))
    rid = _submit(h, tier=ApprovalTier.TIER_2)

    async def _t() -> None:
        async with h.client() as client:
            headers = h.bearer("user:app", groups=["approvers"])
            r = await client.post(f"/api/v1/approvals/{rid}/approve", json={}, headers=headers)
            assert r.status_code == 200

    run(_t())

    engine = sa.create_engine(h.audit_url)
    try:
        rows = list_records(engine, action="api.approval.submitted")
    finally:
        engine.dispose()
    assert len(rows) >= 1
    rec = rows[0]
    assert rec.actor == "user:app"
    # Redacted detail: decision / request_id / status only — never a payload value.
    detail = rec.payload["detail"]
    assert "approve" in detail
    assert rid in detail
    assert "approved" in detail


def _protected_standalone_stamp() -> SourceAuthorityStamp:
    """A canonical stamp for an encoded standalone protected reference.

    Mirrors ``ne-runtime/tests/test_approval_payload_read._stamp`` — the
    reference deliberately names an invocation row that does not exist, so the
    REAL resolver admits nothing and reports RETRYABLE_UNKNOWN.
    """
    source = SourceIdentity(
        deployment_id="deployment-a",
        source_kind="m365-sharepoint",
        connector_instance_id="connector-a",
        point=M365SourcePoint(
            graph_tenant_id="tenant-a", site_id="", drive_id="drive-a", item_id="item-a"
        ),
        generation=3,
    )
    contract = SourceContractIdentity(
        source_kind="m365-sharepoint", descriptor_schema_version=1, descriptor_hash="a" * 64
    )
    artifact = SourceContentArtifactIdentity(
        source_identity=source,
        source_snapshot_hash="b" * 64,
        content_snapshot_hash="c" * 64,
        acl_snapshot_hash="d" * 64,
        source_contract=contract,
        artifact_schema_version=1,
    )
    receipt = CurrentSourceAuthorityReceipt(
        receipt_id="receipt-a",
        source_identity=source,
        artifact_identity=artifact,
        source_revision="etag-a",
        source_contract=contract,
        contract_epoch=4,
        acl_snapshot_hash=artifact.acl_snapshot_hash,
        state=SourceAuthorityState.PRESENT,
        admitted_at=datetime(2026, 8, 21, 12, tzinfo=UTC),
        authority_as_of=datetime(2026, 8, 21, 11, 59, tzinfo=UTC),
        audit_event_id="audit-a",
        lineage_id="lineage-a",
        freshness_sla_seconds=Decimal("900"),
    )
    return SourceAuthorityStamp(
        source_identity=source,
        artifact_identity=artifact,
        selected_head_revision=receipt.source_revision,
        acl_snapshot_hash=receipt.acl_snapshot_hash,
        source_contract=contract,
        contract_epoch=receipt.contract_epoch,
        authority_receipt_id=receipt.receipt_id,
        authority_receipt_hash=receipt.receipt_hash,
    )


def _submit_protected_standalone(h) -> str:
    reference = ProtectedSourceInvocationRef(
        reference_id="invocation-that-does-not-exist",
        stamps=(_protected_standalone_stamp(),),
        invocation_hash="e" * 64,
    )
    state = get_state(h.app)
    return run(
        state.gate.submit(
            ApprovalRequest(
                tier=ApprovalTier.TIER_2,
                action="write.external.demo.send",
                principal=Principal(id="user:req", groups=(), claims={}),
                payload={
                    "tool": "write.external.demo.send",
                    "disclosure": {
                        "target_resource": "account:ops-payables",
                        "description": "Protected standalone request.",
                        PROTECTED_INVOCATION_KEY: encode_protected_reference(reference),
                    },
                },
            )
        )
    )


def test_standalone_retryable_authority_is_503_with_the_row_still_pending(
    make_app: AppBuilder,
) -> None:
    """Retryable authority on the STANDALONE branch is 503, never a crash (Sol P1).

    The gate raises ``SourceAuthorityRetryableError`` when a protected request's
    authority cannot be established (here: the referenced invocation row does
    not exist, which the real resolver reports as RETRYABLE_UNKNOWN). The
    standalone payload/approve/reject handlers previously caught neither it nor
    ``RuntimeError``, so it escaped as an unhandled 500. All three must map to
    503 and — the durable fact — the row STAYS PENDING throughout.
    """
    h = make_app(approver_roles=("approvers",))
    rid = _submit_protected_standalone(h)

    def _status() -> str:
        engine = sa.create_engine(h.runtime_url)
        try:
            with engine.connect() as conn:
                return str(
                    conn.execute(
                        sa.select(ne_approval_requests.c.status).where(
                            ne_approval_requests.c.request_id == rid
                        )
                    ).scalar_one()
                )
        finally:
            engine.dispose()

    async def _t() -> None:
        async with h.client() as client:
            headers = h.bearer("user:app", groups=["approvers"])
            revealed = await client.get(f"/api/v1/approvals/{rid}/payload", headers=headers)
            assert revealed.status_code == 503, revealed.text
            approved = await client.post(
                f"/api/v1/approvals/{rid}/approve", json={}, headers=headers
            )
            assert approved.status_code == 503, approved.text
            rejected = await client.post(
                f"/api/v1/approvals/{rid}/reject", json={"reason": "no"}, headers=headers
            )
            assert rejected.status_code == 503, rejected.text

    run(_t())
    assert _status() == "pending"


def test_post_cas_audit_failure_never_reports_a_committed_decision_as_pending(
    make_app: AppBuilder,
) -> None:
    """A committed decision is reported as committed, even when its audit fails.

    Sol cross-model review round 2 (P1): the gate commits the decision CAS
    BEFORE its mandatory audit append (deliberate — the decision must never be
    lost to a sink outage), so a failing sink can leave the row terminal while
    the generic retryable arm told the approver "the request remains pending" —
    a false statement about durable governance state. The response must name
    the committed decision (500, never a success receipt without audit), and
    the durable row is asserted terminal underneath.
    """
    h = make_app(approver_roles=("approvers",))
    approve_rid = _submit(h, tier=ApprovalTier.TIER_2)
    reject_rid = _submit(h, tier=ApprovalTier.TIER_2, action="write.external.other.send")
    state = get_state(h.app)
    real_sink = state.gate._sink

    class _FailsOnDecisionAudit:
        """Fails EXACTLY the post-CAS decision appends; everything else lands."""

        async def append(self, event):
            if event.action in {"approval.granted", "approval.rejected"}:
                raise OSError("audit sink down")
            return await real_sink.append(event)

    state.gate._sink = _FailsOnDecisionAudit()  # type: ignore[assignment]

    def _status(rid: str) -> str:
        engine = sa.create_engine(h.runtime_url)
        try:
            with engine.connect() as conn:
                return str(
                    conn.execute(
                        sa.select(ne_approval_requests.c.status).where(
                            ne_approval_requests.c.request_id == rid
                        )
                    ).scalar_one()
                )
        finally:
            engine.dispose()

    async def _t() -> None:
        async with h.client() as client:
            headers = h.bearer("user:app", groups=["approvers"])
            approved = await client.post(
                f"/api/v1/approvals/{approve_rid}/approve", json={}, headers=headers
            )
            assert approved.status_code == 500, approved.text
            detail = approved.json()["detail"]
            assert "durably recorded" in detail and "approved" in detail
            assert "remains pending" not in detail

            rejected = await client.post(
                f"/api/v1/approvals/{reject_rid}/reject",
                json={"reason": "no"},
                headers=headers,
            )
            assert rejected.status_code == 500, rejected.text
            detail = rejected.json()["detail"]
            assert "durably recorded" in detail and "rejected" in detail
            assert "remains pending" not in detail

    run(_t())
    # The durable truth the responses must agree with:
    assert _status(approve_rid) == "approved"
    assert _status(reject_rid) == "rejected"


def test_post_audit_metrics_failure_still_reports_the_committed_decision(
    make_app: AppBuilder,
) -> None:
    """The 503-over-terminal defect must not return via ANY post-CAS exception.

    Sol slice 2 (P1): the round-2 fix caught ``RuntimeAuditError``
    specifically, but the gate records metrics AFTER the decision AND its
    audit have committed — a plain ``RuntimeError`` from ``record_latency``
    still hit the generic retryable arm and reported "the request remains
    pending" over a terminal, fully-audited row. The generic arm now reloads
    durable state for every post-CAS exception instead of enumerating types:
    the response names the committed decision, and the durable row is asserted
    terminal underneath.
    """
    h = make_app(approver_roles=("approvers",))
    approve_rid = _submit(h, tier=ApprovalTier.TIER_2)
    reject_rid = _submit(h, tier=ApprovalTier.TIER_2, action="write.external.other.send")
    state = get_state(h.app)

    class _FailingMetrics:
        """Raises a PLAIN RuntimeError after decision + audit both committed."""

        def record_latency(self, **_kwargs) -> None:
            raise RuntimeError("metrics backend down")

        def __getattr__(self, name: str):
            return lambda *args, **kwargs: None

    state.gate._metrics = _FailingMetrics()  # type: ignore[assignment]

    def _status(rid: str) -> str:
        engine = sa.create_engine(h.runtime_url)
        try:
            with engine.connect() as conn:
                return str(
                    conn.execute(
                        sa.select(ne_approval_requests.c.status).where(
                            ne_approval_requests.c.request_id == rid
                        )
                    ).scalar_one()
                )
        finally:
            engine.dispose()

    async def _t() -> None:
        async with h.client() as client:
            headers = h.bearer("user:app", groups=["approvers"])
            approved = await client.post(
                f"/api/v1/approvals/{approve_rid}/approve", json={}, headers=headers
            )
            assert approved.status_code == 500, approved.text
            detail = approved.json()["detail"]
            assert "durably recorded and audited" in detail and "approved" in detail
            assert "remains pending" not in detail

            rejected = await client.post(
                f"/api/v1/approvals/{reject_rid}/reject",
                json={"reason": "no"},
                headers=headers,
            )
            assert rejected.status_code == 500, rejected.text
            detail = rejected.json()["detail"]
            assert "durably recorded and audited" in detail and "rejected" in detail
            assert "remains pending" not in detail

    run(_t())
    assert _status(approve_rid) == "approved"
    assert _status(reject_rid) == "rejected"
