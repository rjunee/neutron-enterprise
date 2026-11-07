# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""SCIM Users provisioning (NE-089) — governed writes, audit trail, fail-closed
deactivation proven both at the store layer and end-to-end through the real
C7 ACL-trimmed retrieval path.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa

from ne.audit import list_records
from ne.permissions.models import PUBLIC_LINK, TENANT_ALL

from .conftest import AppBuilder, chunk, get_state, run

_TOKEN = "a" * 32


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_TOKEN}"}


def _scim_audit_rows(h: Any) -> tuple:
    engine = sa.create_engine(h.audit_url)
    try:
        return list_records(engine, action="permissions.scim_provision")
    finally:
        engine.dispose()


def _membership_audit_rows(h: Any) -> tuple:
    engine = sa.create_engine(h.audit_url)
    try:
        return list_records(engine, action="permissions.membership_change")
    finally:
        engine.dispose()


def test_create_user_provisions_live_principal_and_audits(make_app: AppBuilder) -> None:
    h = make_app(scim_bearer_token=_TOKEN)
    state = get_state(h.app)

    async def _t() -> None:
        async with h.client() as client:
            r = await client.post(
                "/scim/v2/Users",
                json={"userName": "ada@ne.test", "externalId": "obj-ada", "active": True},
                headers=_headers(),
            )
            assert r.status_code == 201
            body = r.json()
            assert body["id"] == "obj-ada"
            assert body["userName"] == "ada@ne.test"
            assert body["schemas"] == ["urn:ietf:params:scim:schemas:core:2.0:User"]
            assert body["meta"]["resourceType"] == "User"

    run(_t())

    assert state.perm_store.is_known("obj-ada") is True
    rows = _scim_audit_rows(h)
    assert any(row.outcome == "success" and "obj-ada" in row.resource for row in rows)


def test_filter_username_eq_listresponse(make_app: AppBuilder) -> None:
    h = make_app(scim_bearer_token=_TOKEN)

    async def _t() -> None:
        async with h.client() as client:
            await client.post(
                "/scim/v2/Users",
                json={"userName": "bob@ne.test", "externalId": "obj-bob"},
                headers=_headers(),
            )
            await client.post(
                "/scim/v2/Users",
                json={"userName": "carol@ne.test", "externalId": "obj-carol"},
                headers=_headers(),
            )
            r = await client.get(
                '/scim/v2/Users?filter=userName eq "BOB@NE.TEST"', headers=_headers()
            )
            assert r.status_code == 200
            body = r.json()
            assert body["totalResults"] == 1
            assert body["Resources"][0]["id"] == "obj-bob"

    run(_t())


def test_get_user_404_scim_error_shape(make_app: AppBuilder) -> None:
    h = make_app(scim_bearer_token=_TOKEN)

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get("/scim/v2/Users/nope", headers=_headers())
            assert r.status_code == 404
            body = r.json()
            assert body["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:Error"]
            assert body["status"] == "404"

    run(_t())


def test_put_user_updates_displayname(make_app: AppBuilder) -> None:
    h = make_app(scim_bearer_token=_TOKEN)

    async def _t() -> None:
        async with h.client() as client:
            await client.post(
                "/scim/v2/Users",
                json={"userName": "dave@ne.test", "externalId": "obj-dave"},
                headers=_headers(),
            )
            r = await client.put(
                "/scim/v2/Users/obj-dave",
                json={
                    "userName": "dave@ne.test",
                    "externalId": "obj-dave",
                    "displayName": "Dave Renamed",
                },
                headers=_headers(),
            )
            assert r.status_code == 200
            assert r.json()["displayName"] == "Dave Renamed"

    run(_t())


def test_duplicate_username_409(make_app: AppBuilder) -> None:
    h = make_app(scim_bearer_token=_TOKEN)

    async def _t() -> None:
        async with h.client() as client:
            r1 = await client.post(
                "/scim/v2/Users",
                json={"userName": "eve@ne.test", "externalId": "obj-eve-1"},
                headers=_headers(),
            )
            assert r1.status_code == 201
            r2 = await client.post(
                "/scim/v2/Users",
                json={"userName": "eve@ne.test", "externalId": "obj-eve-2"},
                headers=_headers(),
            )
            assert r2.status_code == 409
            assert r2.json()["scimType"] == "uniqueness"

    run(_t())


