# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""OidcClaimsPrincipalSource — the generic OIDC-claims sync mode.

Does NOT verify tokens: NE-060 / ne-identity owns token verification (this source
CONSUMES already-verified :class:`ne.contracts.identity.TokenClaims` only). Group
membership is derived from a configurable groups claim; anything that is not a
list/tuple of strings yields NO groups (never an error — the principal is still
recorded). Observation-driven, so it never tombstones absentees (``is_full`` is
always ``False``).

Reserved ``role:*`` / ``acl:*`` groups are dropped at extraction, so a forged
claim can never seed one of the reserved ACL labels (``acl:tenant-all`` /
``acl:public-link``) into the principal store where it would become BFS-reachable
in :func:`ne.permissions.expand.PrincipalExpander.expand` and widen retrieval
(Checkpoint-D finding #2, C7).
"""

from __future__ import annotations

from collections.abc import Mapping

from ne.contracts.identity import TokenClaims
from ne.permissions.config import PermissionsSettings
from ne.permissions.models import (
    MembershipEdge,
    PrincipalKind,
    PrincipalSnapshot,
    SyncedPrincipal,
)

__all__ = ["OidcClaimsPrincipalSource"]

# Mirrors ne.identity.roles.RESERVED_PREFIXES — ne-permissions cannot import
# ne-identity (dependency direction is contracts-only), so keep the two in sync.
# ``role:*`` is the platform-role namespace; ``acl:*`` is the reserved ACL-label
# namespace (TENANT_ALL / PUBLIC_LINK in ne.permissions.models) and must never
# enter the principal store as a group id — it would become BFS-reachable in
# expand() and widen retrieval (Checkpoint-D finding #2, C7).
_RESERVED_GROUP_PREFIXES = ("role:", "acl:")


class OidcClaimsPrincipalSource:
    """Turn verified token claims into principal + membership snapshots."""

    def __init__(self, settings: PermissionsSettings) -> None:
        self._groups_claim = settings.groups_claim
        # subject -> group ids from its LAST observation (last-write-wins).
        self._buffer: dict[str, tuple[str, ...]] = {}

    @property
    def name(self) -> str:
        return "oidc-claims"

    def observe(self, claims: TokenClaims) -> None:
        """Record a verified subject + its groups; last observation wins."""
        self._buffer[claims.subject] = self._extract_groups(claims)

    def _extract_groups(self, claims: TokenClaims) -> tuple[str, ...]:
        raw = claims.claims.get(self._groups_claim)
        if isinstance(raw, (list, tuple)) and all(isinstance(g, str) for g in raw):
            return tuple(g for g in raw if not g.startswith(_RESERVED_GROUP_PREFIXES))
        # Absent / scalar / mixed-type groups claim → no groups (never an error).
        return ()

    async def fetch(self, state: Mapping[str, str]) -> PrincipalSnapshot:
        """Drain the observation buffer into a delta snapshot (never tombstones)."""
        observed = self._buffer
        self._buffer = {}

        principals: list[SyncedPrincipal] = []
        edges: list[MembershipEdge] = []
        replaced_members: list[str] = []
        for subject, groups in observed.items():
            principals.append(SyncedPrincipal(principal_id=subject, kind=PrincipalKind.USER))
            replaced_members.append(subject)
            for group in groups:
                principals.append(SyncedPrincipal(principal_id=group, kind=PrincipalKind.GROUP))
                edges.append(MembershipEdge(member_id=subject, group_id=group))

        return PrincipalSnapshot(
            principals=tuple(principals),
            edges=tuple(edges),
            replaced_members=tuple(replaced_members),
            is_full=False,
            state=dict(state),
        )
