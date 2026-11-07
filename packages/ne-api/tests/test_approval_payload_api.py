# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""GET /v1/approvals/{request_id}/payload — the governed disclosure read (spec §5.3).

THE LOAD-BEARING SUITE (owner ruling 2026-07-31): an approver-role holder who is
NOT the named approver for a request is REFUSED the payload — proven on BODY
CONTENT (probe-value absence), not status alone — while the SAME bearer still
sees the entry EXISTS on the listing. The fixture seeds THREE approver-role
principals (alice, bob, mallory) so the discrimination is actually exercised.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import cast

import pytest
import sqlalchemy as sa

from ne.api.wire import ApprovalRunContextView
from ne.audit import list_records
from ne.contracts.governance import ApprovalRequest
from ne.contracts.types import ApprovalTier, Principal
from ne.runtime.approvals import reversibility_label
from ne.runtime.config import ApprovalSettings
from ne.runtime.run_store import AgentRunStore, RunState
from ne.runtime.source_results import ProtectedSourceResultGuard

from .conftest import AppBuilder, get_state, run

MATERIAL_AMOUNT = "250,000.00"
MATERIAL_COUNTERPARTY = "ACME GLOBAL SUPPLY LTD"


def _wire_payload() -> dict[str, object]:
    return {
        "tool": "finance.wire.transfer",
        "disclosure": {
            "target_resource": "account:ops-payables",
            "description": "Wire transfer to a named counterparty.",
            "material_parameters": {
                "amount": MATERIAL_AMOUNT,
                "counterparty": MATERIAL_COUNTERPARTY,
            },
            "reversibility": reversibility_label(3),
        },
    }


def _submit(
    h,
    *,
    tier: ApprovalTier,
    action: str = "finance.wire.transfer",
    payload: dict[str, object] | None = None,
) -> str:
    state = get_state(h.app)
    return run(
        state.gate.submit(
            ApprovalRequest(
                tier=tier,
                action=action,
                principal=Principal(id="user:req", groups=(), claims={}),
                payload=payload if payload is not None else {},
            )
        )
    )


def _make_h(h_builder: AppBuilder):
    return h_builder(
        approver_roles=("approvers",),
        approvals=ApprovalSettings(
            tier3_named_approvers=("user:alice", "user:bob"), poll_interval_s=0.05
        ),
    )


def _bind_canonical_run(
    h,
    request_id: str,
    *,
    tier: int = 2,
    action: str = "draft.email.create",
    arguments: dict[str, object] | None = None,
    authorize_error: Exception | None = None,
) -> None:
    """Bind one exact waiting Run to an already-submitted approval row.

    U2 (F2) made the park-time governance metadata on the join row load-bearing:
    ``_parked_governance_matches`` refuses (409) any join that cannot prove the
    tier/action the human's approval actually covered - the mutable request row
    is never trusted to backfill it. A well-formed join therefore ALWAYS carries
    ``tier``/``action`` equal to the park; ``tier``/``action`` here must match
    what the caller ``_submit``-ed. The metadata-less legacy join staying 409 is
    pinned by ``test_run_approval_decision_end_to_end.py``, not here.
    """

    state = get_state(h.app)

    class _Checkpoints:
        async def get_join(self, approval_id: str):
            return SimpleNamespace(
                approval_id=approval_id,
                run_id="run-exact",
                principal_id="user:req",
                tier=tier,
                action=action,
            )

        async def authorize(self, approval_id: str, **_kwargs):
            assert approval_id == request_id
            if authorize_error is not None:
                raise authorize_error
            return arguments if arguments is not None else {"subject": "protected"}

    class _Runs:
        async def get_run(self, *, run_id: str, owner_principal_id: str):
            assert (run_id, owner_principal_id) == ("run-exact", "user:req")
            return SimpleNamespace(
                run_id=run_id,
                owner_principal_id=owner_principal_id,
                agent_id="agt-meridian",
                status=RunState.WAITING_APPROVAL,
            )

    state.gate._run_approval_checkpoints = _Checkpoints()  # type: ignore[attr-defined]
    state.source_guard = cast(ProtectedSourceResultGuard, SimpleNamespace())
    state.agent_run_store = cast(AgentRunStore, _Runs())


