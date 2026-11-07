# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Fail-closed Bot Framework activity JWT validation (NE-080) — pure, guardian-safe.

:class:`TeamsActivityAuthenticator` verifies the ``Authorization: Bearer <jwt>``
an inbound Bot Framework activity carries, mirroring
``ne.identity.verifier.OidcTokenVerifier``'s posture: every failure funnels to the
SINGLE opaque :class:`~ne.chat.errors.ActivityAuthError` — no branch leaks WHY a
token was rejected (no oracle). This module imports NO httpx (the C2 egress
guardian keeps ``ne.chat.teams.client`` the sole egress site); the LIVE
:class:`KeySource` is the client, tests inject :class:`StaticJwksKeySource`.

Attack surface addressed (SPEC §5 NE-080 forged-activity matrix):

* ``alg:none`` / ``HS*`` confusion — the header ``alg`` must be exactly
  ``RS256``, checked BEFORE any key is fetched (a symmetric/unsigned token never
  reaches key resolution);
* key/alg-family confusion — the resolved JWK's declared ``alg`` (if present)
  must equal ``RS256`` and its ``kty`` must be ``RSA``;
* issuer substitution — the ``iss`` claim must be in ``allowed_issuers`` (manual
  multi-issuer check: multi-tenant ``api.botframework.com`` plus any configured
  single-tenant issuer);
* audience / expiry — ``aud`` must equal the bot ``app_id``; ``exp`` / ``nbf``
  are enforced with the configured leeway; ``exp`` / ``iss`` / ``aud`` are
  REQUIRED claims;
* serviceUrl binding — the ``serviceurl`` claim (lowercase, per the Bot Connector
  spec) must match the activity's ``serviceUrl`` (both rstrip-``/`` normalized) so
  a valid token minted for one bot cannot be replayed to redirect replies.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

import jwt

from ne.chat.errors import ActivityAuthError
from ne.chat.teams.config import TeamsChatSettings

__all__ = ["KeySource", "StaticJwksKeySource", "TeamsActivityAuthenticator"]

_logger = logging.getLogger(__name__)
_ALLOWED_ALGS = frozenset({"RS256"})
#: The SINGLE opaque message every rejection surfaces — no branch leaks WHY.
_OPAQUE_MESSAGE = "activity authorization failed"


def _reject(reason: str) -> ActivityAuthError:
    """Log the REASON only (never token material) and return the opaque error."""
    _logger.warning("activity token rejected: %s", reason)
    return ActivityAuthError(_OPAQUE_MESSAGE)


class KeySource(Protocol):
    """Resolves a JWT ``kid`` to a verified :class:`jwt.PyJWK` signing key."""

    async def get_signing_key(self, kid: str) -> jwt.PyJWK: ...


class StaticJwksKeySource:
    """A fixed, in-memory JWKS. No network. Used by CI tests and air-gapped runs."""

    def __init__(self, jwks: dict[str, Any]) -> None:
        try:
            self._set = jwt.PyJWKSet.from_dict(jwks)
        except (jwt.PyJWKError, jwt.InvalidKeyError, KeyError, TypeError, ValueError) as exc:
            raise _reject("invalid JWKS") from exc

    async def get_signing_key(self, kid: str) -> jwt.PyJWK:
        for key in self._set.keys:
            if key.key_id == kid:
                return key
        raise _reject("no signing key for kid")


class TeamsActivityAuthenticator:
    """Verifies a Bot Framework activity JWT, fail-closed to one opaque error."""

    def __init__(self, settings: TeamsChatSettings, key_source: KeySource) -> None:
        self._settings = settings
        self._key_source = key_source

    async def authenticate(
        self, authorization_header: str | None, activity_service_url: str
    ) -> None:
        """Validate the header's bearer JWT against the activity's serviceUrl.

        Returns ``None`` on success; raises the single opaque
        :class:`ActivityAuthError` on ANY failure.
        """
        token = self._bearer_token(authorization_header)

        try:
            header = jwt.get_unverified_header(token)
        except (jwt.PyJWTError, ValueError, TypeError, AttributeError) as exc:
            raise _reject("malformed activity token header") from exc

        alg = header.get("alg")
        if not isinstance(alg, str) or alg not in _ALLOWED_ALGS:
            # Kills alg:none + HS* confusion BEFORE any key is fetched.
            raise _reject(f"algorithm not permitted: {alg!r}")

        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise _reject("token header missing kid")

        try:
            key = await self._key_source.get_signing_key(kid)
        except ActivityAuthError:
            raise _reject("key source could not resolve kid") from None

        declared_alg = getattr(key, "_jwk_data", {}).get("alg")
        if isinstance(declared_alg, str) and declared_alg not in _ALLOWED_ALGS:
            raise _reject("JWK declared alg does not match token alg")
        if key.key_type != "RSA":
            raise _reject("key type does not match token algorithm family")

        settings = self._settings
        try:
            payload: dict[str, Any] = jwt.decode(
                token,
                key=key.key,
                algorithms=["RS256"],
                audience=settings.app_id,
                leeway=settings.jwt_leeway_s,
                options={
                    "require": ["exp", "iss", "aud"],
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_nbf": True,
                    "verify_aud": True,
                },
            )
        except (jwt.PyJWTError, ValueError, TypeError) as exc:
            raise _reject("token failed verification") from exc

        # Manual multi-issuer check (multi-tenant + any single-tenant issuer).
        if payload.get("iss") not in settings.allowed_issuers:
            raise _reject("issuer not permitted")

        # serviceUrl binding: the lowercase 'serviceurl' claim must match the
        # activity's serviceUrl (both rstrip-'/' normalized).
        claim_service_url = payload.get("serviceurl")
        if not isinstance(claim_service_url, str) or not claim_service_url:
            raise _reject("missing serviceurl claim")
        if claim_service_url.rstrip("/") != activity_service_url.rstrip("/"):
            raise _reject("serviceurl claim does not match the activity")

    @staticmethod
    def _bearer_token(authorization_header: str | None) -> str:
        if not authorization_header:
            raise _reject("missing authorization header")
        parts = authorization_header.split(" ", 1)
        if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
            raise _reject("authorization header is not a bearer token")
        return parts[1].strip()
