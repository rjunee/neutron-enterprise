# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""resolve_row_principals — the fail-closed SQL row-ACL resolver (NE-046)."""

from __future__ import annotations

from ne.connect.sql.acl import TENANT_ALL, resolve_row_principals
from ne.permissions.models import TENANT_ALL as NE_TENANT_ALL


def test_none_value_is_empty_and_untainted() -> None:
    out = resolve_row_principals(None)
    assert out.principals == ()
    assert out.tainted is False
    assert "no principal" in out.reason


def test_empty_string_is_empty_and_untainted() -> None:
    out = resolve_row_principals("   ")
    assert out.principals == ()
    assert out.tainted is False


def test_plain_string_yields_one_principal() -> None:
    out = resolve_row_principals("u-bob")
    assert out.principals == ("u-bob",)
    assert out.tainted is False


def test_separator_splits_and_strips() -> None:
    out = resolve_row_principals("u-ada, g-eng ,", separator=",")
    assert out.principals == ("u-ada", "g-eng")
    assert out.tainted is False


def test_list_of_strings_resolves() -> None:
    out = resolve_row_principals(["u-a", "g-b"])
    assert out.principals == ("u-a", "g-b")
    assert out.tainted is False


def test_tuple_of_strings_resolves() -> None:
    out = resolve_row_principals(("u-a", "g-b"))
    assert out.principals == ("u-a", "g-b")


def test_non_str_item_in_list_taints() -> None:
    out = resolve_row_principals(["u-a", 123])
    assert out.tainted is True
    assert "int" in out.reason
    assert "123" not in out.reason  # the offending VALUE never appears, only its type


def test_reserved_acl_prefix_in_data_taints() -> None:
    out = resolve_row_principals("acl:tenant-all")
    assert out.tainted is True
    assert out.principals == ()  # the forged label is NOT admitted as a principal


def test_reserved_role_prefix_in_data_taints() -> None:
    out = resolve_row_principals("role:admin")
    assert out.tainted is True


def test_reserved_prefix_within_list_taints() -> None:
    out = resolve_row_principals(["u-a", "acl:tenant-all"])
    assert out.tainted is True
    assert out.principals == ("u-a",)  # the non-reserved sibling still resolves


def test_order_preserving_dedupe() -> None:
    out = resolve_row_principals(["u-a", "g-b", "u-a"])
    assert out.principals == ("u-a", "g-b")


def test_unsupported_value_type_taints() -> None:
    out = resolve_row_principals(42)
    assert out.tainted is True
    assert out.principals == ()


def test_reserved_label_constant_matches_ne_permissions() -> None:
    # The NE-CKD2 reserved-namespace consistency lock (dev-dep, test-only).
    assert TENANT_ALL == NE_TENANT_ALL