def test_role_holder_not_named_is_refused_but_sees_entry(make_app: AppBuilder) -> None:
    h = _make_h(make_app)
    rid = _submit(h, tier=ApprovalTier.TIER_3, payload=_wire_payload())

    async def _t() -> None:
        async with h.client() as client:
            mallory = h.bearer("user:mallory", groups=["approvers"])

            # The listing still shows the entry EXISTS to this bearer.
            r = await client.get("/api/v1/approvals/pending", headers=mallory)
            assert r.status_code == 200
            rows = r.json()["approvals"]
            assert any(row["request_id"] == rid for row in rows)
            assert MATERIAL_AMOUNT not in r.text
            assert MATERIAL_COUNTERPARTY not in r.text

            # The payload read is refused.
            r2 = await client.get(f"/api/v1/approvals/{rid}/payload", headers=mallory)
            assert r2.status_code == 403
            assert MATERIAL_AMOUNT not in r2.text
            assert MATERIAL_COUNTERPARTY not in r2.text
            assert "named approver" in r2.json()["detail"]

    run(_t())


def test_named_approver_reads_payload(make_app: AppBuilder) -> None:
    h = _make_h(make_app)
    rid = _submit(h, tier=ApprovalTier.TIER_3, payload=_wire_payload())

    async def _t() -> None:
        async with h.client() as client:
            alice = h.bearer("user:alice", groups=["approvers"])
            r = await client.get(f"/api/v1/approvals/{rid}/payload", headers=alice)
            assert r.status_code == 200
            body = r.json()
            assert body["material_parameters"] == {
                "amount": MATERIAL_AMOUNT,
                "counterparty": MATERIAL_COUNTERPARTY,
            }
            assert body["target_resource"] == "account:ops-payables"
            assert "irreversible" in body["reversibility"]
            # Exact-keys pin — the wire shape is fully accounted for.
            assert set(body) == {
                "request_id",
                "action",
                "tier",
                "principal_id",
                "target_resource",
                "description",
                "material_parameters",
                "reversibility",
            }

    run(_t())


def test_run_approval_discloses_only_exact_content_free_context(make_app: AppBuilder) -> None:
    assert set(ApprovalRunContextView.model_fields) == {"approval_id", "agent_id", "run_id"}
    h = _make_h(make_app)
    rid = _submit(h, tier=ApprovalTier.TIER_2, action="draft.email.create")
    _bind_canonical_run(h, rid, arguments={"subject": "protected"})

    async def _t() -> None:
        async with h.client() as client:
            approver = h.bearer("user:alice", groups=["approvers"])
            response = await client.get(f"/api/v1/approvals/{rid}/payload", headers=approver)
            assert response.status_code == 200
            body = response.json()
            assert body["disclosure_status"] == "available"
            assert body["run_context"] == {
                "approval_id": rid,
                "agent_id": "agt-meridian",
                "run_id": "run-exact",
            }
            assert set(body["run_context"]) == {"approval_id", "agent_id", "run_id"}
            forbidden = {
                "conversation_id",
                "owner_principal_id",
                "prompt",
                "output",
                "plan",
                "evidence",
                "model",
                "source_ref",
            }
            assert forbidden.isdisjoint(body["run_context"])

    run(_t())


def test_run_tier3_context_requires_named_approver(make_app: AppBuilder) -> None:
    h = _make_h(make_app)
    rid = _submit(h, tier=ApprovalTier.TIER_3, action="draft.email.create")
    _bind_canonical_run(h, rid, tier=3)

    async def _t() -> None:
        async with h.client() as client:
            for principal_id in ("user:alice", "user:bob"):
                named = h.bearer(principal_id, groups=["approvers"])
                response = await client.get(f"/api/v1/approvals/{rid}/payload", headers=named)
                assert response.status_code == 200
                assert response.json()["run_context"] == {
                    "approval_id": rid,
                    "agent_id": "agt-meridian",
                    "run_id": "run-exact",
                }

            mallory = h.bearer("user:mallory", groups=["approvers"])
            refused = await client.get(f"/api/v1/approvals/{rid}/payload", headers=mallory)
            assert refused.status_code == 403
            assert "named approver" in refused.json()["detail"]
            assert "run_context" not in refused.text

    run(_t())


def test_run_approval_payload_read_is_audited_before_disclosure(make_app: AppBuilder) -> None:
    h = _make_h(make_app)
    rid = _submit(h, tier=ApprovalTier.TIER_2, action="draft.email.create")
    sentinel = "PROTECTED-CANONICAL-SENTINEL"
    _bind_canonical_run(h, rid, arguments={"subject": sentinel})

    async def _t() -> None:
        async with h.client() as client:
            approver = h.bearer("user:alice", groups=["approvers"])
            response = await client.get(f"/api/v1/approvals/{rid}/payload", headers=approver)
            assert response.status_code == 200

    run(_t())

    engine = sa.create_engine(h.audit_url)
    try:
        reads = list_records(engine, action="approval.payload_read")
        surface_reads = list_records(engine, action="api.approval.payload_read")
    finally:
        engine.dispose()
    assert any(row.actor == "user:alice" for row in reads)
    assert any(row.actor == "user:alice" for row in surface_reads)
    for row in [*reads, *surface_reads]:
        assert sentinel not in str(row.payload)


