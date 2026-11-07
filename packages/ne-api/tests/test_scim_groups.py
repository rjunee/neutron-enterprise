# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""SCIM Groups provisioning (NE-089) — recompute-not-accrete membership,
the tombstoned-member fail-closed guard, and audit-shape parity with
``ne.permissions.sync.PrincipalSyncService``.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa

from ne.audit import list_records

from .conftest import AppBuilder, get_state, run

_TOKEN = "b" * 32


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_TOKEN}"}


def _membership_audit_rows(h: Any) -> tuple:
    engine = sa.create_engine(h.audit_url)
    try:
        return list_records(engine, action="permissions.membership_change")
    finally:
        engine.dispose()


async def _create_user(client: Any, username: str, external_id: str) -> None:
    r = await client.post(
        "/scim/v2/Users",
        json={"userName": username, "externalId": external_id},
        headers=_headers(),
    )
    assert r.status_code == 201


def test_create_group_with_members_expands_for_member(make_app: AppBuilder) -> None:
    h = make_app(scim_bearer_token=_TOKEN)
    state = get_state(h.app)

    async def _t() -> None:
        async with h.client() as client:
            await _create_user(client, "ivy@ne.test", "obj-ivy")
            r = await client.post(
                "/scim/v2/Groups",
                json={
                    "displayName": "product-team",
                    "externalId": "grp-product",
                    "members": [{"value": "obj-ivy"}],
                },
                headers=_headers(),
            )
            assert r.status_code == 201
            body = r.json()
            assert body["id"] == "grp-product"
            assert [m["value"] for m in body["members"]] == ["obj-ivy"]

    run(_t())

    assert "grp-product" in state.expander.expand("obj-ivy")


def test_create_group_ignores_extra_attributes_and_member_fields(make_app: AppBuilder) -> None:
    """Codex NE-089 P2 regression (group sibling): IdP group pushes carry
    member entries with extra fields (``type``, ``$ref``) and unmapped group
    attributes — the create must ignore them, not 400.
    """
    h = make_app(scim_bearer_token=_TOKEN)
    state = get_state(h.app)

    async def _t() -> None:
        async with h.client() as client:
            await _create_user(client, "nia@ne.test", "obj-nia")
            r = await client.post(
                "/scim/v2/Groups",
                json={
                    "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"],
                    "displayName": "ops-team",
                    "externalId": "grp-ops",
                    "members": [
                        {
                            "value": "obj-nia",
                            "display": "Nia",
                            "type": "User",
                            "$ref": "https://idp.example/scim/v2/Users/obj-nia",
                        }
                    ],
                    "meta": {"resourceType": "Group"},  # partial inbound meta — must be dropped
                },
                headers=_headers(),
            )
            assert r.status_code == 201
            body = r.json()
            assert [m["value"] for m in body["members"]] == ["obj-nia"]
            # The response meta is server-computed, never the (partial) echoed inbound one.
            assert body["meta"]["location"].endswith("/scim/v2/Groups/grp-ops")
            assert "created" in body["meta"]

    run(_t())

    assert "grp-ops" in state.expander.expand("obj-nia")


def test_duplicate_group_resource_id_409_never_overwrites(make_app: AppBuilder) -> None:
    """Codex NE-089 P2 regression (group sibling): a create whose computed gid
    collides with a LIVE group (same ``externalId``, different ``displayName``)
    must 409, never overwrite the existing group's record or membership.
    """
    h = make_app(scim_bearer_token=_TOKEN)

    async def _t() -> None:
        async with h.client() as client:
            r1 = await client.post(
                "/scim/v2/Groups",
                json={"displayName": "blue-team", "externalId": "grp-blue"},
                headers=_headers(),
            )
            assert r1.status_code == 201
            r2 = await client.post(
                "/scim/v2/Groups",
                json={"displayName": "red-team", "externalId": "grp-blue"},
                headers=_headers(),
            )
            assert r2.status_code == 409
            assert r2.json()["scimType"] == "uniqueness"
            r3 = await client.get("/scim/v2/Groups/grp-blue", headers=_headers())
            assert r3.status_code == 200
            assert r3.json()["displayName"] == "blue-team"

    run(_t())


def test_patch_add_member(make_app: AppBuilder) -> None:
    h = make_app(scim_bearer_token=_TOKEN)
    state = get_state(h.app)

    async def _t() -> None:
        async with h.client() as client:
            await _create_user(client, "jack@ne.test", "obj-jack")
            await client.post(
                "/scim/v2/Groups",
                json={"displayName": "ops-team", "externalId": "grp-ops", "members": []},
                headers=_headers(),
            )
            r = await client.patch(
                "/scim/v2/Groups/grp-ops",
                json={
                    "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                    "Operations": [
                        {"op": "add", "path": "members", "value": [{"value": "obj-jack"}]}
                    ],
                },
                headers=_headers(),
            )
            assert r.status_code == 200
            assert [m["value"] for m in r.json()["members"]] == ["obj-jack"]

    run(_t())

    assert "grp-ops" in state.expander.expand("obj-jack")


