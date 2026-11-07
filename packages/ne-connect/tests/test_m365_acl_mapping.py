# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""map_permissions — the fail-closed Graph-permission → principal table (NE-044)."""

from __future__ import annotations

from collections.abc import Mapping

from ne.connect.m365.acl import PUBLIC_LINK, TENANT_ALL, map_permissions
from ne.permissions.models import PUBLIC_LINK as NE_PUBLIC_LINK
from ne.permissions.models import TENANT_ALL as NE_TENANT_ALL


def _perm(**kw: object) -> dict[str, object]:
    return dict(kw)


def test_direct_user_grant_yields_raw_guid() -> None:
    out = map_permissions([_perm(grantedToV2={"user": {"id": "u-1"}})])
    assert out.principals == ("u-1",)
    assert not out.tainted


def test_direct_group_grant_yields_raw_guid() -> None:
    out = map_permissions([_perm(grantedToV2={"group": {"id": "g-1"}})])
    assert out.principals == ("g-1",)


def test_anonymous_link_is_public_link() -> None:
    out = map_permissions([_perm(link={"scope": "anonymous"})])
    assert out.principals == (PUBLIC_LINK,)


def test_organization_link_is_tenant_all() -> None:
    out = map_permissions([_perm(link={"scope": "organization"})])
    assert out.principals == (TENANT_ALL,)


def test_users_link_collects_named_identities() -> None:
    out = map_permissions(
        [_perm(link={"scope": "users"}, grantedToIdentitiesV2=[{"user": {"id": "u-2"}}])]
    )
    assert out.principals == ("u-2",)


def test_existing_access_link_grants_nothing() -> None:
    out = map_permissions([_perm(link={"scope": "existingAccess"})])
    assert out.principals == ()
    assert not out.tainted  # empty, but NOT tainted (caller still quarantines)
    assert any("existingAccess" in reason for reason in out.dropped)


def test_spo_grid_all_users_sitegroup_is_tenant_all() -> None:
    out = map_permissions(
        [_perm(grantedToV2={"siteGroup": {"loginName": "c:0-.f|rolemanager|spo-grid-all-users/x"}})]
    )
    assert out.principals == (TENANT_ALL,)


def test_everyone_claim_is_tenant_all() -> None:
    out = map_permissions([_perm(grantedToV2={"siteUser": {"loginName": "c:0(.s|true"}})])
    assert out.principals == (TENANT_ALL,)


def test_unresolvable_sitegroup_is_dropped_with_reason() -> None:
    out = map_permissions([_perm(grantedToV2={"siteGroup": {"loginName": "Custom Owners"}})])
    assert out.principals == ()
    assert not out.tainted
    assert any("no resolvable AAD object id" in reason for reason in out.dropped)


def test_application_and_device_identities_dropped() -> None:
    out = map_permissions(
        [
            _perm(grantedToV2={"application": {"id": "app-1"}}),
            _perm(grantedToV2={"device": {"id": "dev-1"}}),
        ]
    )
    assert out.principals == ()
    assert len(out.dropped) == 2


def test_forged_reserved_acl_prefix_taints() -> None:
    out = map_permissions([_perm(grantedToV2={"user": {"id": "acl:tenant-all"}})])
    assert out.tainted is True
    assert out.principals == ()  # the forged id is NOT admitted as a principal


def test_forged_reserved_role_prefix_taints() -> None:
    out = map_permissions([_perm(grantedToV2={"group": {"id": "role:admin"}})])
    assert out.tainted is True


def test_order_preserving_dedupe() -> None:
    out = map_permissions(
        [
            _perm(grantedToV2={"user": {"id": "u-a"}}),
            _perm(grantedToV2={"group": {"id": "g-b"}}),
            _perm(grantedToV2={"user": {"id": "u-a"}}),  # duplicate
        ]
    )
    assert out.principals == ("u-a", "g-b")


def test_zero_resolved_is_empty_and_not_tainted() -> None:
    out = map_permissions([_perm(link={"scope": "existingAccess"})])
    assert out.principals == ()
    assert out.tainted is False  # distinct from the tainted case


def test_reserved_label_constants_match_ne_permissions() -> None:
    # The NE-CKD2 reserved-namespace consistency lock (dev-dep, test-only).
    assert TENANT_ALL == NE_TENANT_ALL
    assert PUBLIC_LINK == NE_PUBLIC_LINK


def test_map_permissions_accepts_frozen_mappings() -> None:
    # Graph payloads arriving as nested mappings still resolve (isinstance Mapping).
    perm: Mapping[str, object] = {"grantedToV2": {"user": {"id": "u-frozen"}}}
    out = map_permissions([perm])
    assert out.principals == ("u-frozen",)


