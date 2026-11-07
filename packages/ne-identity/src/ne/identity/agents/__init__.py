# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Agent identity (NE-036) — scoped per-agent service credentials + rotation.

A new subpackage layered on top of the frozen NE-060 authn spine. It issues,
rotates, and revokes least-privilege per-agent service credentials whose secret
material lives ONLY in the frozen SecretStore (NE-007), binds an agent-scoped
:class:`~ne.contracts.types.Principal` accepted by NE-033's
``DepartmentAgentSession`` (C6 attribution), and enforces scope with a sync
``Guardrail`` — all audited through the frozen ``AuditSink``.
"""

from __future__ import annotations

from ne.identity.agents.binding import (
    CLAIM_AGENT_NAME,
    CLAIM_CREDENTIAL_ID,
    CLAIM_DEPARTMENT,
    CLAIM_ON_BEHALF_OF,
    CLAIM_SCOPE_SOURCES,
    CLAIM_SCOPE_TOOLS,
    bind_agent_principal,
    credential_id_from_principal,
    scope_from_principal,
)
from ne.identity.agents.config import AgentCredentialConfig, AgentIdentitySettings
from ne.identity.agents.factory import create_agent_credential_service
from ne.identity.agents.guardrail import AgentScopeGuardrail
from ne.identity.agents.model import (
    AgentCredential,
    AgentRef,
    CredentialScope,
    CredentialState,
)
from ne.identity.agents.pool import (
    CooldownReason,
    CredentialPoolState,
    PoolEntry,
)
from ne.identity.agents.service import AgentCredentialService

__all__ = [
    "CLAIM_AGENT_NAME",
    "CLAIM_CREDENTIAL_ID",
    "CLAIM_DEPARTMENT",
    "CLAIM_ON_BEHALF_OF",
    "CLAIM_SCOPE_SOURCES",
    "CLAIM_SCOPE_TOOLS",
    "AgentCredential",
    "AgentCredentialConfig",
    "AgentCredentialService",
    "AgentIdentitySettings",
    "AgentRef",
    "AgentScopeGuardrail",
    "CooldownReason",
    "CredentialPoolState",
    "CredentialScope",
    "CredentialState",
    "PoolEntry",
    "bind_agent_principal",
    "create_agent_credential_service",
    "credential_id_from_principal",
    "scope_from_principal",
]
