# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""AclMapper: the fail-closed never-empty-ACL invariant (C7/C12, NE-CKC2)."""

from __future__ import annotations

import pytest

from ne.connect import (
    AclMapper,
    AclResolutionError,
    PermissionMode,
    PermissionModelSpec,
    build_acl_index,
)
from ne.contracts.integration import AclEntry, Record


def _record(source_id: str = "src/res", record_id: str = "r1") -> Record:
    return Record(record_id=record_id, payload={"x": 1}, source_id=source_id)


def test_source_acl_absent_source_and_empty_default_raises() -> None:
    mapper = AclMapper(PermissionModelSpec(mode=PermissionMode.SOURCE_ACL))
    with pytest.raises(AclResolutionError):
        mapper.resolve(_record(), {})


def test_source_acl_present_but_empty_principals_raises() -> None:
    mapper = AclMapper(PermissionModelSpec(mode=PermissionMode.SOURCE_ACL))
    index = build_acl_index([AclEntry(resource_id="src/res", principals=())])
    with pytest.raises(AclResolutionError):
        mapper.resolve(_record(), index)


def test_source_acl_absent_source_falls_back_to_default() -> None:
    mapper = AclMapper(
        PermissionModelSpec(mode=PermissionMode.SOURCE_ACL, default_acl=("oidc:all",))
    )
    assert mapper.resolve(_record(), {}) == ("oidc:all",)


def test_source_acl_uses_index_principals() -> None:
    mapper = AclMapper(PermissionModelSpec(mode=PermissionMode.SOURCE_ACL))
    index = build_acl_index([AclEntry(resource_id="src/res", principals=("oidc:sales",))])
    assert mapper.resolve(_record(), index) == ("oidc:sales",)


def test_public_within_tenant_returns_default_acl() -> None:
    mapper = AclMapper(
        PermissionModelSpec(mode=PermissionMode.PUBLIC_WITHIN_TENANT, default_acl=("tenant:all",))
    )
    assert mapper.resolve(_record(), {}) == ("tenant:all",)


@pytest.mark.parametrize(
    ("spec", "index"),
    [
        (
            PermissionModelSpec(mode=PermissionMode.SOURCE_ACL, default_acl=("d",)),
            {},
        ),
        (
            PermissionModelSpec(mode=PermissionMode.SOURCE_ACL),
            {"src/res": ("p",)},
        ),
        (
            PermissionModelSpec(mode=PermissionMode.PUBLIC_WITHIN_TENANT, default_acl=("t",)),
            {},
        ),
    ],
)
def test_resolve_never_returns_empty_acl(
    spec: PermissionModelSpec, index: dict[str, tuple[str, ...]]
) -> None:
    result = AclMapper(spec).resolve(_record(), index)
    assert result, "resolve() must never return an empty ACL"


def test_build_acl_index_unions_paginated_duplicate_resource_ids() -> None:
    """NE-040 ACL P2: paginated crawls emit multiple AclEntry rows per resource.

    Last-write-wins DROPPED principals named on earlier pages (users lost access).
    The index must UNION them, order-preserving, with no dropped principals.
    """
    index = build_acl_index(
        [
            AclEntry(resource_id="res-1", principals=("user:a",)),
            AclEntry(resource_id="res-1", principals=("group:eng",)),
            AclEntry(resource_id="res-1", principals=("user:a", "user:b")),
        ]
    )
    assert index["res-1"] == ("user:a", "group:eng", "user:b")

    # AclMapper.resolve in SOURCE_ACL mode returns the union for a res-1 record.
    mapper = AclMapper(PermissionModelSpec(mode=PermissionMode.SOURCE_ACL))
    resolved = mapper.resolve(_record(source_id="res-1"), index)
    assert resolved == ("user:a", "group:eng", "user:b")
