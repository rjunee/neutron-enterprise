# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The authn-spine security suite — real RSA signatures, no Keycloak, no network.

Every rejection asserts the SINGLE opaque TokenVerificationError (fail-closed,
no oracle). Covers the SPEC §5 NE-060 accept/reject taxonomy.
"""

from __future__ import annotations

import base64
import json
import time
from datetime import UTC, datetime
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from ne.contracts.identity import TokenClaims
from ne.identity.config import OidcConfig
from ne.identity.errors import TokenVerificationError
from ne.identity.jwks import StaticJwksSource
from ne.identity.verifier import OidcTokenVerifier, _verified_expiry

from .conftest import (
    DEFAULT_KID,
    TEST_AUDIENCE,
    TEST_ISSUER,
    run,
)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def test_valid_token_accepted(verifier: OidcTokenVerifier, mint: Any) -> None:
    now = int(time.time())
    claims = run(verifier.verify(mint({"iat": now, "exp": now + 300})))
    assert isinstance(claims, TokenClaims)
    assert claims.subject == "user-1"
    assert claims.issuer == TEST_ISSUER
    assert claims.claims["aud"] == TEST_AUDIENCE
    assert claims.verified_expires_at == datetime.fromtimestamp(now + 300, UTC)


@pytest.mark.parametrize(
    "raw",
    [
        True,
        False,
        "1800000000",
        None,
        1.25,
        float("inf"),
        float("-inf"),
        float("nan"),
        -1,
        253_402_300_800,
    ],
)
def test_verified_expiry_rejects_non_integer_or_out_of_range_numeric_date(raw: object) -> None:
    with pytest.raises(ValueError):
        _verified_expiry(raw)


def test_verified_expiry_accepts_integral_numeric_date_and_normalizes_utc() -> None:
    expected = datetime(2027, 1, 15, 8, 0, tzinfo=UTC)
    assert _verified_expiry(1_800_000_000) == expected
    assert _verified_expiry(1_800_000_000.0) == expected


def test_expired_token_rejected(verifier: OidcTokenVerifier, mint: Any) -> None:
    now = int(time.time())
    with pytest.raises(TokenVerificationError):
        run(verifier.verify(mint({"exp": now - 3600, "iat": now - 3700})))


def test_nbf_future_rejected(verifier: OidcTokenVerifier, mint: Any) -> None:
    now = int(time.time())
    with pytest.raises(TokenVerificationError):
        run(verifier.verify(mint({"nbf": now + 3600})))


def test_wrong_audience_rejected(verifier: OidcTokenVerifier, mint: Any) -> None:
    with pytest.raises(TokenVerificationError):
        run(verifier.verify(mint({"aud": "other-api"})))


def test_missing_audience_rejected(verifier: OidcTokenVerifier, mint: Any, DROP: Any) -> None:
    with pytest.raises(TokenVerificationError):
        run(verifier.verify(mint({"aud": DROP})))


def test_missing_sub_rejected(verifier: OidcTokenVerifier, mint: Any, DROP: Any) -> None:
    with pytest.raises(TokenVerificationError):
        run(verifier.verify(mint({"sub": DROP})))


def test_missing_exp_rejected(verifier: OidcTokenVerifier, mint: Any, DROP: Any) -> None:
    with pytest.raises(TokenVerificationError):
        run(verifier.verify(mint({"exp": DROP})))


def test_missing_iat_rejected(verifier: OidcTokenVerifier, mint: Any, DROP: Any) -> None:
    with pytest.raises(TokenVerificationError):
        run(verifier.verify(mint({"iat": DROP})))


def test_wrong_issuer_rejected(verifier: OidcTokenVerifier, mint: Any) -> None:
    with pytest.raises(TokenVerificationError):
        run(verifier.verify(mint({"iss": "https://evil.example/realms/x"})))


def test_issuer_prefix_variant_rejected(verifier: OidcTokenVerifier, mint: Any) -> None:
    # Exact-match iss (CVE-2024-53861 class): a superstring issuer must fail.
    with pytest.raises(TokenVerificationError):
        run(verifier.verify(mint({"iss": TEST_ISSUER + "/evil"})))


def test_bad_signature_rejected(verifier: OidcTokenVerifier, mint: Any, attacker_key: Any) -> None:
    # Attacker signs but advertises the trusted kid — signature must not verify.
    token = mint(key=attacker_key, kid=DEFAULT_KID)
    with pytest.raises(TokenVerificationError):
        run(verifier.verify(token))


def test_alg_none_rejected(verifier: OidcTokenVerifier) -> None:
    header = {"alg": "none", "kid": DEFAULT_KID, "typ": "JWT"}
    now = int(time.time())
    payload = {
        "iss": TEST_ISSUER,
        "aud": TEST_AUDIENCE,
        "sub": "user-1",
        "iat": now,
        "exp": now + 300,
    }
    token = _b64url(json.dumps(header).encode()) + "." + _b64url(json.dumps(payload).encode()) + "."
    with pytest.raises(TokenVerificationError):
        run(verifier.verify(token))


def test_hs256_confusion_rejected(verifier: OidcTokenVerifier, signing_key_pem: bytes) -> None:
    # Classic RS/HS confusion: HMAC-sign with the RSA public key PEM as the
    # secret. Modern PyJWT refuses to MINT this via jwt.encode, so we craft the
    # token by hand to prove the VERIFIER rejects it (alg pinned to RS256).
    import hashlib
    import hmac

    now = int(time.time())
    header = {"alg": "HS256", "kid": DEFAULT_KID, "typ": "JWT"}
    payload = {
        "iss": TEST_ISSUER,
        "aud": TEST_AUDIENCE,
        "sub": "user-1",
        "iat": now,
        "exp": now + 300,
    }
    signing_input = (
        _b64url(json.dumps(header).encode()) + "." + _b64url(json.dumps(payload).encode())
    )
    sig = hmac.new(signing_key_pem, signing_input.encode(), hashlib.sha256).digest()
    token = signing_input + "." + _b64url(sig)
    with pytest.raises(TokenVerificationError):
        run(verifier.verify(token))


def test_hs256_never_configurable() -> None:
    with pytest.raises(ValueError):
        OidcConfig(issuer=TEST_ISSUER, audience=TEST_AUDIENCE, allowed_algs=("HS256",))


def test_alg_none_never_configurable() -> None:
    with pytest.raises(ValueError):
        OidcConfig(issuer=TEST_ISSUER, audience=TEST_AUDIENCE, allowed_algs=("none",))


def test_unknown_kid_rejected(verifier: OidcTokenVerifier, mint: Any) -> None:
    with pytest.raises(TokenVerificationError):
        run(verifier.verify(mint(kid="no-such-kid")))


def test_missing_kid_rejected(verifier: OidcTokenVerifier, mint: Any) -> None:
    with pytest.raises(TokenVerificationError):
        run(verifier.verify(mint(kid=None)))


def test_jwk_alg_mismatch_rejected(oidc_config: OidcConfig, signing_key: Any, mint: Any) -> None:
    # JWKS advertises the key as RS512; the token header says RS256 -> reject.
    jwk = json.loads(RSAAlgorithm.to_jwk(signing_key.public_key()))
    jwk.update({"kid": DEFAULT_KID, "use": "sig", "alg": "RS512"})
    source = StaticJwksSource({"keys": [jwk]})
    v = OidcTokenVerifier(oidc_config, key_source=source)
    with pytest.raises(TokenVerificationError):
        run(v.verify(mint()))


@pytest.mark.parametrize("bad", ["not.a.jwt", "", "aGVsbG8", "a.b.c"])
def test_garbage_token_rejected(verifier: OidcTokenVerifier, bad: str) -> None:
    with pytest.raises(TokenVerificationError):
        run(verifier.verify(bad))


def test_all_failures_raise_single_opaque_error(
    verifier: OidcTokenVerifier, mint: Any, attacker_key: Any
) -> None:
    now = int(time.time())
    cases = [
        mint({"exp": now - 3600, "iat": now - 3700}),
        mint({"aud": "other-api"}),
        mint({"iss": TEST_ISSUER + "/evil"}),
        mint(key=attacker_key, kid=DEFAULT_KID),
        mint(kid="no-such-kid"),
        "garbage",
    ]
    for token in cases:
        with pytest.raises(TokenVerificationError):
            run(verifier.verify(token))


# GHSA-8wjv-2p76-3863: nesting deep enough that json.loads raises RecursionError
# on every supported interpreter (measured: 2,000 levels parse fine, 100,000 do not).
_NESTED_HEADER_DEPTH = 100_000


def _deeply_nested_header_token(opener: bytes, closer: bytes) -> str:
    """An unsigned bearer token whose otherwise-plausible header nests one member deeply."""
    header = (
        b'{"alg":"RS256","kid":"'
        + DEFAULT_KID.encode("ascii")
        + b'","typ":"JWT","x":'
        + opener * _NESTED_HEADER_DEPTH
        + b"1"
        + closer * _NESTED_HEADER_DEPTH
        + b"}"
    )
    return _b64url(header) + ".e30.AAAA"


@pytest.mark.parametrize(
    ("opener", "closer"),
    [(b"[", b"]"), (b'{"a":', b"}")],
    ids=["nested-arrays", "nested-objects"],
)
def test_deeply_nested_header_rejected_with_opaque_error_not_recursion_error(
    verifier: OidcTokenVerifier, opener: bytes, closer: bytes
) -> None:
    # GHSA-8wjv-2p76-3863 (fixed in PyJWT 2.14.0): on 2.13.0 this header makes
    # jwt.get_unverified_header raise RecursionError, which is not in verify()'s
    # except tuple, so an unauthenticated caller got an unhandled exception
    # instead of the fail-closed rejection. pytest.raises lets any other
    # exception type propagate, so an escaping RecursionError fails this test.
    with pytest.raises(TokenVerificationError) as exc:
        run(verifier.verify(_deeply_nested_header_token(opener, closer)))
    # Precondition guard (non-vacuity): the rejection must be PyJWT's own DecodeError
    # wrap of the recursion failure, not a later branch that happened to reject.
    # Exact type: InvalidSignatureError subclasses DecodeError and must not satisfy this guard.
    # And it must wrap the RecursionError itself (PyJWT 2.15.0 raises DecodeError(...) from
    # the RecursionError, so __cause__ holds it): a fixture that is merely invalid JSON yields
    # a DecodeError wrapping JSONDecodeError and must not satisfy this guard.
    cause = exc.value.__cause__
    assert type(cause) is jwt.DecodeError
    assert isinstance(cause.__cause__, RecursionError)


def _signed_raw_payload_token(signing_key: rsa.RSAPrivateKey, raw_payload: bytes) -> str:
    """A well-formed header + ``raw_payload`` bytes VERBATIM, signed by the TRUSTED IdP key.

    Bypasses ``jwt.encode``'s ``json.dumps`` so the payload can be something a dict
    cannot round-trip (a 100,000-deep nesting, a non-finite JSON number).
    """
    return jwt.PyJWS().encode(
        raw_payload, signing_key, algorithm="RS256", headers={"kid": DEFAULT_KID}
    )


@pytest.mark.parametrize(
    ("opener", "closer"),
    [(b"[", b"]"), (b'{"a":', b"}")],
    ids=["nested-arrays", "nested-objects"],
)
def test_signed_deeply_nested_payload_rejected_with_opaque_error_not_recursion_error(
    verifier: OidcTokenVerifier, signing_key: rsa.RSAPrivateKey, opener: bytes, closer: bytes
) -> None:
    # GHSA-42vr-xj54-vc7v (fixed in PyJWT 2.15.0): with verify_signature=True the
    # payload is json.loads-ed only AFTER the signature verifies, so this token is
    # signed by the trusted IdP key (an authenticated DoS, not unauthenticated). On
    # 2.14.0 json.loads raised RecursionError inside jwt.decode, which is not in
    # verify()'s except tuple, so it escapes pytest.raises and fails this test.
    payload = opener * _NESTED_HEADER_DEPTH + b"1" + closer * _NESTED_HEADER_DEPTH
    with pytest.raises(TokenVerificationError) as exc:
        run(verifier.verify(_signed_raw_payload_token(signing_key, payload)))
    # Exact type: InvalidSignatureError subclasses DecodeError and must not satisfy this guard.
    # And it must wrap the RecursionError itself (PyJWT 2.15.0 raises DecodeError(...) from
    # the RecursionError, so __cause__ holds it): a fixture that is merely invalid JSON yields
    # a DecodeError wrapping JSONDecodeError and must not satisfy this guard.
    cause = exc.value.__cause__
    assert type(cause) is jwt.DecodeError
    assert isinstance(cause.__cause__, RecursionError)


def test_signed_non_finite_exp_rejected_with_opaque_error_not_overflow_error(
    verifier: OidcTokenVerifier, signing_key: rsa.RSAPrivateKey
) -> None:
    # PyJWT 2.15.0 also wraps int(payload["exp"]) OverflowError in DecodeError. On
    # 2.14.0 a signed token whose exp is the JSON number 1e400 (json.loads -> inf)
    # made jwt.decode raise OverflowError, which is not in the except tuple either.
    now = int(time.time())
    claims = json.dumps(
        {"iss": TEST_ISSUER, "aud": TEST_AUDIENCE, "sub": "user-1", "iat": now, "typ": "Bearer"}
    )
    payload = (claims[:-1] + ', "exp": 1e400}').encode("ascii")
    with pytest.raises(TokenVerificationError) as exc:
        run(verifier.verify(_signed_raw_payload_token(signing_key, payload)))
    # Exact type: InvalidSignatureError subclasses DecodeError and must not satisfy this guard.
    # And it must wrap the OverflowError itself (PyJWT 2.15.0 raises DecodeError(...) from
    # None inside the except block, so __cause__ is None and __context__ holds it): a
    # fixture that is merely invalid JSON must not satisfy this guard.
    cause = exc.value.__cause__
    assert type(cause) is jwt.DecodeError
    assert isinstance(cause.__context__, OverflowError)