def test_run_approval_missing_protected_invocation_is_generic_409(make_app: AppBuilder) -> None:
    h = _make_h(make_app)
    rid = _submit(h, tier=ApprovalTier.TIER_2, action="draft.email.create")
    sentinel = "MISSING-PROTECTED-INVOCATION-SENTINEL"
    _bind_canonical_run(h, rid, authorize_error=LookupError(sentinel))

    async def _t() -> None:
        async with h.client() as client:
            approver = h.bearer("user:alice", groups=["approvers"])
            response = await client.get(f"/api/v1/approvals/{rid}/payload", headers=approver)
            assert response.status_code == 409
            assert response.json() == {"detail": "approval context unavailable"}
            assert sentinel not in response.text
            assert "run_context" not in response.text

    run(_t())


def test_run_approval_requires_current_source_guard(make_app: AppBuilder) -> None:
    h = _make_h(make_app)
    rid = _submit(h, tier=ApprovalTier.TIER_2, action="draft.email.create")
    state = get_state(h.app)

    class _Checkpoints:
        async def get_join(self, approval_id: str):
            return SimpleNamespace(
                approval_id=approval_id,
                run_id="run-exact",
                principal_id="user:req",
            )

    state.gate._run_approval_checkpoints = _Checkpoints()  # type: ignore[attr-defined]
    state.source_guard = None

    async def _t() -> None:
        async with h.client() as client:
            approver = h.bearer("user:alice", groups=["approvers"])
            response = await client.get(f"/api/v1/approvals/{rid}/payload", headers=approver)
            assert response.status_code == 503
            assert "run_context" not in response.text

    run(_t())


def test_run_approval_context_mismatch_is_one_generic_409(make_app: AppBuilder) -> None:
    h = _make_h(make_app)
    rid = _submit(h, tier=ApprovalTier.TIER_2, action="draft.email.create")
    state = get_state(h.app)

    class _Checkpoints:
        async def get_join(self, approval_id: str):
            return SimpleNamespace(
                approval_id=approval_id,
                run_id="run-missing",
                principal_id="user:req",
                # Well-formed park-time governance metadata (U2 F2): the fault
                # under test is the MISSING RUN, not a metadata-less join.
                tier=2,
                action="draft.email.create",
            )

        async def authorize(self, _approval_id: str, **_kwargs):
            return {"subject": "protected"}

    class _Runs:
        async def get_run(self, **_kwargs):
            return None

    state.gate._run_approval_checkpoints = _Checkpoints()  # type: ignore[attr-defined]
    state.source_guard = cast(ProtectedSourceResultGuard, SimpleNamespace())
    state.agent_run_store = cast(AgentRunStore, _Runs())

    async def _t() -> None:
        async with h.client() as client:
            approver = h.bearer("user:alice", groups=["approvers"])
            response = await client.get(f"/api/v1/approvals/{rid}/payload", headers=approver)
            assert response.status_code == 409
            assert response.json() == {"detail": "approval context unavailable"}
            assert "run-missing" not in response.text

    run(_t())


