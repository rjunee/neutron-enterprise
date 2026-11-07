# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""map_permission_scheme — the fail-closed Jira project -> principal mapper (NE-093)."""

from __future__ import annotations

from ne.connect.jira.acl import (
    GROUP_PREFIX,
    ROLE_PREFIX,
    TENANT_ALL,
    USER_PREFIX,
    map_permission_scheme,
)
from ne.permissions.models import TENANT_ALL as NE_TENANT_ALL


def _scheme(*rows: dict[str, object]) -> dict[str, object]:
    return {"id": 1, "name": "scheme", "permissions": list(rows)}


def _browse(holder: dict[str, object]) -> dict[str, object]:
    return {"permission": "BROWSE_PROJECTS", "holder": holder}


def test_group_holder_maps_to_jira_group_label() -> None:
    out = map_permission_scheme(
        _scheme(_browse({"type": "group", "group": {"groupId": "g1"}})), "10001"
    )
    assert out.principals == (f"{GROUP_PREFIX}g1",)
    assert not out.tainted


def test_group_holder_falls_back_to_parameter() -> None:
    out = map_permission_scheme(
        _scheme(_browse({"type": "group", "parameter": "g-legacy"})), "10001"
    )
    assert out.principals == (f"{GROUP_PREFIX}g-legacy",)


def test_project_role_holder_scopes_to_project_and_role() -> None:
    out = map_permission_scheme(
        _scheme(_browse({"type": "projectRole", "projectRole": {"id": 10100}})), "10001"
    )
    assert out.principals == (f"{ROLE_PREFIX}10001:10100",)


def test_user_holder_maps_to_jira_user_label() -> None:
    out = map_permission_scheme(
        _scheme(_browse({"type": "user", "user": {"accountId": "u1"}})), "10001"
    )
    assert out.principals == (f"{USER_PREFIX}u1",)


def test_application_role_holder_mints_tenant_all() -> None:
    out = map_permission_scheme(
        _scheme(_browse({"type": "applicationRole", "applicationRole": {"key": "jira-software"}})),
        "10001",
    )
    assert out.principals == (TENANT_ALL,)
    assert not out.tainted


def test_anyone_holder_mints_tenant_all() -> None:
    out = map_permission_scheme(_scheme(_browse({"type": "anyone"})), "10001")
    assert out.principals == (TENANT_ALL,)


def test_only_browse_projects_rows_are_consulted() -> None:
    out = map_permission_scheme(
        _scheme(
            {
                "permission": "ADMINISTER_PROJECTS",
                "holder": {"type": "group", "group": {"groupId": "admins"}},
            },
            _browse({"type": "group", "group": {"groupId": "g1"}}),
            {
                "permission": "EDIT_ISSUES",
                "holder": {"type": "group", "group": {"groupId": "editors"}},
            },
        ),
        "10001",
    )
    assert out.principals == (f"{GROUP_PREFIX}g1",)


def test_missing_holder_is_dropped_with_reason() -> None:
    out = map_permission_scheme(_scheme({"permission": "BROWSE_PROJECTS"}), "10001")
    assert out.principals == ()
    assert out.dropped
    assert not out.tainted


def test_group_holder_missing_id_is_dropped() -> None:
    out = map_permission_scheme(_scheme(_browse({"type": "group", "group": {}})), "10001")
    assert out.principals == ()
    assert out.dropped


def test_unsupported_holder_type_is_dropped() -> None:
    out = map_permission_scheme(_scheme(_browse({"type": "unknownkind"})), "10001")
    assert out.principals == ()
    assert out.dropped


def test_forged_reserved_group_id_taints() -> None:
    out = map_permission_scheme(
        _scheme(_browse({"type": "group", "group": {"groupId": "acl:tenant-all"}})), "10001"
    )
    assert out.tainted is True
    assert out.principals == ()


def test_forged_reserved_role_id_taints() -> None:
    out = map_permission_scheme(
        _scheme(_browse({"type": "projectRole", "projectRole": {"id": "role:admin"}})), "10001"
    )
    assert out.tainted is True
    assert out.principals == ()


def test_forged_reserved_user_id_taints() -> None:
    out = map_permission_scheme(
        _scheme(_browse({"type": "user", "user": {"accountId": "acl:tenant-all"}})), "10001"
    )
    assert out.tainted is True


def test_order_preserving_dedupe() -> None:
    out = map_permission_scheme(
        _scheme(
            _browse({"type": "group", "group": {"groupId": "g1"}}),
            _browse({"type": "user", "user": {"accountId": "u1"}}),
            _browse({"type": "group", "group": {"groupId": "g1"}}),  # duplicate
        ),
        "10001",
    )
    assert out.principals == (f"{GROUP_PREFIX}g1", f"{USER_PREFIX}u1")


def test_zero_browse_grants_yields_empty_outcome() -> None:
    out = map_permission_scheme(
        _scheme(
            {
                "permission": "ADMINISTER_PROJECTS",
                "holder": {"type": "group", "group": {"groupId": "a"}},
            }
        ),
        "10001",
    )
    assert out.principals == ()
    assert not out.tainted


def test_missing_permissions_key_yields_empty_outcome() -> None:
    out = map_permission_scheme({"id": 1, "name": "scheme"}, "10001")
    assert out.principals == ()
    assert not out.tainted


def test_tenant_all_is_byte_equal_to_ne_permissions_models() -> None:
    # TENANT_ALL is minted ONLY by this mapper (applicationRole/anyone); it must
    # be byte-identical to the reserved constant the trimmer resolves (dev-dep
    # import, the m365/slack/salesforce lock-test pattern).
    assert TENANT_ALL == NE_TENANT_ALL
    assert TENANT_ALL.startswith("acl:")


def test_only_mapper_minted_labels_use_reserved_prefix() -> None:
    out = map_permission_scheme(
        _scheme(
            _browse({"type": "group", "group": {"groupId": "g1"}}),
            _browse({"type": "user", "user": {"accountId": "u1"}}),
            _browse({"type": "projectRole", "projectRole": {"id": 10100}}),
        ),
        "10001",
    )
    for principal in out.principals:
        assert not principal.startswith(("acl:", "role:"))
