# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""map_ticket_acl / map_comment_acl / map_article_acl — fail-closed mappers (NE-093)."""

from __future__ import annotations

from ne.connect.zendesk.acl import (
    AGENT_PREFIX,
    GROUP_PREFIX,
    ORG_PREFIX,
    SEGMENT_PREFIX,
    TENANT_ALL,
    USER_PREFIX,
    AclOutcome,
    map_article_acl,
    map_comment_acl,
    map_ticket_acl,
)
from ne.permissions.models import TENANT_ALL as NE_TENANT_ALL


def test_ticket_full_principal_set() -> None:
    out = map_ticket_acl(
        {"requester_id": 5001, "assignee_id": 6001, "group_id": 7001, "organization_id": 8001}
    )
    assert out.principals == (
        f"{USER_PREFIX}5001",
        f"{AGENT_PREFIX}6001",
        f"{GROUP_PREFIX}7001",
        f"{ORG_PREFIX}8001",
    )
    assert not out.tainted


def test_ticket_absent_ids_are_simply_not_granted() -> None:
    out = map_ticket_acl({"requester_id": 5002, "organization_id": 8002})
    assert out.principals == (f"{USER_PREFIX}5002", f"{ORG_PREFIX}8002")


def test_principal_less_ticket_yields_zero_principals() -> None:
    out = map_ticket_acl({"subject": "no ids"})
    assert out.principals == ()
    assert not out.tainted


def test_ticket_reserved_prefix_taints() -> None:
    out = map_ticket_acl({"requester_id": "acl:tenant-all", "group_id": 7001})
    assert out.tainted is True
    assert out.principals == (f"{GROUP_PREFIX}7001",)


def test_public_comment_keeps_ticket_acl() -> None:
    ticket = map_ticket_acl({"requester_id": 5001, "group_id": 7001, "assignee_id": 6001})
    out = map_comment_acl(ticket, public=True)
    assert out.principals == ticket.principals


def test_private_comment_narrows_to_staff_subset() -> None:
    ticket = map_ticket_acl(
        {"requester_id": 5001, "organization_id": 8001, "group_id": 7001, "assignee_id": 6001}
    )
    out = map_comment_acl(ticket, public=False)
    assert set(out.principals) == {f"{GROUP_PREFIX}7001", f"{AGENT_PREFIX}6001"}
    assert not any(p.startswith((USER_PREFIX, ORG_PREFIX)) for p in out.principals)


def test_private_comment_empty_staff_subset_yields_zero() -> None:
    ticket = map_ticket_acl({"requester_id": 5002, "organization_id": 8002})  # no group/agent
    out = map_comment_acl(ticket, public=False)
    assert out.principals == ()


def test_draft_article_is_quarantined() -> None:
    out = map_article_acl({"id": 1, "draft": True, "user_segment_id": None})
    assert out.principals == ()
    assert out.dropped
    assert not out.tainted


def test_null_segment_article_mints_tenant_all() -> None:
    out = map_article_acl({"id": 1, "draft": False, "user_segment_id": None})
    assert out.principals == (TENANT_ALL,)


def test_single_segment_article() -> None:
    out = map_article_acl({"id": 1, "draft": False, "user_segment_id": 3001})
    assert out.principals == (f"{SEGMENT_PREFIX}3001",)


def test_multiple_segment_ids_article() -> None:
    out = map_article_acl({"id": 1, "draft": False, "user_segment_ids": [3001, 3002]})
    assert out.principals == (f"{SEGMENT_PREFIX}3001", f"{SEGMENT_PREFIX}3002")


def test_article_reserved_segment_taints() -> None:
    out = map_article_acl({"id": 1, "draft": False, "user_segment_id": "acl:tenant-all"})
    assert out.tainted is True
    assert out.principals == ()


def test_ticket_acl_is_order_preserving_deduped() -> None:
    # A ticket that somehow repeats a value across two id fields dedupes cleanly.
    out = map_ticket_acl({"requester_id": 1, "assignee_id": 1})
    assert out.principals == (f"{USER_PREFIX}1", f"{AGENT_PREFIX}1")
    assert isinstance(out, AclOutcome)


def test_tenant_all_is_byte_equal_to_ne_permissions_models() -> None:
    assert TENANT_ALL == NE_TENANT_ALL
    assert TENANT_ALL.startswith("acl:")
