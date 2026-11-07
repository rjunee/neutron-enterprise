# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""StorePrincipalResolver — the store-backed ``PrincipalResolver`` (C5 slot).

Implements the FROZEN ``ne.contracts.identity.PrincipalResolver`` Protocol by
resolving a subject to its expanded group set. Registered under the
``ne.plugins.principal_resolver`` entry point as ``store``, so it is swappable
with ne-identity's JIT resolver.
"""

from __future__ import annotations

from ne.contracts.types import Principal
from ne.permissions.expand import PrincipalExpander

__all__ = ["StorePrincipalResolver"]


class StorePrincipalResolver:
    """Resolve a subject to a :class:`Principal` via the principal store."""

    def __init__(self, expander: PrincipalExpander) -> None:
        self._expander = expander

    async def resolve(self, subject: str) -> Principal:
        """Return ``Principal(id=subject, groups=<sorted expansion minus self>)``."""
        groups = tuple(sorted(self._expander.expand(subject) - {subject}))
        return Principal(id=subject, groups=groups)
