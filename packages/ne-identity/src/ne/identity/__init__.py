# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ne-identity — OIDC authn spine: fail-closed JWT verification, JIT
provisioning + config-driven group->role mapping, and a pure-ASGI OIDC
middleware, all pinned to the local Keycloak issuer (one issuer per deployment; C1)."""

from __future__ import annotations

from ne.identity import errors
from ne.identity.agents import (
    AgentCredential,
    AgentCredentialConfig,
    AgentCredentialService,
    AgentIdentitySettings,
    AgentRef,
    AgentScopeGuardrail,
    CredentialScope,
    CredentialState,
    bind_agent_principal,
    create_agent_credential_service,
)
from ne.identity.agents.binding import (
    CLAIM_AGENT_NAME,
    CLAIM_CREDENTIAL_ID,
    CLAIM_DEPARTMENT,
    CLAIM_ON_BEHALF_OF,
    CLAIM_SCOPE_SOURCES,
    CLAIM_SCOPE_TOOLS,
)
from ne.identity.config import IdentitySettings, OidcConfig, RoleMappingConfig
from ne.identity.factory import build_identity, build_token_verifier
from ne.identity.jwks import HttpJwksSource, KeySource, StaticJwksSource
from ne.identity.middleware import OidcAuthMiddleware, current_principal
from ne.identity.provisioning import (
    InMemoryPrincipalStore,
    JitPrincipalResolver,
    JitProvisioner,
    PrincipalStore,
)
from ne.identity.roles import ROLE_PREFIX, map_groups_to_roles, sanitize_idp_groups
from ne.identity.verifier import OidcTokenVerifier

__version__ = "0.1.0"

__all__ = [
    "CLAIM_AGENT_NAME",
    "CLAIM_CREDENTIAL_ID",
    "CLAIM_DEPARTMENT",
    "CLAIM_ON_BEHALF_OF",
    "CLAIM_SCOPE_SOURCES",
    "CLAIM_SCOPE_TOOLS",
    "ROLE_PREFIX",
    "AgentCredential",
    "AgentCredentialConfig",
    "AgentCredentialService",
    "AgentIdentitySettings",
    "AgentRef",
    "AgentScopeGuardrail",
    "CredentialScope",
    "CredentialState",
    "HttpJwksSource",
    "IdentitySettings",
    "InMemoryPrincipalStore",
    "JitPrincipalResolver",
    "JitProvisioner",
    "KeySource",
    "OidcAuthMiddleware",
    "OidcConfig",
    "OidcTokenVerifier",
    "PrincipalStore",
    "RoleMappingConfig",
    "StaticJwksSource",
    "bind_agent_principal",
    "build_identity",
    "build_token_verifier",
    "create_agent_credential_service",
    "current_principal",
    "errors",
    "map_groups_to_roles",
    "sanitize_idp_groups",
]
