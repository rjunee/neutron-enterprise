# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""OidcClaimsPrincipalSource: claims→snapshot, tolerant groups claim, last-wins."""

from __future__ import annotations

import asyncio
from typing import Any

from ne.contracts.identity import TokenClaims
from ne.permissions.config import PermissionsSettings
from ne.permissions.models import PrincipalKind
from ne.permissions.oidc import OidcClaimsPrincipalSource


def _source(**overrides: Any) -> OidcClaimsPrincipalSource:
    return OidcClaimsPrincipalSource(PermissionsSettings(**overrides))


def _claims(subject: str, claims: dict[str, Any]) -> TokenClaims:
    return TokenClaims(subject=subject, issuer="local", claims=claims)


def test_groups_become_principals_and_edges() -> None:
    src = _source()
    src.observe(_claims("u1", {"groups": ["g-eng", "g-sales"]}))
    snap = asyncio.run(src.fetch({}))

    kinds = {p.principal_id: p.kind for p in snap.principals}
    assert kinds["u1"] is PrincipalKind.USER
    assert kinds["g-eng"] is PrincipalKind.GROUP
    assert kinds["g-sales"] is PrincipalKind.GROUP
    assert {(e.member_id, e.group_id) for e in snap.edges} == {
        ("u1", "g-eng"),
        ("u1", "g-sales"),
    }
    assert snap.replaced_members == ("u1",)
    assert snap.is_full is False


def test_reserved_prefix_groups_filtered_from_snapshot() -> None:
    src = _source()
    src.observe(_claims("u1", {"groups": ["acl:tenant-all", "role:admin", "engineering"]}))
    snap = asyncio.run(src.fetch({}))

    kinds = {p.principal_id: p.kind for p in snap.principals}
    # The USER principal for the subject is still recorded.
    assert kinds["u1"] is PrincipalKind.USER
    # No GROUP principal or edge names a reserved acl:/role: id.
    group_ids = {p.principal_id for p in snap.principals if p.kind is PrincipalKind.GROUP}
    group_ids |= {e.group_id for e in snap.edges}
    assert "engineering" in group_ids
    assert not any(g.startswith(("acl:", "role:")) for g in group_ids)


def test_missing_or_malformed_groups_yields_no_groups() -> None:
    for claims in ({}, {"groups": None}, {"groups": "scalar"}, {"groups": ["ok", 5]}):
        src = _source()
        src.observe(_claims("u1", claims))
        snap = asyncio.run(src.fetch({}))
        assert [p.principal_id for p in snap.principals] == ["u1"]
        assert snap.edges == ()


def test_custom_groups_claim_honored() -> None:
    src = _source(groups_claim="roles")
    src.observe(_claims("u1", {"roles": ["r-admin"], "groups": ["ignored"]}))
    snap = asyncio.run(src.fetch({}))
    assert {e.group_id for e in snap.edges} == {"r-admin"}


def test_last_observation_wins_and_buffer_drains() -> None:
    src = _source()
    src.observe(_claims("u1", {"groups": ["g-old"]}))
    src.observe(_claims("u1", {"groups": ["g-new"]}))
    snap = asyncio.run(src.fetch({}))
    assert {e.group_id for e in snap.edges} == {"g-new"}
    # Second fetch drains empty.
    snap2 = asyncio.run(src.fetch({}))
    assert snap2.principals == ()
    assert snap2.edges == ()


def test_state_passed_through() -> None:
    src = _source()
    src.observe(_claims("u1", {"groups": []}))
    snap = asyncio.run(src.fetch({"k": "v"}))
    assert snap.state == {"k": "v"}