@pytest.mark.parametrize(
    "fault",
    (
        "join-approval",
        "row-request",
        "join-row-principal",
        "row-status",
        "run-id",
        "run-owner",
        "run-status",
        "store-absent",
    ),
)
def test_run_approval_context_every_binding_fails_closed(make_app: AppBuilder, fault: str) -> None:
    h = _make_h(make_app)
    rid = _submit(h, tier=ApprovalTier.TIER_2, action="draft.email.create")
    state = get_state(h.app)
    joined = SimpleNamespace(
        approval_id="approval-other" if fault == "join-approval" else rid,
        run_id="run-exact",
        principal_id="user:other" if fault == "join-row-principal" else "user:req",
        # Well-formed park-time governance metadata (U2 F2), so each fault
        # below is the one that actually trips the fail-closed 409.
        tier=2,
        action="draft.email.create",
    )
    row = SimpleNamespace(
        request_id="approval-other" if fault == "row-request" else rid,
        action="draft.email.create",
        tier=2,
        principal_id="user:req",
        status="approved" if fault == "row-status" else "pending",
    )
    agent_run = SimpleNamespace(
        run_id="run-other" if fault == "run-id" else "run-exact",
        owner_principal_id="user:other" if fault == "run-owner" else "user:req",
        agent_id="agt-meridian",
        status=RunState.COMPLETED if fault == "run-status" else RunState.WAITING_APPROVAL,
    )

    class _Rows:
        async def get(self, _request_id: str):
            return row

    class _Checkpoints:
        async def get_join(self, _approval_id: str):
            return joined

        async def authorize(self, _approval_id: str, **_kwargs):
            return {"subject": "protected"}

    class _Runs:
        async def get_run(self, **_kwargs):
            return agent_run

    state.gate._store = _Rows()  # type: ignore[assignment]
    state.gate._run_approval_checkpoints = _Checkpoints()  # type: ignore[attr-defined]
    state.source_guard = cast(ProtectedSourceResultGuard, SimpleNamespace())
    state.agent_run_store = None if fault == "store-absent" else cast(AgentRunStore, _Runs())

    async def _t() -> None:
        async with h.client() as client:
            approver = h.bearer("user:alice", groups=["approvers"])
            response = await client.get(f"/api/v1/approvals/{rid}/payload", headers=approver)
            assert response.status_code == 409
            assert response.json() == {"detail": "approval context unavailable"}
            assert "run-exact" not in response.text
            assert "user:req" not in response.text

    run(_t())


def test_dual_approver_both_read_third_refused(make_app: AppBuilder) -> None:
    h = _make_h(make_app)
    rid = _submit(h, tier=ApprovalTier.TIER_3, payload=_wire_payload())

    async def _t() -> None:
        async with h.client() as client:
            alice = h.bearer("user:alice", groups=["approvers"])
            bob = h.bearer("user:bob", groups=["approvers"])
            mallory = h.bearer("user:mallory", groups=["approvers"])

            # Both named approvers read at the initial 'pending' status.
            r_alice = await client.get(f"/api/v1/approvals/{rid}/payload", headers=alice)
            assert r_alice.status_code == 200
            r_bob = await client.get(f"/api/v1/approvals/{rid}/payload", headers=bob)
            assert r_bob.status_code == 200
            r_mallory = await client.get(f"/api/v1/approvals/{rid}/payload", headers=mallory)
            assert r_mallory.status_code == 403
            assert MATERIAL_AMOUNT not in r_mallory.text

            # Drive the real Tier-3 first check (alice) -> pending_second.
            approve1 = await client.post(f"/api/v1/approvals/{rid}/approve", json={}, headers=alice)
            assert approve1.status_code == 200
            assert approve1.json()["status"] == "pending_second"

            # BOTH named approvers still read at pending_second; the third still refused.
            r_alice2 = await client.get(f"/api/v1/approvals/{rid}/payload", headers=alice)
            assert r_alice2.status_code == 200
            r_bob2 = await client.get(f"/api/v1/approvals/{rid}/payload", headers=bob)
            assert r_bob2.status_code == 200
            assert r_bob2.json()["material_parameters"]["amount"] == MATERIAL_AMOUNT
            r_mallory2 = await client.get(f"/api/v1/approvals/{rid}/payload", headers=mallory)
            assert r_mallory2.status_code == 403
            assert MATERIAL_AMOUNT not in r_mallory2.text
            assert MATERIAL_COUNTERPARTY not in r_mallory2.text

    run(_t())


def test_no_role_is_403_before_gate(make_app: AppBuilder) -> None:
    h = _make_h(make_app)
    rid = _submit(h, tier=ApprovalTier.TIER_3, payload=_wire_payload())

    async def _t() -> None:
        async with h.client() as client:
            outsider = h.bearer("user:outsider", groups=["other"])
            r = await client.get(f"/api/v1/approvals/{rid}/payload", headers=outsider)
            assert r.status_code == 403
            assert MATERIAL_AMOUNT not in r.text
            assert MATERIAL_COUNTERPARTY not in r.text

    run(_t())

    engine = sa.create_engine(h.audit_url)
    try:
        rows = list_records(engine, action="api.approval.unauthorized")
    finally:
        engine.dispose()
    assert any(row.actor == "user:outsider" for row in rows)


