# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Group sanitization + group->role mapping — the JIT anti-escalation core."""

from __future__ import annotations

from ne.identity.config import RoleMappingConfig
from ne.identity.roles import (
    ACL_PREFIX,
    RESERVED_PREFIXES,
    ROLE_PREFIX,
    map_groups_to_roles,
    sanitize_idp_groups,
)


def _config() -> RoleMappingConfig:
    return RoleMappingConfig(
        mapping={"platform-admins": ("admin", "auditor"), "engineering": ("developer",)},
        default_roles=("viewer",),
    )


def test_mapped_group_yields_roles() -> None:
    roles = map_groups_to_roles(("platform-admins",), _config())
    assert set(roles) == {"admin", "auditor", "viewer"}


def test_unmapped_group_contributes_nothing() -> None:
    roles = map_groups_to_roles(("random-group",), _config())
    assert set(roles) == {"viewer"}  # only default_roles


def test_default_roles_always_applied() -> None:
    assert "viewer" in map_groups_to_roles((), _config())


def test_role_prefixed_group_dropped_by_sanitize() -> None:
    # JIT self-assignment attack: a token group literally named role:admin.
    groups = sanitize_idp_groups(["engineering", f"{ROLE_PREFIX}admin"])
    assert groups == ("engineering",)


def test_acl_prefixed_group_dropped_by_sanitize() -> None:
    # C7 smuggle attack: token groups literally named the reserved ACL labels.
    groups = sanitize_idp_groups(
        ["engineering", "acl:tenant-all", "acl:public-link", "acl:anything"]
    )
    assert groups == ("engineering",)


def test_acl_prefixed_group_dropped_after_slash_strip() -> None:
    # Keycloak full-path form cannot smuggle: strip happens after slash normalize.
    assert sanitize_idp_groups(["/acl:tenant-all"]) == ()


def test_reserved_prefixes_are_role_and_acl() -> None:
    assert RESERVED_PREFIXES == (ROLE_PREFIX, ACL_PREFIX)
    assert ACL_PREFIX == "acl:"


def test_non_reserved_groups_never_dropped() -> None:
    # Only TRUE prefix matches are dropped; no legitimate group lost.
    groups = sanitize_idp_groups(["acl", "aclx", "team:acl", "my-acl:x", "engineering"])
    assert set(groups) == {"acl", "aclx", "team:acl", "my-acl:x", "engineering"}


def test_leading_slash_stripped() -> None:
    assert sanitize_idp_groups(["/platform-admins"]) == ("platform-admins",)


def test_non_str_values_dropped() -> None:
    assert sanitize_idp_groups(["engineering", 123, None, {"x": 1}]) == ("engineering",)


def test_non_sequence_raw_yields_empty() -> None:
    assert sanitize_idp_groups("engineering") == ()
    assert sanitize_idp_groups(None) == ()


def test_output_deterministic_sorted_deduped() -> None:
    assert sanitize_idp_groups(["b", "a", "a", "c"]) == ("a", "b", "c")


def test_token_roles_claim_never_consulted() -> None:
    # A forged realm_access.roles / roles claim must not become a platform role.
    idp_groups = sanitize_idp_groups(["engineering"])
    roles = map_groups_to_roles(idp_groups, _config())
    assert "admin" not in roles  # engineering maps only to developer(+viewer)
    assert set(roles) == {"developer", "viewer"}
