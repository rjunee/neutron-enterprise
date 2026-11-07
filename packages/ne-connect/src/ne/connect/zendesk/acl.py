# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Map Zendesk ticket/comment/article visibility to frozen ACL principals (NE-093) — pure.

This is the SECURITY CORE of the connector: it turns a ticket's principal ids /
a comment's public flag / an article's user-segment into the principal set
stamped onto each record's :class:`~ne.contracts.integration.AclEntry`.

Label families:

* a ticket's ``requester_id`` -> ``zd-user:<id>``, ``assignee_id`` ->
  ``zd-agent:<id>``, ``group_id`` -> ``zd-group:<id>``, ``organization_id`` ->
  ``zd-org:<id>`` (each present-and-nonempty id becomes a label; an ABSENT id is
  simply not granted — only a ticket with NO surviving principal at all is the
  quarantine case);
* a PRIVATE (internal) comment narrows the ticket ACL to its STAFF SUBSET —
  ``zd-group:`` / ``zd-agent:`` labels retained, ``zd-user:`` / ``zd-org:``
  stripped (an end user must not see an internal note); an empty staff subset ->
  zero principals (the caller quarantines the comment);
* a Help Center article's ``user_segment_id`` / ``user_segment_ids`` ->
  ``zd-segment:<id>`` per segment; a NULL segment is Zendesk's "everyone" ->
  :data:`TENANT_ALL`; a DRAFT article (editor-only, unresolvable in v1) is
  quarantined.

Fail-closed: a RAW source id starting with a reserved namespace
(``acl:``/``role:`` — NE-CKD2) TAINTS the outcome so the caller quarantines
rather than admitting a forged label. :data:`TENANT_ALL` is minted ONLY by
:func:`map_article_acl` for a null-segment article — never lifted from a source
id. The constant is byte-identical to ``ne.permissions.models`` (a dev-dep test
locks the equality), the same package-local pattern ``ne.connect.slack.acl`` uses.

Honest v1 limitation: agent ticket-access policies, custom roles, and brand
restrictions are NOT mirrored; the label families mirror
requester/assignee/group/org ticket visibility and Help Center user segments.
Collaborators/followers are a documented v1 cut. Principal-store membership sync
is NE-042-side.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

__all__ = [
    "AGENT_PREFIX",
    "GROUP_PREFIX",
    "ORG_PREFIX",
    "SEGMENT_PREFIX",
    "TENANT_ALL",
    "USER_PREFIX",
    "AclOutcome",
    "map_article_acl",
    "map_comment_acl",
    "map_ticket_acl",
]

ORG_PREFIX = "zd-org:"
GROUP_PREFIX = "zd-group:"
AGENT_PREFIX = "zd-agent:"
USER_PREFIX = "zd-user:"
SEGMENT_PREFIX = "zd-segment:"
TENANT_ALL = "acl:tenant-all"

_RESERVED_PREFIXES = ("acl:", "role:")
#: Labels an internal (private) comment retains — the staff subset.
_STAFF_PREFIXES = (GROUP_PREFIX, AGENT_PREFIX)


@dataclass(frozen=True)
class AclOutcome:
    """The resolved ACL for one Zendesk ticket / comment / article."""

    principals: tuple[str, ...]
    dropped: tuple[str, ...]
    tainted: bool


def _is_reserved(raw_id: str) -> bool:
    return raw_id.startswith(_RESERVED_PREFIXES)


def map_ticket_acl(ticket: Mapping[str, object]) -> AclOutcome:
    """Map one Zendesk ticket's principal ids to an :class:`AclOutcome`.

    The caller quarantines when the outcome is tainted OR carries zero principals
    (a principal-less ticket is unresolvable and never world-readable).
    """
    principals: list[str] = []
    tainted = False
    for field, prefix in (
        ("requester_id", USER_PREFIX),
        ("assignee_id", AGENT_PREFIX),
        ("group_id", GROUP_PREFIX),
        ("organization_id", ORG_PREFIX),
    ):
        raw = ticket.get(field)
        if raw in (None, ""):
            continue
        if _is_reserved(str(raw)):
            tainted = True
            continue
        principals.append(f"{prefix}{raw}")
    return AclOutcome(tuple(dict.fromkeys(principals)), (), tainted)


def map_comment_acl(ticket_outcome: AclOutcome, public: bool) -> AclOutcome:
    """Narrow a ticket's ACL for a comment: public keeps it, private -> staff subset.

    A private (internal) comment retains only ``zd-group:`` / ``zd-agent:``
    labels; ``zd-user:`` / ``zd-org:`` are stripped so an end user cannot see an
    internal note. An empty staff subset -> zero principals (caller quarantines).
    """
    if public:
        return ticket_outcome
    staff = tuple(p for p in ticket_outcome.principals if p.startswith(_STAFF_PREFIXES))
    return AclOutcome(staff, (), ticket_outcome.tainted)


def map_article_acl(article: Mapping[str, object]) -> AclOutcome:
    """Map one Help Center article's visibility to an :class:`AclOutcome`.

    A draft is quarantined (editor-only, unresolvable in v1); a null-segment
    article is Zendesk's "everyone" (:data:`TENANT_ALL`); otherwise one
    ``zd-segment:<id>`` per configured segment.
    """
    if article.get("draft") is True:
        return AclOutcome((), ("draft article — editor-only visibility unresolvable in v1",), False)

    raw_ids = article.get("user_segment_ids")
    segment_ids: list[object] = []
    if isinstance(raw_ids, (list, tuple)):
        segment_ids.extend(raw_ids)
    single = article.get("user_segment_id")
    if single not in (None, ""):
        segment_ids.append(single)

    if not segment_ids:
        # Null segment == everyone (Zendesk semantics); TENANT_ALL minted ONLY here.
        return AclOutcome((TENANT_ALL,), (), False)

    principals: list[str] = []
    tainted = False
    for seg in segment_ids:
        if seg in (None, ""):
            continue
        if _is_reserved(str(seg)):
            tainted = True
            continue
        principals.append(f"{SEGMENT_PREFIX}{seg}")
    return AclOutcome(tuple(dict.fromkeys(principals)), (), tainted)
