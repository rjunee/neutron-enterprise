# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The package-local ``PrincipalSource`` Protocol (C5 swappable slot).

A source enumerates principals + membership edges from some backing directory
(Entra ID, an OIDC-claims buffer, a recorded fixture) behind one clean async
surface. Sources register via the ``ne.plugins.principal_source`` entry point.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from ne.permissions.models import PrincipalSnapshot

__all__ = ["PrincipalSource"]


@runtime_checkable
class PrincipalSource(Protocol):
    """Enumerate principals + memberships from a backing directory.

    ``state`` is the persisted sync-state map (delta links etc.) the store handed
    back from the previous run; the returned ``snapshot.state`` REPLACES it after
    a successful apply. ``snapshot.is_full=True`` means the snapshot enumerates
    the whole directory (absent principals are tombstoned); ``is_full=False`` is a
    delta (only replaced groups/members and removals are applied).
    """

    @property
    def name(self) -> str: ...

    async def fetch(self, state: Mapping[str, str]) -> PrincipalSnapshot: ...
