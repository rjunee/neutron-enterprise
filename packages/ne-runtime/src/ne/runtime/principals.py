# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The NE-036 agent-identity SEAM (interface only) — resolve an agent → its Principal.

This module defines ONLY the seam a cron job needs to run under its agent's
identity: :class:`AgentPrincipalProvider`. NE-036 (a separate lane/worktree) ships
the production, SecretStore-scoped implementation that mints a per-agent Principal
with the right claims; ne-runtime must not touch ``ne-identity``. Until then, a
:class:`StaticAgentPrincipalProvider` (an explicit mapping) is the seam a
composition root supplies.

Fail-closed: an unknown agent raises ``LookupError`` — there is NO default
principal. A cron job whose agent has no identity does not run under some ambient
identity; it fails audibly (the scheduler records the failure).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from ne.contracts.types import Principal


class AgentPrincipalProvider(Protocol):
    """Resolve the :class:`Principal` an agent runs as (NE-036 identity seam).

    Implementations MUST raise ``LookupError`` for an unknown agent (fail closed —
    no ambient/default identity). NE-036 ships the SecretStore-scoped impl.
    """

    def principal_for(self, agent: str) -> Principal: ...


class StaticAgentPrincipalProvider:
    """A fixed agent→Principal mapping (the seam a composition root supplies pre-NE-036).

    Raises ``LookupError`` on a miss (fail closed, no default) — matching the
    :class:`AgentPrincipalProvider` contract.
    """

    def __init__(self, mapping: Mapping[str, Principal]) -> None:
        self._mapping = dict(mapping)

    def principal_for(self, agent: str) -> Principal:
        try:
            return self._mapping[agent]
        except KeyError as exc:
            raise LookupError(
                f"no principal registered for agent {agent!r}; a cron/sub-agent "
                "identity must be explicitly provisioned (fail closed, no default)"
            ) from exc
