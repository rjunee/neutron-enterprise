# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""map_object_permissions — the fail-closed Salesforce object -> principal mapper (NE-047)."""

from __future__ import annotations

from ne.connect.salesforce.acl import PERMSET_PREFIX, PROFILE_PREFIX, map_object_permissions
from ne.permissions.models import TENANT_ALL as NE_TENANT_ALL


def _row(
    obj: str,
    *,
    read: bool = True,
    parent_id: str | None = "0PS000000000001AAA",
    is_profile: bool = False,
    profile_id: str | None = "00e000000000001AAA",
    parent: object | None = "__unset__",
) -> dict[str, object]:
    if parent == "__unset__":
        parent = {"Id": parent_id, "IsOwnedByProfile": is_profile, "ProfileId": profile_id}
    return {"SobjectType": obj, "PermissionsRead": read, "Parent": parent}


def test_profile_owned_row_maps_to_sf_profile_label() -> None:
    rows = [_row("Account", is_profile=True, profile_id="00e000000000001AAA")]
    out = map_object_permissions(rows, ["Account"])
    assert out["Account"].principals == (f"{PROFILE_PREFIX}00e000000000001AAA",)
    assert not out["Account"].tainted


def test_standalone_permission_set_maps_to_sf_permset_label() -> None:
    rows = [_row("Account", is_profile=False, parent_id="0PS000000000001AAA")]
    out = map_object_permissions(rows, ["Account"])
    assert out["Account"].principals == (f"{PERMSET_PREFIX}0PS000000000001AAA",)
    assert not out["Account"].tainted


def test_permissions_read_false_is_ignored() -> None:
    rows = [_row("Account", read=False)]
    out = map_object_permissions(rows, ["Account"])
    assert out["Account"].principals == ()
    assert out["Account"].dropped == ()
    assert not out["Account"].tainted


def test_null_parent_is_dropped_with_reason() -> None:
    rows = [_row("Account", parent=None)]
    out = map_object_permissions(rows, ["Account"])
    assert out["Account"].principals == ()
    assert out["Account"].dropped
    assert not out["Account"].tainted


def test_empty_parent_is_dropped_with_reason() -> None:
    rows = [_row("Account", parent={})]
    out = map_object_permissions(rows, ["Account"])
    assert out["Account"].principals == ()
    assert out["Account"].dropped


def test_parent_missing_id_field_is_dropped_with_reason() -> None:
    rows = [_row("Account", is_profile=True, profile_id=None)]
    out = map_object_permissions(rows, ["Account"])
    assert out["Account"].principals == ()
    assert out["Account"].dropped
    assert not out["Account"].tainted


def test_forged_reserved_acl_prefix_taints() -> None:
    rows = [_row("Account", is_profile=False, parent_id="acl:tenant-all")]
    out = map_object_permissions(rows, ["Account"])
    assert out["Account"].tainted is True
    assert out["Account"].principals == ()


def test_forged_reserved_role_prefix_taints() -> None:
    rows = [_row("Account", is_profile=False, parent_id="role:admin")]
    out = map_object_permissions(rows, ["Account"])
    assert out["Account"].tainted is True


def test_forged_reserved_profile_id_taints() -> None:
    rows = [_row("Account", is_profile=True, profile_id="acl:tenant-all")]
    out = map_object_permissions(rows, ["Account"])
    assert out["Account"].tainted is True
    assert out["Account"].principals == ()


def test_order_preserving_dedupe_across_pages() -> None:
    rows = [
        _row("Account", is_profile=True, profile_id="00e000000000001AAA"),
        _row("Account", is_profile=False, parent_id="0PS000000000001AAA"),
        _row("Account", is_profile=True, profile_id="00e000000000001AAA"),  # duplicate
    ]
    out = map_object_permissions(rows, ["Account"])
    assert out["Account"].principals == (
        f"{PROFILE_PREFIX}00e000000000001AAA",
        f"{PERMSET_PREFIX}0PS000000000001AAA",
    )


def test_object_absent_from_rows_yields_empty_outcome() -> None:
    out = map_object_permissions([], ["Account", "Opportunity"])
    assert out["Account"].principals == ()
    assert out["Opportunity"].principals == ()
    assert not out["Account"].tainted
    assert not out["Opportunity"].tainted


def test_every_configured_object_gets_an_entry_even_with_no_matching_rows() -> None:
    rows = [_row("Account")]
    out = map_object_permissions(rows, ["Account", "Contact", "Secretplan__c"])
    assert set(out) == {"Account", "Contact", "Secretplan__c"}
    assert out["Contact"].principals == ()
    assert out["Secretplan__c"].principals == ()


def test_no_emitted_label_starts_with_a_reserved_prefix() -> None:
    # The NE-CKD2 reserved-namespace consistency lock: this mapper NEVER
    # emits a reserved-namespace label (unlike slack/m365's TENANT_ALL); it
    # only ever admits sf-profile:/sf-permset:-prefixed labels.
    rows = [
        _row("Account", is_profile=True, profile_id="00e000000000001AAA"),
        _row("Account", is_profile=False, parent_id="0PS000000000001AAA"),
    ]
    out = map_object_permissions(rows, ["Account"])
    for principal in out["Account"].principals:
        assert not principal.startswith(("acl:", "role:"))
    # And the reserved-namespace constant itself is exactly what this module's
    # taint check guards against — cross-checked against ne.permissions.models
    # (dev-dep import, m365/slack lock-test pattern).
    assert NE_TENANT_ALL.startswith("acl:")