def test_duplicate_resource_id_409_never_overwrites(make_app: AppBuilder) -> None:
    """Codex NE-089 P2 regression: a create whose computed resource id collides
    with a LIVE record (same ``externalId``, different ``userName``) must 409 —
    never silently overwrite/revive the existing resource.
    """
    h = make_app(scim_bearer_token=_TOKEN)

    async def _t() -> None:
        async with h.client() as client:
            r1 = await client.post(
                "/scim/v2/Users",
                json={"userName": "kay@ne.test", "externalId": "obj-kay"},
                headers=_headers(),
            )
            assert r1.status_code == 201
            r2 = await client.post(
                "/scim/v2/Users",
                json={"userName": "impostor@ne.test", "externalId": "obj-kay"},
                headers=_headers(),
            )
            assert r2.status_code == 409
            assert r2.json()["scimType"] == "uniqueness"
            # The original resource is untouched by the rejected create.
            r3 = await client.get("/scim/v2/Users/obj-kay", headers=_headers())
            assert r3.status_code == 200
            assert r3.json()["userName"] == "kay@ne.test"

    run(_t())

    # The rejected mutation is audited with outcome=error (ids/flags only).
    rows = _scim_audit_rows(h)
    assert any(row.outcome == "error" and "obj-kay" in row.resource for row in rows)
    assert any(row.outcome == "success" and "obj-kay" in row.resource for row in rows)


def test_create_user_ignores_unsupported_standard_attributes(make_app: AppBuilder) -> None:
    """Codex NE-089 P2 regression: Entra/Okta DEFAULT mappings send standard
    SCIM attributes outside the supported subset (``name``, ``emails``, ...) —
    the create must succeed and ignore them, not 400 the whole request.
    """
    h = make_app(scim_bearer_token=_TOKEN)
    state = get_state(h.app)

    async def _t() -> None:
        async with h.client() as client:
            r = await client.post(
                "/scim/v2/Users",
                json={
                    "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
                    "userName": "grace@ne.test",
                    "externalId": "obj-grace",
                    "displayName": "Grace H",
                    "active": "True",  # the Entra string-boolean quirk
                    "name": {"givenName": "Grace", "familyName": "H"},
                    "emails": [{"primary": True, "type": "work", "value": "grace@ne.test"}],
                    "title": "Rear Admiral",
                    "meta": {"resourceType": "User"},  # partial inbound meta — must be dropped
                },
                headers=_headers(),
            )
            assert r.status_code == 201
            body = r.json()
            assert body["id"] == "obj-grace"
            assert body["active"] is True
            # Unsupported attributes are ignored, never echoed back.
            assert "emails" not in body and "name" not in body and "title" not in body
            # The response meta is server-computed, never the (partial) echoed inbound one.
            assert body["meta"]["location"].endswith("/scim/v2/Users/obj-grace")
            assert "created" in body["meta"]

    run(_t())

    assert state.perm_store.is_known("obj-grace") is True


def test_patch_deactivate_fail_closed(make_app: AppBuilder) -> None:
    h = make_app(scim_bearer_token=_TOKEN)
    state = get_state(h.app)

    async def _t() -> None:
        async with h.client() as client:
            await client.post(
                "/scim/v2/Users",
                json={"userName": "frank@ne.test", "externalId": "obj-frank"},
                headers=_headers(),
            )
            group = await client.post(
                "/scim/v2/Groups",
                json={
                    "displayName": "eng-team",
                    "externalId": "grp-eng",
                    "members": [{"value": "obj-frank"}],
                },
                headers=_headers(),
            )
            assert group.status_code == 201

            before = state.expander.expand("obj-frank")
            assert "grp-eng" in before
            assert TENANT_ALL in before

            r = await client.patch(
                "/scim/v2/Users/obj-frank",
                json={
                    "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                    "Operations": [{"op": "replace", "path": "active", "value": False}],
                },
                headers=_headers(),
            )
            assert r.status_code == 200
            assert r.json()["active"] is False

    run(_t())

    assert state.perm_store.is_known("obj-frank") is False
    after = state.expander.expand("obj-frank")
    assert "grp-eng" not in after
    assert TENANT_ALL not in after
    assert PUBLIC_LINK not in after
    assert (
        state.perm_store.direct_groups_of(["obj-frank"]).get("obj-frank", frozenset())
        == frozenset()
    )

    provision_rows = _scim_audit_rows(h)
    assert any(
        "obj-frank" in row.resource and row.action == "permissions.scim_provision"
        for row in provision_rows
    )
    membership_rows = _membership_audit_rows(h)
    assert any("obj-frank" in row.resource for row in membership_rows)


