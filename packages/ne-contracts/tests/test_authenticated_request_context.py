# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ADR-0023 authenticated-request-context contract controls."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from ne.contracts import AuthenticatedRequestContext, TokenClaims, TokenVerifier


def _field(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return len(encoded).to_bytes(8, "big") + encoded


def _expected_hash() -> str:
    values = (
        "1",
        "user:ada",
        "https://issuer.example/realms/ne",
        "1800000000",
        "2",
        "engineering",
        "role:developer",
        "1",
        "engineering",
    )
    material = b"".join(
        (
            _field("ne.authenticated-request-context.v1"),
            *(_field(value) for value in values),
        )
    )
    return hashlib.sha256(material).hexdigest()


def _context(**changes: object) -> AuthenticatedRequestContext:
    values: dict[str, object] = {
        "principal_id": "user:ada",
        "authorization_groups": ("engineering", "role:developer"),
        "department": "engineering",
        "issuer": "https://issuer.example/realms/ne",
        "expires_at": 1_800_000_000,
    }
    values.update(changes)
    return AuthenticatedRequestContext.model_validate(values)


def test_token_claims_expiry_is_defaulted_for_protocol_compatibility() -> None:
    claims = TokenClaims(subject="user:ada", issuer="https://issuer.example")
    assert claims.verified_expires_at is None

    class NondurableVerifier:
        async def verify(self, token: str) -> TokenClaims:
            return TokenClaims(subject=token, issuer="https://issuer.example")

    verifier: TokenVerifier = NondurableVerifier()
    assert verifier is not None


def test_token_claims_accept_only_exact_integer_second_utc_expiry() -> None:
    expires = datetime(2027, 1, 15, 8, 0, tzinfo=UTC)
    claims = TokenClaims(subject="user:ada", issuer="issuer", verified_expires_at=expires)
    assert claims.verified_expires_at == expires
    assert claims.verified_expires_at is not None
    assert claims.verified_expires_at.tzinfo is UTC

    invalid = (
        datetime(2027, 1, 15, 8, 0),
        datetime(2027, 1, 15, 8, 0, tzinfo=timezone(timedelta(hours=1))),
        datetime(2027, 1, 15, 8, 0, 0, 1, tzinfo=UTC),
        "2027-01-15T08:00:00Z",
        1_800_000_000,
    )
    for value in invalid:
        with pytest.raises(ValidationError):
            TokenClaims.model_validate(
                {"subject": "user:ada", "issuer": "issuer", "verified_expires_at": value}
            )


def test_context_hash_is_domain_separated_and_length_prefixed() -> None:
    context = _context()
    assert context.context_hash == _expected_hash()
    assert AuthenticatedRequestContext.model_validate(context.model_dump()) == context


@pytest.mark.parametrize(
    "changes",
    [
        {"principal_id": "user:bob"},
        {"authorization_groups": ("engineering", "role:approver")},
        {"department": "finance"},
        {"department": None},
        {"issuer": "https://issuer.example/realms/other"},
        {"expires_at": 1_800_000_001},
    ],
)
def test_every_authorization_coordinate_moves_the_hash(changes: dict[str, object]) -> None:
    assert _context(**changes).context_hash != _context().context_hash


def test_context_rejects_unsorted_or_duplicate_groups_and_hash_forgery() -> None:
    with pytest.raises(ValidationError, match="sorted and unique"):
        _context(authorization_groups=("role:developer", "engineering"))
    with pytest.raises(ValidationError, match="sorted and unique"):
        _context(authorization_groups=("engineering", "engineering"))
    with pytest.raises(ValidationError, match="context_hash"):
        _context(context_hash="f" * 64)


@pytest.mark.parametrize("expires_at", [True, 1.5, "1800000000", -1, 253_402_300_800])
def test_context_requires_bounded_integer_numeric_date(expires_at: object) -> None:
    with pytest.raises(ValidationError):
        _context(expires_at=expires_at)


def test_context_structurally_excludes_profile_session_and_arbitrary_claims() -> None:
    assert set(AuthenticatedRequestContext.model_fields) == {
        "schema_version",
        "principal_id",
        "authorization_groups",
        "department",
        "issuer",
        "expires_at",
        "context_hash",
    }
    for name in ("email", "name", "preferred_username", "sid", "claims", "token"):
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            _context(**{name: "private"})