# --------------------------------------------------------------------------
# Claims-encoded Entra object ids in siteGroup/siteUser loginNames.
#
# Encodings handled are the DOCUMENTED SPO ones whose value segment is an object
# id: ``c:0t.c|tenant|<guid>`` (tenant claim provider — Entra security group or
# user) and ``c:0o.c|federateddirectoryclaimprovider|<guid>`` (M365 group). The
# documented ``i:0#.f|membership|<upn>`` user form carries a UPN, not an object
# id, so it stays dropped. Everything else fails closed.
# --------------------------------------------------------------------------

APPROVER_GUID = "ab98c77c-1310-4e50-9427-a91d6bbfc3e3"  # /m365/groups/meridian-approver


def test_tenant_claim_sitegroup_resolves_to_object_id() -> None:
    """THE LIVE CASE: the grant that refused all 30 seeded driveItems."""
    out = map_permissions(
        [_perm(grantedToV2={"siteGroup": {"loginName": f"c:0t.c|tenant|{APPROVER_GUID}"}})]
    )
    assert out.principals == (APPROVER_GUID,)
    assert out.tainted is False
    assert out.dropped == ()


def test_tenant_claim_siteuser_resolves_to_object_id() -> None:
    out = map_permissions(
        [_perm(grantedToV2={"siteUser": {"loginName": f"c:0t.c|tenant|{APPROVER_GUID}"}})]
    )
    assert out.principals == (APPROVER_GUID,)
    assert out.dropped == ()


def test_federated_directory_group_claim_resolves_to_object_id() -> None:
    login = f"c:0o.c|federateddirectoryclaimprovider|{APPROVER_GUID}"
    out = map_permissions([_perm(grantedToV2={"siteGroup": {"loginName": login}})])
    assert out.principals == (APPROVER_GUID,)
    assert out.dropped == ()


def test_claim_guid_is_normalised_to_lowercase() -> None:
    # Graph hands back canonical lowercase object ids on the group/user branch;
    # a claims login must not mint a case-variant duplicate of the same id.
    login = f"C:0T.C|TENANT|{APPROVER_GUID.upper()}"
    out = map_permissions([_perm(grantedToV2={"siteGroup": {"loginName": login}})])
    assert out.principals == (APPROVER_GUID,)


# --- MUTATION-CHECK: strict-GUID validation (``_GUID.fullmatch``) -----------
# red: relaxing ``fullmatch`` to ``search`` resolves every case below.


def test_m365_group_owners_claim_is_dropped_not_widened_to_members() -> None:
    """``<guid>_o`` is the OWNERS claim — the bare guid is the MEMBERS set."""
    login = f"c:0o.c|federateddirectoryclaimprovider|{APPROVER_GUID}_o"
    out = map_permissions([_perm(grantedToV2={"siteGroup": {"loginName": login}})])
    assert out.principals == ()  # never widen owners -> members
    assert out.tainted is False
    assert any("no resolvable AAD object id" in reason for reason in out.dropped)


def test_claim_with_trailing_junk_is_dropped() -> None:
    login = f"c:0t.c|tenant|{APPROVER_GUID}/subscope"
    out = map_permissions([_perm(grantedToV2={"siteGroup": {"loginName": login}})])
    assert out.principals == ()
    assert any("no resolvable AAD object id" in reason for reason in out.dropped)


def test_guid_merely_embedded_in_value_is_dropped() -> None:
    # "find a guid somewhere in the string" is exactly what must NOT happen.
    login = f"c:0t.c|tenant|prefix-{APPROVER_GUID}-suffix"
    out = map_permissions([_perm(grantedToV2={"siteGroup": {"loginName": login}})])
    assert out.principals == ()


def test_malformed_guid_in_recognised_claim_is_dropped() -> None:
    for bad in (
        "ab98c77c-1310-4e50-9427-a91d6bbfc3e",
        "not-a-guid",
        "",
        "zb98c77c-1310-4e50-9427-a91d6bbfc3e3",
    ):
        out = map_permissions(
            [_perm(grantedToV2={"siteGroup": {"loginName": f"c:0t.c|tenant|{bad}"}})]
        )
        assert out.principals == (), bad
        assert out.tainted is False, bad


# --- MUTATION-CHECK: unrecognised-prefix guard (``_CLAIMS_AAD_PREFIXES``) ---
# red: accepting any prefix (e.g. taking the segment after the last "|")
# resolves every case below.


def test_unrecognised_claim_prefix_with_valid_guid_is_dropped() -> None:
    out = map_permissions(
        [_perm(grantedToV2={"siteGroup": {"loginName": f"c:0zz.x|madeupprovider|{APPROVER_GUID}"}})]
    )
    assert out.principals == ()
    assert out.tainted is False
    assert any("no resolvable AAD object id" in reason for reason in out.dropped)


