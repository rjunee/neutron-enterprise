# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Project a Keycloak source group into the derived Pilot Meridian Skill ACL group."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

SOURCE_GROUP = "bank.servicing.read"
TARGET_GROUP = "department:servicing"
_PAGE_SIZE = 100


class SkillAuthorityError(RuntimeError):
    """A content-closed authority projection failure."""


@dataclass(frozen=True)
class SkillAuthorityReceipt:
    """Metadata-only result; never contains member identities or credentials."""

    status: str
    added: int
    removed: int
    unchanged: int


def _group_id(client: Any, base: str, headers: dict[str, str], name: str) -> str:
    response = client.get(
        f"{base}/admin/realms/ne-pilot/groups",
        params={"search": name, "exact": "true"},
        headers=headers,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise SkillAuthorityError("Skill authority group lookup returned malformed data")
    exact = [row for row in payload if isinstance(row, dict) and row.get("name") == name]
    if len(exact) != 1 or not isinstance(exact[0].get("id"), str) or not exact[0]["id"]:
        raise SkillAuthorityError("Skill authority group lookup was not uniquely resolvable")
    return exact[0]["id"]


def _direct_member_ids(
    client: Any, base: str, headers: dict[str, str], group_id: str
) -> frozenset[str]:
    members: set[str] = set()
    first = 0
    encoded_group_id = quote(group_id, safe="")
    while True:
        response = client.get(
            f"{base}/admin/realms/ne-pilot/groups/{encoded_group_id}/members",
            params={"first": first, "max": _PAGE_SIZE, "briefRepresentation": "true"},
            headers=headers,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list) or len(payload) > _PAGE_SIZE:
            raise SkillAuthorityError("Skill authority membership page was malformed")
        page: list[str] = []
        for row in payload:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
                raise SkillAuthorityError("Skill authority membership identity was malformed")
            member_id = row["id"]
            if member_id in members or member_id in page:
                raise SkillAuthorityError("Skill authority membership identity was duplicated")
            page.append(member_id)
        members.update(page)
        if len(payload) < _PAGE_SIZE:
            return frozenset(members)
        first += _PAGE_SIZE


def project_servicing_skill_authority(
    client: Any, *, base: str, headers: dict[str, str]
) -> SkillAuthorityReceipt:
    """Make target direct membership exactly equal source direct membership.

    Both complete sets are resolved and validated before the first write. This
    deliberately derives authority only from current Keycloak membership; it
    never accepts or infers a principal from usernames, email, M365, Skill body,
    or prior target membership.
    """

    source_id = _group_id(client, base, headers, SOURCE_GROUP)
    target_id = _group_id(client, base, headers, TARGET_GROUP)
    if source_id == target_id:
        raise SkillAuthorityError("Skill authority source and target groups were not distinct")

    source_members = _direct_member_ids(client, base, headers, source_id)
    target_members = _direct_member_ids(client, base, headers, target_id)
    missing = source_members - target_members
    stale = target_members - source_members
    encoded_target_id = quote(target_id, safe="")

    for member_id in sorted(missing):
        encoded_member_id = quote(member_id, safe="")
        client.put(
            f"{base}/admin/realms/ne-pilot/users/{encoded_member_id}/groups/{encoded_target_id}",
            headers=headers,
        ).raise_for_status()
    for member_id in sorted(stale):
        encoded_member_id = quote(member_id, safe="")
        client.delete(
            f"{base}/admin/realms/ne-pilot/users/{encoded_member_id}/groups/{encoded_target_id}",
            headers=headers,
        ).raise_for_status()

    return SkillAuthorityReceipt(
        status="synchronized",
        added=len(missing),
        removed=len(stale),
        unchanged=len(source_members & target_members),
    )
