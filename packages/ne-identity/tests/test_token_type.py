# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Token-type gate (NE-FIX-IDENTITY P2-2).

A signature/iss/aud/exp/sub-valid JWT is accepted ONLY when it carries an
access-token marker (Keycloak payload ``typ == "Bearer"`` or RFC 9068 header
``typ`` of ``at+jwt``). OIDC ID tokens, Refresh tokens, and marker-less JWTs are
rejected with exactly the opaque ``token verification failed`` message so the
verifier leaks no oracle.
"""

from __future__ import annotations

from typing import Any

import pytest

from ne.identity.errors import TokenVerificationError
from ne.identity.verifier import OidcTokenVerifier

from .conftest import run


def test_id_token_typ_rejected_opaquely(verifier: OidcTokenVerifier, mint: Any) -> None:
    # The minted-ID-token attack: a fully valid token whose payload typ is 'ID'
    # (as Keycloak stamps ID tokens) must be rejected, opaquely.
    token = mint({"typ": "ID"})
    with pytest.raises(TokenVerificationError) as excinfo:
        run(verifier.verify(token))
    assert str(excinfo.value) == "token verification failed"


def test_refresh_token_typ_rejected(verifier: OidcTokenVerifier, mint: Any) -> None:
    with pytest.raises(TokenVerificationError):
        run(verifier.verify(mint({"typ": "Refresh"})))


def test_missing_typ_marker_rejected(verifier: OidcTokenVerifier, mint: Any, DROP: Any) -> None:
    with pytest.raises(TokenVerificationError):
        run(verifier.verify(mint({"typ": DROP})))


def test_rfc9068_at_jwt_header_accepted(verifier: OidcTokenVerifier, mint: Any, DROP: Any) -> None:
    # An RFC 9068 access token carries the marker in the HEADER typ, not payload.
    token = mint({"typ": DROP}, headers={"typ": "at+jwt"})
    claims = run(verifier.verify(token))
    assert claims.subject == "user-1"
    # Case-insensitive: an upper-case header marker is also accepted.
    token_upper = mint({"typ": DROP}, headers={"typ": "AT+JWT"})
    assert run(verifier.verify(token_upper)).subject == "user-1"


def test_bearer_payload_typ_accepted(verifier: OidcTokenVerifier, mint: Any) -> None:
    # A plain Keycloak access token (payload typ='Bearer', from the default mint).
    claims = run(verifier.verify(mint()))
    assert claims.subject == "user-1"
