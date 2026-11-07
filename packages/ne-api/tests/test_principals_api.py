# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Reference-bound principal naming: visible records in, never raw ids in."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import sqlalchemy as sa

from ne.audit import list_records
from ne.contracts.governance import ApprovalRequest
from ne.contracts.types import ApprovalTier, Principal
from ne.events import new_audit_event
from ne.permissions.models import PrincipalKind, SyncedPrincipal
from ne.schema import AuditOutcome

from .conftest import AppBuilder, get_state, run

ADA = "c21456b9-c8dc-4458-b393-bc3230db0081"
BOB = "06a099a0-3333-4333-8333-000000000001"
NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


def _seed_name(h, principal_id: str, name: str, *, removed: bool = False) -> None:
    store = get_state(h.app).perm_store
    store.upsert_principals(
        [SyncedPrincipal(principal_id=principal_id, kind=PrincipalKind.USER, display_name=name)],
        source="test-idp",
        now=NOW,
    )
    if removed:
        store.mark_removed([principal_id], now=NOW)


def _submit(h, principal_id: str = ADA) -> str:
    return run(
        get_state(h.app).gate.submit(
            ApprovalRequest(
                tier=ApprovalTier.TIER_2,
                action="write.internal.crm.note",
                principal=Principal(id=principal_id, groups=(), claims={}),
                payload={},
            )
        )
    )


def _audit_rows(h, action: str):
    engine = sa.create_engine(h.audit_url)
    try:
        return list_records(engine, action=action, limit=500)
    finally:
        engine.dispose()


def test_approval_reference_derives_principal_and_never_accepts_raw_id(
    make_app: AppBuilder,
) -> None:
    h = make_app(approver_roles=("approvers",))
    _seed_name(h, ADA, "Noor Inoue")
    request_id = _submit(h)

    async def _t() -> None:
        async with h.client() as client:
            headers = h.bearer("user:approver", groups=["approvers"])
            response = await client.post(
                "/api/v1/principals/resolve",
                headers=headers,
                json={"scope": "approvals", "references": [request_id]},
            )
            assert response.status_code == 200
            assert response.json() == {
                "resolved": [{"id": ADA, "display_name": "Noor Inoue", "removed": False}]
            }
            # The reverted oracle shape is structurally rejected.
            raw = await client.post(
                "/api/v1/principals/resolve",
                headers=headers,
                json={"scope": "approvals", "references": [request_id], "ids": [ADA]},
            )
            assert raw.status_code == 422

    run(_t())


def test_unknown_approval_reference_discloses_nothing_and_receipt_names_exact_probe(
    make_app: AppBuilder,
) -> None:
    h = make_app(approver_roles=("approvers",))
    _seed_name(h, ADA, "Noor Inoue")
    request_id = _submit(h)
    unknown = "00000000-0000-4000-8000-000000000000"

    async def _t() -> None:
        async with h.client() as client:
            response = await client.post(
                "/api/v1/principals/resolve",
                headers=h.bearer("user:approver", groups=["approvers"]),
                json={"scope": "approvals", "references": [unknown, request_id]},
            )
            assert response.json()["resolved"][0]["id"] == ADA

    run(_t())
    receipts = _audit_rows(h, "api.principal.resolve")
    assert len(receipts) == 1
    detail = json.loads(receipts[0].payload["detail"])
    assert json.loads(detail["references"]) == [unknown, request_id]
    assert json.loads(detail["visible_references"]) == [request_id]
    assert json.loads(detail["principal_ids"]) == [ADA]


def test_raw_principal_id_as_reference_cannot_enumerate_seeded_directory(
    make_app: AppBuilder,
) -> None:
    h = make_app(approver_roles=("approvers",))
    _seed_name(h, BOB, "Directory-only Bob")

    async def _t() -> None:
        async with h.client() as client:
            response = await client.post(
                "/api/v1/principals/resolve",
                headers=h.bearer("user:approver", groups=["approvers"]),
                json={"scope": "approvals", "references": [BOB]},
            )
            assert response.status_code == 200
            assert response.json() == {"resolved": []}

    run(_t())


def test_scope_roles_do_not_cross(make_app: AppBuilder) -> None:
    h = make_app(ops_roles=("ops",), approver_roles=("approvers",))
    request_id = _submit(h)

    async def _t() -> None:
        async with h.client() as client:
            approver = h.bearer("user:approver", groups=["approvers"])
            ops = h.bearer("user:ops", groups=["ops"])
            audit_as_approver = await client.post(
                "/api/v1/principals/resolve",
                headers=approver,
                json={"scope": "audit", "references": ["event-x"]},
            )
            approvals_as_ops = await client.post(
                "/api/v1/principals/resolve",
                headers=ops,
                json={"scope": "approvals", "references": [request_id]},
            )
            assert audit_as_approver.status_code == 403
            assert approvals_as_ops.status_code == 403

    run(_t())


def test_audit_reference_derives_payload_authoritative_actor_and_tombstone(
    make_app: AppBuilder,
) -> None:
    h = make_app(ops_roles=("ops",))
    _seed_name(h, BOB, "Departed Bob", removed=True)
    event = new_audit_event(
        action="demo.action",
        resource="demo:record",
        outcome=AuditOutcome.success,
        actor=BOB,
    )
    run(get_state(h.app).sink.append(event))

    async def _t() -> None:
        async with h.client() as client:
            response = await client.post(
                "/api/v1/principals/resolve",
                headers=h.bearer("user:ops", groups=["ops"]),
                json={"scope": "audit", "references": [event.event_id]},
            )
            assert response.status_code == 200
            assert response.json() == {
                "resolved": [{"id": BOB, "display_name": "Departed Bob", "removed": True}]
            }

    run(_t())


def test_reference_bounds_are_pinned(make_app: AppBuilder) -> None:
    h = make_app(ops_roles=("ops",))

    async def _t() -> None:
        async with h.client() as client:
            headers = h.bearer("user:ops", groups=["ops"])
            too_many = await client.post(
                "/api/v1/principals/resolve",
                headers=headers,
                json={"scope": "audit", "references": [f"e-{i}" for i in range(501)]},
            )
            too_long = await client.post(
                "/api/v1/principals/resolve",
                headers=headers,
                json={"scope": "audit", "references": ["x" * 256]},
            )
            assert too_many.status_code == 422
            assert too_long.status_code == 422

    run(_t())