def test_deactivate_removes_retrieval_access_e2e(make_app: AppBuilder) -> None:
    h = make_app(
        chunks=[chunk("c1", acl=[TENANT_ALL], text="secret plan")],
        scim_bearer_token=_TOKEN,
    )

    async def _t() -> None:
        async with h.client() as client:
            created = await client.post(
                "/scim/v2/Users",
                json={"userName": "ada@ne.test", "externalId": "user:ada"},
                headers=_headers(),
            )
            assert created.status_code == 201

            bearer = h.bearer("user:ada", groups=())
            search1 = await client.post(
                "/api/v1/brain/search", json={"query": "plan", "top_k": 5}, headers=bearer
            )
            assert search1.status_code == 200
            assert len(search1.json()["chunks"]) == 1

            patch = await client.patch(
                "/scim/v2/Users/user:ada",
                json={
                    "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                    "Operations": [{"op": "replace", "value": {"active": False}}],
                },
                headers=_headers(),
            )
            assert patch.status_code == 200

            bearer2 = h.bearer("user:ada", groups=())
            search2 = await client.post(
                "/api/v1/brain/search", json={"query": "plan", "top_k": 5}, headers=bearer2
            )
            assert search2.status_code == 200
            assert search2.json()["chunks"] == []

    run(_t())


def test_delete_user_tombstones_then_404(make_app: AppBuilder) -> None:
    h = make_app(scim_bearer_token=_TOKEN)
    state = get_state(h.app)

    async def _t() -> None:
        async with h.client() as client:
            await client.post(
                "/scim/v2/Users",
                json={"userName": "gina@ne.test", "externalId": "obj-gina"},
                headers=_headers(),
            )
            d = await client.delete("/scim/v2/Users/obj-gina", headers=_headers())
            assert d.status_code == 204
            g = await client.get("/scim/v2/Users/obj-gina", headers=_headers())
            assert g.status_code == 404

    run(_t())

    assert state.perm_store.is_known("obj-gina") is False


def test_reactivate_revives_identity_but_not_memberships(make_app: AppBuilder) -> None:
    h = make_app(scim_bearer_token=_TOKEN)
    state = get_state(h.app)

    async def _t() -> None:
        async with h.client() as client:
            await client.post(
                "/scim/v2/Users",
                json={"userName": "hank@ne.test", "externalId": "obj-hank"},
                headers=_headers(),
            )
            await client.post(
                "/scim/v2/Groups",
                json={
                    "displayName": "sales-team",
                    "externalId": "grp-sales",
                    "members": [{"value": "obj-hank"}],
                },
                headers=_headers(),
            )
            await client.patch(
                "/scim/v2/Users/obj-hank",
                json={
                    "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                    "Operations": [{"op": "replace", "path": "active", "value": False}],
                },
                headers=_headers(),
            )
            assert state.perm_store.is_known("obj-hank") is False

            r = await client.patch(
                "/scim/v2/Users/obj-hank",
                json={
                    "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                    "Operations": [{"op": "replace", "path": "active", "value": True}],
                },
                headers=_headers(),
            )
            assert r.status_code == 200
            assert r.json()["active"] is True

    run(_t())

    assert state.perm_store.is_known("obj-hank") is True
    assert (
        state.perm_store.direct_groups_of(["obj-hank"]).get("obj-hank", frozenset()) == frozenset()
    )
    assert "grp-sales" not in state.expander.expand("obj-hank")


