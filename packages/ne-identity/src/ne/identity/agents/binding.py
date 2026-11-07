# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Principal binding for agent identity (NE-036).

Produces an agent-scoped :class:`~ne.contracts.types.Principal` that NE-033's
``DepartmentAgentSession`` accepts (its ``claims['department']`` gate) and that
the PDP / guardrails / audit see as the acting subject (C6 — the audit trail
records WHICH agent identity acted).

Anti-escalation (least privilege): a bound agent principal gets ONLY the two
agent groups (``agent:<dept>/<agent>`` and ``agent:<dept>``). It deliberately does
NOT inherit the delegating human's groups, so a compromised agent can never
escalate to its delegator's authority — mirroring NE-060's role-mapping posture.

The claim keys are cross-package string seams. ``CLAIM_DEPARTMENT`` MUST equal the
literal ``DepartmentAgentSession`` reads (``"department"``); a dedicated test pins
it so drift fails loudly.
"""

from __future__ import annotations

from ne.contracts.types import Principal
from ne.identity.agents.model import AgentCredential, CredentialScope

#: MUST equal the literal ne-runtime DepartmentAgentSession reads from claims.
CLAIM_DEPARTMENT = "department"
CLAIM_AGENT_NAME = "agent_name"
CLAIM_CREDENTIAL_ID = "agent_credential_id"
CLAIM_SCOPE_TOOLS = "agent_scope_tools"
CLAIM_SCOPE_SOURCES = "agent_scope_sources"
CLAIM_ON_BEHALF_OF = "on_behalf_of"


def _join(values: tuple[str, ...]) -> str:
    return ",".join(sorted(values))


def _split(value: str) -> tuple[str, ...]:
    return tuple(part for part in value.split(",") if part)


def bind_agent_principal(
    credential: AgentCredential,
    *,
    on_behalf_of: Principal | None = None,
) -> Principal:
    """Mint an agent-scoped principal from a credential's metadata (no secret).

    The principal id is ``agent:<dept>/<agent>``; its groups are the two agent
    groups only (no delegator-group inheritance). The scope is carried as
    comma-joined, sorted claim strings so the round-trip via
    :func:`scope_from_principal` is stable.
    """
    key = credential.agent.key
    claims: dict[str, str] = {
        CLAIM_DEPARTMENT: credential.agent.department,
        CLAIM_AGENT_NAME: credential.agent.agent_name,
        CLAIM_CREDENTIAL_ID: credential.credential_id,
        CLAIM_SCOPE_TOOLS: _join(credential.scope.tools),
        CLAIM_SCOPE_SOURCES: _join(credential.scope.source_systems),
    }
    if on_behalf_of is not None:
        claims[CLAIM_ON_BEHALF_OF] = on_behalf_of.id
    return Principal(
        id=f"agent:{key}",
        groups=(f"agent:{key}", f"agent:{credential.agent.department}"),
        claims=claims,
    )


def credential_id_from_principal(principal: Principal) -> str | None:
    """Return the bound credential id, or None if this is not an agent principal."""
    value = principal.claims.get(CLAIM_CREDENTIAL_ID)
    return value if isinstance(value, str) and value else None


def scope_from_principal(principal: Principal) -> CredentialScope | None:
    """Reconstruct the :class:`CredentialScope` from an agent principal's claims.

    Returns None when the principal carries no agent credential id (not an agent
    principal). An empty scope claim maps to an empty tuple (authorizes nothing).

    WARNING: this reconstructs the scope the principal CLAIMS. Claims are
    forgeable — never use this for authorization; AgentScopeGuardrail authorizes
    against the credential's stored scope via AgentCredentialService.validate().
    """
    if credential_id_from_principal(principal) is None:
        return None
    tools = principal.claims.get(CLAIM_SCOPE_TOOLS, "")
    sources = principal.claims.get(CLAIM_SCOPE_SOURCES, "")
    return CredentialScope(
        tools=_split(tools if isinstance(tools, str) else ""),
        source_systems=_split(sources if isinstance(sources, str) else ""),
    )
