# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Config-driven construction of the identity authn spine.

``build_token_verifier`` resolves the ``ne.plugins.token_verifier`` entry point
named by ``settings.verifier`` and constructs it from the pinned OIDC config.
``build_identity`` is the convenience wiring for the ne-api process: it builds
the verifier, an in-memory principal store, the JIT provisioner, and installs
the :class:`~ne.identity.middleware.OidcAuthMiddleware`.

An optional ``audit_sink`` (NE-BEW-GROUPS-ZEROGROUPS) wires the JIT
provisioner's zero-mapped-groups signal to the platform's queryable audit
store; without one, the signal degrades to the structured WARNING log only —
it is never fully silent either way.
"""

from __future__ import annotations

from ne.contracts.governance import AuditSink
from ne.contracts.identity import TokenVerifier
from ne.contracts.plugins import PluginRegistry
from ne.identity.config import IdentitySettings
from ne.identity.middleware import OidcAuthMiddleware
from ne.identity.provisioning import InMemoryPrincipalStore, JitProvisioner, PrincipalStore


def build_token_verifier(
    settings: IdentitySettings, registry: PluginRegistry | None = None
) -> TokenVerifier:
    """Resolve and construct the configured TokenVerifier adapter."""
    registry = registry or PluginRegistry()
    cls = registry.resolve("token_verifier", settings.verifier)
    verifier: TokenVerifier = cls(settings.oidc)
    return verifier


def build_identity(
    settings: IdentitySettings,
    app: object,
    *,
    store: PrincipalStore | None = None,
    registry: PluginRegistry | None = None,
    audit_sink: AuditSink | None = None,
) -> OidcAuthMiddleware:
    """Wire verifier + store + JIT provisioner + middleware around ``app``.

    Returns the installed :class:`OidcAuthMiddleware` (itself an ASGI app).
    """
    verifier = build_token_verifier(settings, registry)
    principal_store = store or InMemoryPrincipalStore()
    provisioner = JitProvisioner(
        principal_store,
        settings.roles,
        groups_claim=settings.oidc.groups_claim,
        principal_claims=settings.principal_claims,
        idp_claim=settings.oidc.idp_claim,
        audit_sink=audit_sink,
    )
    return OidcAuthMiddleware(
        app,  # type: ignore[arg-type]
        verifier=verifier,
        provisioner=provisioner,
        exempt_paths=settings.oidc.exempt_paths,
    )