def test_deactivate_scrubs_stale_group_record_so_reactivation_never_resurrects_membership(
    make_app: AppBuilder,
) -> None:
    """Codex NE-089 P1 regression.

    ``mark_removed`` cascade-deletes the LIVE membership edge the instant a
    user deactivates, but the SCIM group's own KV-backed ``members`` array is
    a separate copy of that membership list. Before the fix it stayed stale,
    so once the user was reactivated (a deliberate revive path — identity
    comes back, memberships do not) an UNRELATED subsequent PUT/PATCH of the
    SAME group re-derived membership from that stale array and silently
    re-added the edge via ``store.is_known()`` flipping back to true — with
    no explicit group-add operation at all.
    """
    h = make_app(scim_bearer_token=_TOKEN)
    state = get_state(h.app)

    async def _t() -> None:
        async with h.client() as client:
            await client.post(
                "/scim/v2/Users",
                json={"userName": "petra@ne.test", "externalId": "obj-petra"},
                headers=_headers(),
            )
            await client.post(
                "/scim/v2/Groups",
                json={
                    "displayName": "eng-team",
                    "externalId": "grp-eng",
                    "members": [{"value": "obj-petra"}],
                },
                headers=_headers(),
            )

            deactivate = await client.patch(
                "/scim/v2/Users/obj-petra",
                json={
                    "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                    "Operations": [{"op": "replace", "path": "active", "value": False}],
                },
                headers=_headers(),
            )
            assert deactivate.status_code == 200
            assert state.perm_store.is_known("obj-petra") is False

            # The group's OWN persisted record must already be scrubbed, not
            # just the live edge — proven before any further write to it.
            got = await client.get("/scim/v2/Groups/grp-eng", headers=_headers())
            assert got.json()["members"] == []

            reactivate = await client.patch(
                "/scim/v2/Users/obj-petra",
                json={
                    "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                    "Operations": [{"op": "replace", "path": "active", "value": True}],
                },
                headers=_headers(),
            )
            assert reactivate.status_code == 200
            assert state.perm_store.is_known("obj-petra") is True

            # Unrelated group PATCH (rename only — no membership op at all).
            # Pre-fix, ``_patch_group_sync`` reseeded ``current_members`` from
            # the stale persisted array and re-filtered through
            # ``store.is_known()``, resurrecting obj-petra's membership.
            renamed = await client.patch(
                "/scim/v2/Groups/grp-eng",
                json={
                    "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                    "Operations": [
                        {"op": "replace", "path": "displayName", "value": "eng-team-renamed"}
                    ],
                },
                headers=_headers(),
            )
            assert renamed.status_code == 200
            assert renamed.json()["members"] == []

    run(_t())

    assert "grp-eng" not in state.expander.expand("obj-petra")
    assert (
        state.perm_store.direct_groups_of(["obj-petra"]).get("obj-petra", frozenset())
        == frozenset()
    )


def test_delete_then_reprovision_scrubs_stale_group_record_too(make_app: AppBuilder) -> None:
    """Codex NE-089 P1 regression, delete-then-re-provision sibling.

    Same stale-record hazard as the deactivate/reactivate case above, but via
    DELETE (tombstone) followed by a fresh create reusing the same
    ``externalId`` (an explicitly deliberate revive path).
    """
    h = make_app(scim_bearer_token=_TOKEN)
    state = get_state(h.app)

    async def _t() -> None:
        async with h.client() as client:
            await client.post(
                "/scim/v2/Users",
                json={"userName": "quinn@ne.test", "externalId": "obj-quinn"},
                headers=_headers(),
            )
            await client.post(
                "/scim/v2/Groups",
                json={
                    "displayName": "ops2-team",
                    "externalId": "grp-ops2",
                    "members": [{"value": "obj-quinn"}],
                },
                headers=_headers(),
            )

            deleted = await client.delete("/scim/v2/Users/obj-quinn", headers=_headers())
            assert deleted.status_code == 204

            got = await client.get("/scim/v2/Groups/grp-ops2", headers=_headers())
            assert got.json()["members"] == []

            recreated = await client.post(
                "/scim/v2/Users",
                json={"userName": "quinn2@ne.test", "externalId": "obj-quinn"},
                headers=_headers(),
            )
            assert recreated.status_code == 201
            assert state.perm_store.is_known("obj-quinn") is True

            renamed = await client.patch(
                "/scim/v2/Groups/grp-ops2",
                json={
                    "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                    "Operations": [
                        {"op": "replace", "path": "displayName", "value": "ops2-team-renamed"}
                    ],
                },
                headers=_headers(),
            )
            assert renamed.status_code == 200
            assert renamed.json()["members"] == []

    run(_t())

    assert "grp-ops2" not in state.expander.expand("obj-quinn")
    assert (
        state.perm_store.direct_groups_of(["obj-quinn"]).get("obj-quinn", frozenset())
        == frozenset()
    )