def test_patch_remove_member_via_path_filter(make_app: AppBuilder) -> None:
    h = make_app(scim_bearer_token=_TOKEN)
    state = get_state(h.app)

    async def _t() -> None:
        async with h.client() as client:
            await _create_user(client, "kate@ne.test", "obj-kate")
            await client.post(
                "/scim/v2/Groups",
                json={
                    "displayName": "finance-team",
                    "externalId": "grp-finance",
                    "members": [{"value": "obj-kate"}],
                },
                headers=_headers(),
            )
            r = await client.patch(
                "/scim/v2/Groups/grp-finance",
                json={
                    "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                    "Operations": [{"op": "remove", "path": 'members[value eq "obj-kate"]'}],
                },
                headers=_headers(),
            )
            assert r.status_code == 200
            assert r.json()["members"] == []

    run(_t())

    assert "grp-finance" not in state.expander.expand("obj-kate")


def test_put_group_replaces_members_recompute_not_accrete(make_app: AppBuilder) -> None:
    h = make_app(scim_bearer_token=_TOKEN)
    state = get_state(h.app)

    async def _t() -> None:
        async with h.client() as client:
            await _create_user(client, "leo@ne.test", "obj-leo")
            await _create_user(client, "mia@ne.test", "obj-mia")
            await client.post(
                "/scim/v2/Groups",
                json={
                    "displayName": "support-team",
                    "externalId": "grp-support",
                    "members": [{"value": "obj-leo"}, {"value": "obj-mia"}],
                },
                headers=_headers(),
            )
            r = await client.put(
                "/scim/v2/Groups/grp-support",
                json={
                    "displayName": "support-team",
                    "externalId": "grp-support",
                    "members": [{"value": "obj-mia"}],
                },
                headers=_headers(),
            )
            assert r.status_code == 200
            assert [m["value"] for m in r.json()["members"]] == ["obj-mia"]

    run(_t())

    assert "grp-support" not in state.expander.expand("obj-leo")
    assert "grp-support" in state.expander.expand("obj-mia")


def test_tombstoned_member_skipped_on_group_push(make_app: AppBuilder) -> None:
    h = make_app(scim_bearer_token=_TOKEN)
    state = get_state(h.app)

    async def _t() -> None:
        async with h.client() as client:
            await _create_user(client, "noah@ne.test", "obj-noah")
            await client.patch(
                "/scim/v2/Users/obj-noah",
                json={
                    "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                    "Operations": [{"op": "replace", "path": "active", "value": False}],
                },
                headers=_headers(),
            )
            assert state.perm_store.is_known("obj-noah") is False

            r = await client.post(
                "/scim/v2/Groups",
                json={
                    "displayName": "deactivated-push-team",
                    "externalId": "grp-dpt",
                    "members": [{"value": "obj-noah"}],
                },
                headers=_headers(),
            )
            assert r.status_code == 201
            assert r.json()["members"] == []

    run(_t())

    assert (
        state.perm_store.direct_groups_of(["obj-noah"]).get("obj-noah", frozenset()) == frozenset()
    )
    engine = sa.create_engine(h.audit_url)
    try:
        rows = list_records(engine, action="permissions.scim_provision")
    finally:
        engine.dispose()
    import json as jsonlib

    matches = [
        r
        for r in rows
        if r.resource is not None and "grp-dpt" in r.resource and r.outcome == "success"
    ]
    assert matches
    details = [jsonlib.loads(r.payload["detail"]) for r in matches if "detail" in r.payload]
    assert any(d.get("skipped_members") == ["obj-noah"] for d in details)


def test_delete_group_removes_label_from_members(make_app: AppBuilder) -> None:
    h = make_app(scim_bearer_token=_TOKEN)
    state = get_state(h.app)

    async def _t() -> None:
        async with h.client() as client:
            await _create_user(client, "olive@ne.test", "obj-olive")
            await client.post(
                "/scim/v2/Groups",
                json={
                    "displayName": "legal-team",
                    "externalId": "grp-legal",
                    "members": [{"value": "obj-olive"}],
                },
                headers=_headers(),
            )
            assert "grp-legal" in state.expander.expand("obj-olive")
            d = await client.delete("/scim/v2/Groups/grp-legal", headers=_headers())
            assert d.status_code == 204
            g = await client.get("/scim/v2/Groups/grp-legal", headers=_headers())
            assert g.status_code == 404

    run(_t())

    assert "grp-legal" not in state.expander.expand("obj-olive")


def test_membership_change_events_match_sync_shape(make_app: AppBuilder) -> None:
    h = make_app(scim_bearer_token=_TOKEN)

    async def _t() -> None:
        async with h.client() as client:
            await _create_user(client, "pia@ne.test", "obj-pia")
            await client.post(
                "/scim/v2/Groups",
                json={
                    "displayName": "marketing-team",
                    "externalId": "grp-marketing",
                    "members": [{"value": "obj-pia"}],
                },
                headers=_headers(),
            )

    run(_t())

    import json as jsonlib

    rows = _membership_audit_rows(h)
    pia_rows = [r for r in rows if "obj-pia" in r.resource]
    assert pia_rows
    detail = jsonlib.loads(pia_rows[0].payload["detail"])
    assert set(detail.keys()) == {"added", "removed"}
    assert detail["added"] == ["grp-marketing"]
    assert detail["removed"] == []
