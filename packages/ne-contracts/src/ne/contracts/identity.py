# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Identity domain Protocols (SPEC section 4).

Principal resolution and token verification. Every service trusts only the
local OIDC issuer (one issuer per deployment — follows from C1 self-host).
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Annotated, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ne.contracts.frozen import AnyMap, FrozenDict
from ne.contracts.types import Principal


class TokenClaims(BaseModel):
    """Verified claims extracted from an identity token."""

    model_config = ConfigDict(frozen=True)

    subject: str
    issuer: str
    claims: AnyMap = FrozenDict()
    # Defaulted for source compatibility with nondurable/test TokenVerifier
    # implementations. Only the production OIDC verifier authors this value;
    # delayed-work consumers require AuthenticatedRequestContext instead.
    verified_expires_at: datetime | None = None

    @field_validator("verified_expires_at", mode="before")
    @classmethod
    def _verified_expiry_is_exact_utc_datetime(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise ValueError("verified_expires_at must be an exact datetime")
        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("verified_expires_at must be timezone-aware UTC")
        if value.microsecond != 0:
            raise ValueError("verified_expires_at must use integer NumericDate seconds")
        return value.astimezone(UTC)


_CONTEXT_DOMAIN = "ne.authenticated-request-context.v1"
_MAX_NUMERIC_DATE = 253_402_300_799

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
OpaquePrincipalId = Annotated[str, Field(min_length=1, max_length=512)]
AuthorizationGroup = Annotated[str, Field(min_length=1, max_length=512)]
PolicyDepartment = Annotated[str, Field(min_length=1, max_length=512)]
VerifiedIssuer = Annotated[str, Field(min_length=1, max_length=2048)]


def _field(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return len(encoded).to_bytes(8, "big") + encoded


class AuthenticatedRequestContext(BaseModel):
    """Minimal verifier-authored authorization snapshot for delayed work.

    Profile/display claims, bearer material and non-policy token claims are
    structurally absent. ``context_hash`` binds individually length-prefixed,
    domain-separated fields so component boundaries cannot collide.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Annotated[int, Field(strict=True, ge=1, le=1)] = 1
    principal_id: OpaquePrincipalId
    authorization_groups: tuple[AuthorizationGroup, ...]
    department: PolicyDepartment | None = None
    issuer: VerifiedIssuer
    expires_at: Annotated[int, Field(strict=True, ge=0, le=_MAX_NUMERIC_DATE)]
    context_hash: Sha256 | Annotated[str, Field(max_length=0)] = ""

    @model_validator(mode="after")
    def _bind_canonical_context_hash(self) -> AuthenticatedRequestContext:
        canonical_groups = tuple(sorted(set(self.authorization_groups)))
        if self.authorization_groups != canonical_groups:
            raise ValueError("authorization_groups must be sorted and unique")
        values = (
            str(self.schema_version),
            self.principal_id,
            self.issuer,
            str(self.expires_at),
            str(len(self.authorization_groups)),
            *self.authorization_groups,
            "1" if self.department is not None else "0",
            self.department or "",
        )
        material = b"".join((_field(_CONTEXT_DOMAIN), *(_field(value) for value in values)))
        expected = hashlib.sha256(material).hexdigest()
        if self.context_hash and self.context_hash != expected:
            raise ValueError("context_hash does not match canonical authenticated context")
        object.__setattr__(self, "context_hash", expected)
        return self


class PrincipalResolver(Protocol):
    """Resolve a subject identifier to a full :class:`Principal`."""

    async def resolve(self, subject: str) -> Principal: ...


class TokenVerifier(Protocol):
    """Verify a bearer token; trusts only the local OIDC issuer."""

    async def verify(self, token: str) -> TokenClaims: ...
