# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""OidcTokenVerifier — the authn spine's fail-closed bearer-JWT verifier.

Implements the FROZEN :class:`ne.contracts.identity.TokenVerifier` Protocol.
Trusts ONLY the pinned local issuer (one issuer per deployment; C1). Every step below fails
closed to the single opaque :class:`TokenVerificationError`; no branch leaks *why* a token
was rejected.

Attack surface addressed (SPEC §5 NE-060 security suite):

* ``alg:none`` / ``HS*`` confusion — the header ``alg`` must be in
  ``config.allowed_algs``, which construction-time validation already restricts
  to asymmetric JWKS-backed algorithms. A symmetric or unsigned token is
  rejected before any key is fetched.
* Key/alg-family confusion — the resolved JWK's declared ``alg`` (if present)
  must equal the header ``alg``, and the header-alg family must match the JWK
  ``kty`` (RS*/PS*->RSA, ES*->EC, EdDSA->OKP).
* Issuer substitution — exact ``iss`` comparison (PyJWT >= 2.10.1) plus the
  issuer-pinning already enforced in config and JWKS discovery.
* Missing claims — ``exp``, ``iat``, ``sub``, ``iss``, ``aud`` are required.
* Token-type confusion (NE-FIX-IDENTITY P2-2) — after the cryptographic verify,
  the token MUST carry an ACCESS-token marker: a Keycloak payload ``typ ==
  "Bearer"`` OR an RFC 9068 header ``typ`` of ``at+jwt`` (case-insensitive).
  An OIDC ID token (``typ == "ID"``), a Refresh token, or any marker-less JWT is
  rejected — so an ID token issued to a browser client can never be replayed as
  an API bearer credential and JIT-provision mapped roles.
* Reserved-namespace subject (NE-FIX-IDENTITY P2-1) — a verified ``sub`` that
  starts with a reserved ne-permissions/role prefix (``acl:*`` / ``role:*``,
  e.g. exactly ``acl:tenant-all``) is rejected, so it can never become a
  ``Principal.id`` and match every TENANT_ALL-stamped chunk without being
  store-known (defeats CKD2).
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

from ne.contracts.identity import TokenClaims, TokenVerifier
from ne.identity.config import OidcConfig
from ne.identity.errors import TokenVerificationError
from ne.identity.jwks import HttpJwksSource, KeySource
from ne.identity.roles import RESERVED_PREFIXES

# Sanctioned access-token type markers. Keycloak stamps the payload ``typ``
# claim ``"Bearer"`` on access tokens (``"ID"`` on ID tokens, ``"Refresh"`` on
# refresh tokens); RFC 9068 access tokens carry the header ``typ`` ``at+jwt``.
_ACCESS_TOKEN_HEADER_TYPS = frozenset({"at+jwt", "application/at+jwt"})
_ACCESS_TOKEN_PAYLOAD_TYP = "Bearer"

logger = logging.getLogger(__name__)

_NUMERIC_DATE_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MAX_NUMERIC_DATE = 253_402_300_799

# Maps a JWS algorithm family to the JWK key type (kty) it must be backed by.
_ALG_FAMILY_KTY = {
    "RS": "RSA",
    "PS": "RSA",
    "ES": "EC",
    "Ed": "OKP",
}


def _expected_kty(alg: str) -> str | None:
    return _ALG_FAMILY_KTY.get(alg[:2])


def _verified_expiry(raw: object) -> datetime:
    """Normalize one signature-verified JWT ``exp`` to exact UTC seconds."""

    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError("exp is not numeric")
    if isinstance(raw, float) and (not math.isfinite(raw) or not raw.is_integer()):
        raise ValueError("exp is not a finite integer NumericDate")
    seconds = int(raw)
    if seconds < 0 or seconds > _MAX_NUMERIC_DATE:
        raise ValueError("exp is outside the supported NumericDate range")
    try:
        return _NUMERIC_DATE_EPOCH + timedelta(seconds=seconds)
    except OverflowError as exc:
        raise ValueError("exp is outside the supported NumericDate range") from exc


class OidcTokenVerifier:
    """Verifies a bearer JWT against the pinned local issuer's JWKS."""

    def __init__(self, config: OidcConfig, key_source: KeySource | None = None) -> None:
        self._config = config
        self._key_source: KeySource = key_source or HttpJwksSource(config)

    async def verify(self, token: str) -> TokenClaims:
        config = self._config
        try:
            header = jwt.get_unverified_header(token)
        except (jwt.PyJWTError, ValueError, TypeError, AttributeError) as exc:
            raise self._reject("malformed token header") from exc

        alg = header.get("alg")
        if not isinstance(alg, str) or alg not in config.allowed_algs:
            raise self._reject(f"algorithm not permitted: {alg!r}")

        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise self._reject("token header missing kid")

        # JwksError is a TokenVerificationError subclass — propagates fail-closed.
        key = await self._key_source.get_signing_key(kid)

        # Algorithm-confusion guards, checked BEFORE the cryptographic verify.
        declared_alg = getattr(key, "_jwk_data", {}).get("alg")
        if isinstance(declared_alg, str) and declared_alg != alg:
            raise self._reject("JWK declared alg does not match token header alg")
        expected_kty = _expected_kty(alg)
        if expected_kty is not None and key.key_type != expected_kty:
            raise self._reject("key type does not match token algorithm family")

        try:
            payload: dict[str, Any] = jwt.decode(
                token,
                key=key.key,
                algorithms=list(config.allowed_algs),
                audience=config.audience,
                issuer=config.issuer,
                leeway=config.leeway_seconds,
                options={
                    "require": ["exp", "iat", "sub", "iss", "aud"],
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_nbf": True,
                    "verify_aud": True,
                    "verify_iss": True,
                },
            )
        except (jwt.PyJWTError, ValueError, TypeError) as exc:
            raise self._reject("token failed verification") from exc

        # Token-type gate (P2-2): the header is signature-covered, so reading it
        # post-decode is sound. Accept ONLY an access-token marker; reject OIDC
        # ID tokens (payload typ 'ID'), Refresh tokens, and marker-less JWTs.
        header_typ = header.get("typ")
        payload_typ = payload.get("typ")
        header_ok = isinstance(header_typ, str) and header_typ.lower() in _ACCESS_TOKEN_HEADER_TYPS
        if not (header_ok or payload_typ == _ACCESS_TOKEN_PAYLOAD_TYP):
            raise self._reject("token type is not an access token")

        # Reserved-namespace subject gate (P2-1): a reserved acl:*/role:* sub can
        # never become a principal id (would match every TENANT_ALL chunk).
        sub = payload.get("sub")
        if not isinstance(sub, str) or not sub:
            raise self._reject("subject claim missing or malformed")
        if sub.startswith(RESERVED_PREFIXES):
            raise self._reject("reserved-namespace subject")

        try:
            verified_expires_at = _verified_expiry(payload.get("exp"))
        except (ValueError, TypeError, OverflowError) as exc:
            raise self._reject("expiry claim missing or malformed") from exc

        return TokenClaims(
            subject=sub,
            issuer=payload["iss"],
            claims=payload,
            verified_expires_at=verified_expires_at,
        )

    @staticmethod
    def _reject(reason: str) -> TokenVerificationError:
        # Log the REASON only — never token material or unverified claims.
        logger.warning("token rejected: %s", reason)
        return TokenVerificationError("token verification failed")


# Static conformance to the frozen TokenVerifier Protocol (pyright-checked).
def _check_token_verifier(v: OidcTokenVerifier) -> TokenVerifier:
    return v
