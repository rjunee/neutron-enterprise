# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Declarative product-principal to source-native ACL-label mappings.

Direction is explicit in the document's field names: every ``principal`` is the
product identity the caller authenticates, and every value in ``grants`` is a
source-native ACL label. The resulting edge is always ``principal -> grant`` so
the existing member-to-group expansion carries the label to chunk ACL matching.

A source-native value in ``principal`` is rejected because it strongly suggests
the fields were swapped. Such an inversion would otherwise fail silently closed:
the expander never traverses group-to-member, so the intended grant would simply
be invisible. Source-native prefixes are kept in an extensible module constant.

One deliberate side effect deserves operator attention. Mapping a ``kind="user"``
subject makes that pid store-known, so the existing expander semantics also grant
it ``acl:tenant-all`` and ``acl:public-link``. A group-keyed mapping does not widen
the querying subject because ``is_known`` checks that subject, never its groups.
Prefer group-keyed mappings.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ne.permissions.models import (
    MembershipEdge,
    PrincipalKind,
    PrincipalSnapshot,
    SyncedPrincipal,
)

__all__ = ["AclMappingSource"]

_RESERVED_PREFIXES: tuple[str, ...] = ("acl:", "role:")
_SOURCE_NATIVE_PREFIXES: tuple[str, ...] = (
    "sf-profile:",
    "sf-permset:",
    "m365-group:",
)
"""Known source-native prefixes; extend as new mapped ACL vocabularies arrive."""

_M365_GROUP_PREFIX = "m365-group:"
_M365_OBJECT_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _stored_grant(grant: str) -> str:
    """Translate an explicit mapping namespace to the connector's ACL bytes.

    M365 ACLs deliberately retain raw Entra object ids. The mapping document
    still requires the unambiguous ``m365-group:`` direction marker; only this
    validated boundary removes it before the membership edge is persisted.
    Salesforce labels already carry their source-native namespace and therefore
    pass through byte-for-byte.
    """
    if not grant.startswith(_M365_GROUP_PREFIX):
        return grant
    object_id = grant.removeprefix(_M365_GROUP_PREFIX)
    if not _M365_OBJECT_ID.fullmatch(object_id):
        raise ValueError(
            f"m365-group grant must contain one canonical lowercase Entra object id: {grant!r}"
        )
    return object_id


class AclMappingSource:
    """Serve a validated, authoritative ACL mapping as a ``PrincipalSource``."""

    def __init__(
        self,
        name: str = "acl-mapping",
        entries: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        if not isinstance(name, str) or not name:
            raise ValueError(f"mapping source name must be a non-empty string, got {name!r}")

        parsed: list[tuple[str, PrincipalKind, tuple[str, ...]]] = []
        principal_ids: set[str] = set()

        for raw in entries:
            if not isinstance(raw, Mapping):
                raise ValueError(f"mapping entry must be an object, got {raw!r}")

            principal = raw.get("principal")
            if not isinstance(principal, str) or not principal:
                raise ValueError(f"principal must be a non-empty string, got {principal!r}")
            if principal.startswith(_RESERVED_PREFIXES):
                raise ValueError(f"principal uses a reserved namespace: {principal!r}")
            if principal.startswith(_SOURCE_NATIVE_PREFIXES):
                raise ValueError(
                    "principal looks source-native "
                    f"(mapping direction may be inverted): {principal!r}"
                )
            if principal in principal_ids:
                raise ValueError(f"duplicate principal entry: {principal!r}")

            kind_value = raw.get("kind", PrincipalKind.GROUP.value)
            try:
                kind = PrincipalKind(kind_value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"unknown principal kind: {kind_value!r}") from exc

            grants_value = raw.get("grants")
            if not isinstance(grants_value, list) or not grants_value:
                raise ValueError(f"grants must be a non-empty list, got {grants_value!r}")

            grants: list[str] = []
            seen_grants: set[str] = set()
            for grant in grants_value:
                if not isinstance(grant, str) or not grant:
                    raise ValueError(f"grant must be a non-empty string, got {grant!r}")
                if grant.startswith(_RESERVED_PREFIXES):
                    raise ValueError(f"grant uses a reserved namespace: {grant!r}")
                if not grant.startswith(_SOURCE_NATIVE_PREFIXES):
                    raise ValueError(
                        "grant must use a known source-native namespace "
                        f"{_SOURCE_NATIVE_PREFIXES!r}: {grant!r}"
                    )
                stored = _stored_grant(grant)
                if stored not in seen_grants:
                    grants.append(stored)
                    seen_grants.add(stored)

            parsed.append((principal, kind, tuple(grants)))
            principal_ids.add(principal)

        for _, _, entry_grants in parsed:
            for grant in entry_grants:
                if grant in principal_ids:
                    raise ValueError(
                        f"grant also appears as a principal (mapping direction or cycle): {grant!r}"
                    )

        principal_rows = [
            SyncedPrincipal(principal_id=principal, kind=kind) for principal, kind, _ in parsed
        ]
        grant_ids: set[str] = set()
        edges: list[MembershipEdge] = []
        for principal, _, entry_grants in parsed:
            for grant in entry_grants:
                edges.append(MembershipEdge(member_id=principal, group_id=grant))
                if grant not in grant_ids:
                    principal_rows.append(
                        SyncedPrincipal(principal_id=grant, kind=PrincipalKind.GROUP)
                    )
                    grant_ids.add(grant)

        self._name = name
        self._principals = tuple(principal_rows)
        self._edges = tuple(edges)
        self._replaced_members = tuple(principal for principal, _, _ in parsed)

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> AclMappingSource:
        """Build a source from a decoded mapping document."""
        entries = data.get("mappings", ())
        if not isinstance(entries, list):
            raise ValueError(f"mappings must be a list, got {entries!r}")
        return cls(name=data.get("name", "acl-mapping"), entries=entries)

    @classmethod
    def from_path(cls, path: str | Path) -> AclMappingSource:
        """Load a UTF-8 JSON mapping document from ``path``."""
        return cls.from_data(json.loads(Path(path).read_text(encoding="utf-8")))

    @property
    def name(self) -> str:
        return self._name

    async def fetch(self, state: Mapping[str, str]) -> PrincipalSnapshot:
        """Return the repeatable full snapshot while preserving sync state."""
        return PrincipalSnapshot(
            principals=self._principals,
            edges=self._edges,
            replaced_members=self._replaced_members,
            is_full=True,
            state=dict(state),
        )