def test_membership_upn_claim_is_dropped_by_the_strict_guid_guard() -> None:
    """The documented Entra USER form — its value is a UPN, never an object id.

    THE NAME SAYS WHAT THIS PROVES. The guard that fires here is the strict-GUID
    one, NOT the prefix allowlist: a UPN is not a GUID, so this input stays
    dropped even with ``i:0#.f|membership|`` wrongly added to
    ``_CLAIMS_AAD_PREFIXES``. The allowlist leg is pinned by
    :func:`test_membership_prefix_is_not_an_object_id_encoding` below.
    """
    out = map_permissions(
        [
            _perm(
                grantedToV2={
                    "siteUser": {"loginName": "i:0#.f|membership|approver@meridian.example"}
                }
            )
        ]
    )
    assert out.principals == ()
    assert out.tainted is False
    assert any("no resolvable AAD object id" in reason for reason in out.dropped)


def test_membership_prefix_is_not_an_object_id_encoding() -> None:
    """A guid-SHAPED ``membership`` value is still a UPN, and still dropped.

    ``mutant:`` adding ``"i:0#.f|membership|"`` to ``_CLAIMS_AAD_PREFIXES`` reds
    this and nothing else — it is the only input where the allowlist decides the
    outcome on its own, because every other membership login is already refused
    by the GUID guard downstream. ``i:0#.f|membership|`` names the UPN namespace;
    resolving a value out of it would hand back whatever Entra object happens to
    carry that id, which is namespace confusion, not a reading of the grant.
    """
    out = map_permissions(
        [_perm(grantedToV2={"siteUser": {"loginName": f"i:0#.f|membership|{APPROVER_GUID}"}})]
    )
    assert out.principals == ()
    assert out.tainted is False
    assert any("no resolvable AAD object id" in reason for reason in out.dropped)


# --- MUTATION-CHECK: reserved-namespace taint (``_is_reserved`` on value) ---
# red: dropping the reserved check leaves tainted False (the guid check then
# merely drops the value), so the forged label is no longer quarantined.


def test_claims_login_forging_reserved_acl_label_taints() -> None:
    out = map_permissions(
        [_perm(grantedToV2={"siteGroup": {"loginName": "c:0t.c|tenant|acl:tenant-all"}})]
    )
    assert out.tainted is True
    assert out.principals == ()  # never emitted as a principal


def test_claims_login_forging_reserved_role_label_taints() -> None:
    out = map_permissions(
        [
            _perm(
                grantedToV2={
                    "siteUser": {"loginName": "c:0o.c|federateddirectoryclaimprovider|role:admin"}
                }
            )
        ]
    )
    assert out.tainted is True
    assert out.principals == ()


# --- control: the everyone-claim branch is untouched -----------------------


def test_everyone_claims_still_win_over_claims_resolution() -> None:
    """The everyone-claim branch runs FIRST and short-circuits — observably so.

    ``mutant:`` move the ``_EVERYONE_CLAIMS`` block BELOW the claims-resolution
    chain and every login here still reaches ``TENANT_ALL`` — but only after that
    chain's ``else`` has recorded a spurious ``"no resolvable AAD object id"``
    drop against it. ⭐ **The empty ``dropped`` tuple is what makes the ordering
    observable; the principal alone is not.** The third login is the sharpest:
    it is the only one carrying a RECOGNISED claims prefix, so it is the only one
    a reordering can hand to the claims chain proper — which cannot resolve
    ``spo-grid-all-users`` to a GUID — rather than to that chain's fallthrough.
    """
    for kind, login in (
        ("siteGroup", "c:0-.f|rolemanager|spo-grid-all-users/t1"),
        ("siteUser", "c:0(.s|true"),
        ("siteGroup", "c:0t.c|tenant|spo-grid-all-users"),
    ):
        out = map_permissions([_perm(grantedToV2={kind: {"loginName": login}})])
        assert out.principals == (TENANT_ALL,), login
        assert out.dropped == (), login
        assert out.tainted is False, login


def test_claims_resolved_principal_dedupes_against_direct_grant() -> None:
    # The same group granted twice (once directly, once claims-encoded) is one
    # principal, not two — the claims path emits the identical raw object id.
    out = map_permissions(
        [
            _perm(grantedToV2={"group": {"id": APPROVER_GUID}}),
            _perm(grantedToV2={"siteGroup": {"loginName": f"c:0t.c|tenant|{APPROVER_GUID}"}}),
        ]
    )
    assert out.principals == (APPROVER_GUID,)
