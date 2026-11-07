# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Exception hierarchy for ne-identity.

The authn spine fails closed: EVERY token-verification failure — expired, bad
signature, wrong audience/issuer, rejected algorithm, missing claim, malformed
token, unknown ``kid``, JWKS fetch failure — surfaces as the SINGLE opaque
:class:`TokenVerificationError`. Callers (and the OIDC middleware) therefore
cannot branch on *why* a token was rejected, so the 401 responses carry no
authentication oracle.
"""

from __future__ import annotations


class IdentityError(Exception):
    """Base class for all ne-identity errors."""


class TokenVerificationError(IdentityError):
    """A bearer token failed verification.

    This is the single, opaque failure type raised for every verification
    failure. It deliberately does not distinguish the underlying cause so no
    authentication oracle leaks to the caller.
    """


class JwksError(TokenVerificationError):
    """A signing key could not be resolved (JWKS fetch/parse/unknown-kid).

    A subclass of :class:`TokenVerificationError` so that a key-resolution
    failure is, from the caller's perspective, indistinguishable from any other
    verification failure (fail-closed, no oracle).
    """


class UnknownPrincipalError(IdentityError):
    """``resolve()`` was called for a subject that was never provisioned.

    Resolution never creates principals — provisioning happens only from a
    verified token — so a lookup miss is a distinct, non-verification error.
    """


# --- Agent identity (NE-036) ------------------------------------------------
#
# Error MESSAGES carry credential_id / agent key / state ONLY — never secret
# material (C1). These are additive to the frozen NE-060 authn-spine surface.


class AgentCredentialError(IdentityError):
    """Base class for per-agent service-credential errors (NE-036)."""


class CredentialNotFoundError(AgentCredentialError):
    """No credential is registered under the given id (or none is usable)."""


class CredentialRevokedError(AgentCredentialError):
    """A revoked credential was used — fail closed (revocation is immediate)."""


class CredentialExpiredError(AgentCredentialError):
    """A credential past its grace window (or otherwise expired) was used."""


class CredentialAlreadyProvisionedError(AgentCredentialError):
    """A live credential already exists for this agent — issue is refused."""


class CredentialScopeError(AgentCredentialError):
    """A credential was used for a tool outside its least-privilege scope."""


class CredentialAuditError(AgentCredentialError):
    """The audit append for an issue/rotate/revoke failed — fail closed.

    An unaudited issue is unwound (the secret version is destroyed and the
    credential deregistered); an unaudited revoke still REVOKES (revocation is
    immediate and stands) but this error is still raised.
    """
