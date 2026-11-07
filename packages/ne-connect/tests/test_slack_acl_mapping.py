# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""map_channel_acl — the fail-closed Slack channel -> principal mapper (NE-045)."""

from __future__ import annotations

from ne.connect.slack.acl import TENANT_ALL, map_channel_acl
from ne.permissions.models import TENANT_ALL as NE_TENANT_ALL


def test_public_channel_maps_to_tenant_all() -> None:
    out = map_channel_acl({"is_channel": True, "is_private": False}, None)
    assert out.principals == (TENANT_ALL,)
    assert not out.tainted


def test_public_channel_ignores_members_argument() -> None:
    out = map_channel_acl({"is_channel": True, "is_private": False}, ["U00000ADA", "U00000BOB"])
    assert out.principals == (TENANT_ALL,)


def test_private_channel_maps_to_deduped_member_tuple() -> None:
    out = map_channel_acl(
        {"is_channel": True, "is_private": True},
        ["U00000ADA", "U00000BOB", "U00000ADA"],
    )
    assert out.principals == ("U00000ADA", "U00000BOB")
    assert not out.tainted


def test_mpim_maps_to_member_tuple() -> None:
    out = map_channel_acl({"is_mpim": True}, ["U00000ADA", "U00000EVE"])
    assert out.principals == ("U00000ADA", "U00000EVE")


def test_im_maps_to_member_tuple() -> None:
    out = map_channel_acl({"is_im": True}, ["U00000ADA"])
    assert out.principals == ("U00000ADA",)


def test_ext_shared_channel_is_quarantine_reason_not_public() -> None:
    out = map_channel_acl({"is_channel": True, "is_private": False, "is_ext_shared": True}, None)
    assert out.principals == ()
    assert not out.tainted
    assert any("Slack Connect" in reason for reason in out.dropped)


def test_shared_channel_flag_is_also_quarantined() -> None:
    out = map_channel_acl({"is_channel": True, "is_private": False, "is_shared": True}, None)
    assert out.principals == ()
    assert any("Slack Connect" in reason for reason in out.dropped)


def test_pending_ext_shared_flag_is_also_quarantined() -> None:
    out = map_channel_acl(
        {"is_channel": True, "is_private": True, "is_pending_ext_shared": True}, ["U00000ADA"]
    )
    assert out.principals == ()


def test_unresolvable_channel_type_is_quarantine_reason() -> None:
    out = map_channel_acl({"name": "mystery"}, None)
    assert out.principals == ()
    assert not out.tainted
    assert any("unresolvable" in reason for reason in out.dropped)


def test_unfetched_members_is_empty_and_not_tainted() -> None:
    out = map_channel_acl({"is_private": True}, None)
    assert out.principals == ()
    assert not out.tainted


def test_empty_members_is_empty_and_not_tainted() -> None:
    out = map_channel_acl({"is_private": True}, [])
    assert out.principals == ()
    assert not out.tainted


def test_forged_reserved_acl_prefix_taints() -> None:
    out = map_channel_acl({"is_private": True}, ["U00000ADA", "acl:tenant-all"])
    assert out.tainted is True


def test_forged_reserved_role_prefix_taints() -> None:
    out = map_channel_acl({"is_mpim": True}, ["role:admin"])
    assert out.tainted is True


def test_reserved_label_constant_matches_ne_permissions() -> None:
    # The NE-CKD2 reserved-namespace consistency lock (dev-dep, test-only).
    assert TENANT_ALL == NE_TENANT_ALL
