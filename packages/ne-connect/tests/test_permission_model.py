# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""PermissionModelSpec: the UNREPRESENTABLE fail-open configuration (NE-CKC2)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ne.connect import PermissionMode, PermissionModelSpec


def test_public_within_tenant_without_default_acl_is_unrepresentable() -> None:
    with pytest.raises(ValidationError):
        PermissionModelSpec(mode=PermissionMode.PUBLIC_WITHIN_TENANT, default_acl=())


def test_public_within_tenant_with_default_acl_constructs() -> None:
    spec = PermissionModelSpec(
        mode=PermissionMode.PUBLIC_WITHIN_TENANT, default_acl=("tenant:all",)
    )
    assert spec.default_acl == ("tenant:all",)


def test_source_acl_with_empty_default_acl_constructs() -> None:
    # Per-record resolution enforces non-emptiness for source-acl mode.
    spec = PermissionModelSpec(mode=PermissionMode.SOURCE_ACL, default_acl=())
    assert spec.default_acl == ()
    assert spec.principal_namespace == "oidc"
