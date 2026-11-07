# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Frozen value objects for the principal store and sync sources.

Every model is a frozen pydantic model (``ConfigDict(frozen=True)``, mirroring
``ne.contracts.types``). Principal identifiers are stored EXACTLY as the source
gives them (Entra object ids, OIDC subjects) — no prefixing — so they match the
ACL labels NE-044 stamps onto chunks (principal-id opacity contract).
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

__all__ = [
    "PUBLIC_LINK",
    "TENANT_ALL",
    "MembershipEdge",
    "PrincipalKind",
    "PrincipalSnapshot",
    "SyncReport",
    "SyncedPrincipal",
]

# ---------------------------------------------------------------------------
# Well-known ACL vocabulary (NE-041 bullet 4 — PipesHub EntityType breadth).
# Connectors (NE-044) stamp these labels onto chunks for org-wide / public-link
# grants so those grants are not silently dropped to "named principals only".
# ---------------------------------------------------------------------------
TENANT_ALL = "acl:tenant-all"
"""ACL label a connector stamps for a DOMAIN / ORG / ANYONE-in-tenant grant.

Every store-KNOWN (synced, non-tombstoned) principal expands to include this
label, so a chunk stamped ``(TENANT_ALL,)`` is visible to any tenant member —
but NEVER to an unknown/unsynced principal (fail-closed)."""

PUBLIC_LINK = "acl:public-link"
"""ACL label a connector stamps for an ANYONE-WITH-LINK grant.

Resolves for store-known principals alongside :data:`TENANT_ALL`; never added for
unknown principals."""


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class PrincipalKind(StrEnum):
    """Whether a principal is a user or a group."""

    USER = "user"
    GROUP = "group"


class SyncedPrincipal(_Frozen):
    """One principal (user or group) observed from a sync source."""

    principal_id: str
    kind: PrincipalKind
    display_name: str = ""


class MembershipEdge(_Frozen):
    """A directed membership edge: ``member_id`` is a direct member of ``group_id``."""

    member_id: str
    group_id: str


class PrincipalSnapshot(_Frozen):
    """The outcome of one :meth:`PrincipalSource.fetch` call.

    ``is_full=True`` means the snapshot enumerates the WHOLE directory: any
    currently-stored principal absent from ``principals`` is tombstoned by the
    sync service. ``is_full=False`` is a delta: only ``replaced_groups`` /
    ``replaced_members`` / ``removed_principal_ids`` are applied and absentees are
    left untouched.

    ``replaced_groups`` lists groups whose FULL member list is present in
    ``edges`` (each triggers a per-group membership REPLACE). ``replaced_members``
    lists members whose FULL group list is present in ``edges`` (each triggers a
    per-member REPLACE). ``state`` is the source's opaque sync-state map (delta
    links etc.), persisted verbatim and handed back to the next ``fetch``.
    """

    principals: tuple[SyncedPrincipal, ...] = ()
    edges: tuple[MembershipEdge, ...] = ()
    replaced_groups: tuple[str, ...] = ()
    replaced_members: tuple[str, ...] = ()
    removed_principal_ids: tuple[str, ...] = ()
    is_full: bool = False
    state: Mapping[str, str] = {}


class SyncReport(_Frozen):
    """Counts describing what one sync run applied to the store."""

    source: str
    is_full: bool
    principals_upserted: int = 0
    principals_removed: int = 0
    memberships_added: int = 0
    memberships_removed: int = 0
    changed_members: tuple[str, ...] = ()