def test_tier2_no_role_cannot_fall_through_the_gate(make_app: AppBuilder) -> None:
    h = _make_h(make_app)
    rid = _submit(h, tier=ApprovalTier.TIER_2, action="draft.email.create")

    async def _t() -> None:
        async with h.client() as client:
            outsider = h.bearer("user:outsider", groups=["other"])
            response = await client.get(f"/api/v1/approvals/{rid}/payload", headers=outsider)
            assert response.status_code == 403
            assert "run_context" not in response.text

    run(_t())


def test_payload_read_audit_trail(make_app: AppBuilder) -> None:
    h = _make_h(make_app)
    rid = _submit(h, tier=ApprovalTier.TIER_3, payload=_wire_payload())

    async def _t() -> None:
        async with h.client() as client:
            alice = h.bearer("user:alice", groups=["approvers"])
            r = await client.get(f"/api/v1/approvals/{rid}/payload", headers=alice)
            assert r.status_code == 200

            mallory = h.bearer("user:mallory", groups=["approvers"])
            r2 = await client.get(f"/api/v1/approvals/{rid}/payload", headers=mallory)
            assert r2.status_code == 403

    run(_t())

    engine = sa.create_engine(h.audit_url)
    try:
        reads = list_records(engine, action="approval.payload_read")
        surface_reads = list_records(engine, action="api.approval.payload_read")
        unauthorized = list_records(engine, action="api.approval.unauthorized")
    finally:
        engine.dispose()

    assert any(row.actor == "user:alice" for row in reads)
    assert any(row.actor == "user:alice" for row in surface_reads)
    denied = [row for row in unauthorized if row.actor == "user:mallory"]
    assert denied
    denied_detail = json.loads(denied[0].payload["detail"])
    assert denied_detail["reason"] == "payload-withheld"

    # C12: no audit record anywhere carries a raw material value.
    all_rows = list(reads) + list(surface_reads) + list(unauthorized)
    for row in all_rows:
        blob = str(row.payload)
        assert MATERIAL_AMOUNT not in blob
        assert MATERIAL_COUNTERPARTY not in blob


def test_unknown_404_and_decided_409(make_app: AppBuilder) -> None:
    h = _make_h(make_app)

    async def _t() -> None:
        async with h.client() as client:
            alice = h.bearer("user:alice", groups=["approvers"])
            r = await client.get("/api/v1/approvals/does-not-exist/payload", headers=alice)
            assert r.status_code == 404

    run(_t())

    rid = _submit(h, tier=ApprovalTier.TIER_2, action="write.external.demo.send")

    async def _t2() -> None:
        async with h.client() as client:
            appr = h.bearer("user:app", groups=["approvers"])
            approve = await client.post(f"/api/v1/approvals/{rid}/approve", json={}, headers=appr)
            assert approve.status_code == 200
            r2 = await client.get(f"/api/v1/approvals/{rid}/payload", headers=appr)
            assert r2.status_code == 409

    run(_t2())


def test_listing_stays_payload_free(make_app: AppBuilder) -> None:
    h = _make_h(make_app)
    _submit(h, tier=ApprovalTier.TIER_3, payload=_wire_payload())

    async def _t() -> None:
        async with h.client() as client:
            alice = h.bearer("user:alice", groups=["approvers"])
            r = await client.get("/api/v1/approvals/pending", headers=alice)
            assert r.status_code == 200
            rows = r.json()["approvals"]
            assert len(rows) == 1
            # A CLOSED allowlist on purpose: every new field on this queue must be
            # admitted deliberately, because this is the one approvals surface served
            # to every approver-role holder rather than to a named approver.
            #
            # authorization_expires_at admitted 2026-08-29. It is a TIMESTAMP, not
            # payload: the instant the run owner's frozen authorization snapshot runs
            # out. It is here because the queue was actively misleading — `expires_at`
            # is the request's own 24 h deadline, while the real decision horizon on
            # the demo box was ~15 MINUTES, and approvers were reading the wrong clock
            # (docs/architecture/approval-horizon.md). Marginal disclosure is bounded:
            # the row already names `principal_id` (the owner) and two other deadlines,
            # so this adds "when that owner's session ends" and nothing about content,
            # counterparty or amount — both material-leak assertions below still hold.
            assert set(rows[0]) == {
                "request_id",
                "action",
                "tier",
                "principal_id",
                "requested_at",
                "sla_deadline",
                "expires_at",
                "escalated",
                "authorization_expires_at",
            }
            assert MATERIAL_AMOUNT not in r.text
            assert MATERIAL_COUNTERPARTY not in r.text
            # Detectability control: the same probes DO appear via the governed
            # per-request read (test_named_approver_reads_payload), proving
            # their absence here is a real redaction, not an unreadable fixture.

    run(_t())
