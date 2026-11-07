# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""THE fail-closed Bot Framework activity-JWT matrix (NE-080).

Every forged/unsigned/malformed activity is rejected with the SINGLE opaque
ActivityAuthError; a fully valid token (and a trailing-slash-normalized
serviceUrl) passes. This is the SPEC-mandated forged/unsigned-activity rejection.
"""

from __future__ import annotations

import asyncio
import base64
import json

import jwt
import pytest

from ne.chat.errors import ActivityAuthError
from ne.chat.teams.auth import StaticJwksKeySource, TeamsActivityAuthenticator

from .teams_support import (
    JWKS,
    KID,
    SERVICE_URL,
    activity_claims,
    mint_activity_token,
    mint_alg_none_token,
    mint_hs256_confusion_token,
    mint_raw_payload_activity_token,
    teams_chat_settings,
)

_OPAQUE = "activity authorization failed"  # the single opaque message every branch shares


def _auth() -> TeamsActivityAuthenticator:
    return TeamsActivityAuthenticator(teams_chat_settings(), StaticJwksKeySource(JWKS))


async def _authenticate(header: str | None, service_url: str = SERVICE_URL) -> None:
    await _auth().authenticate(header, service_url)


def _rejects(header: str | None, service_url: str = SERVICE_URL) -> ActivityAuthError:
    with pytest.raises(ActivityAuthError) as exc:
        asyncio.run(_authenticate(header, service_url))
    return exc.value


def test_valid_token_passes() -> None:
    header = f"Bearer {mint_activity_token()}"
    asyncio.run(_authenticate(header))  # no raise


def test_trailing_slash_serviceurl_normalized_passes() -> None:
    # Token claim has NO trailing slash; the activity serviceUrl HAS one — both normalize.
    header = f"Bearer {mint_activity_token(serviceurl='https://smba.trafficmanager.net/amer')}"
    asyncio.run(_authenticate(header, "https://smba.trafficmanager.net/amer/"))


def test_missing_header_rejected() -> None:
    assert _OPAQUE in str(_rejects(None))


def test_non_bearer_header_rejected() -> None:
    _rejects("Basic abc123")


def test_garbage_token_rejected() -> None:
    _rejects("Bearer not-a-jwt")


def test_alg_none_rejected() -> None:
    _rejects(f"Bearer {mint_alg_none_token()}")


def test_hs256_confusion_rejected() -> None:
    _rejects(f"Bearer {mint_hs256_confusion_token()}")


def test_unknown_kid_rejected() -> None:
    _rejects(f"Bearer {mint_activity_token(kid='rotated-away')}")


def test_wrong_audience_rejected() -> None:
    _rejects(f"Bearer {mint_activity_token(aud='some-other-bot')}")


def test_wrong_issuer_rejected() -> None:
    _rejects(f"Bearer {mint_activity_token(iss='https://evil.example.com')}")


def test_expired_token_rejected() -> None:
    _rejects(f"Bearer {mint_activity_token(exp_delta=-10_000)}")


def test_future_nbf_beyond_leeway_rejected() -> None:
    header = f"Bearer {mint_activity_token(nbf=2_000_000_000, exp=2_000_003_600)}"
    _rejects(header)


def test_serviceurl_claim_mismatch_rejected() -> None:
    header = f"Bearer {mint_activity_token(serviceurl='https://smba.trafficmanager.net/emea')}"
    _rejects(header, "https://smba.trafficmanager.net/amer")


def test_missing_serviceurl_claim_rejected() -> None:
    # Override serviceurl to an empty string (falsy) — the claim is treated as absent.
    header = f"Bearer {mint_activity_token(serviceurl='')}"
    _rejects(header)


def test_all_rejections_share_one_opaque_error() -> None:
    messages = {
        str(_rejects(h))
        for h in (
            None,
            "Basic x",
            "Bearer garbage",
            f"Bearer {mint_alg_none_token()}",
            f"Bearer {mint_activity_token(aud='other')}",
        )
    }
    # Each carries the same opaque token — none leaks a distinguishing reason
    # beyond the failure category the caller already knows.
    for message in messages:
        assert _OPAQUE in message


# GHSA-8wjv-2p76-3863: nesting deep enough that json.loads raises RecursionError
# on every supported interpreter (measured: 2,000 levels parse fine, 100,000 do not).
_NESTED_HEADER_DEPTH = 100_000


def _deeply_nested_header_token(opener: bytes, closer: bytes) -> str:
    """An unsigned activity token whose otherwise-plausible header nests one member deeply."""
    header = (
        b'{"alg":"RS256","typ":"JWT","kid":"'
        + KID.encode("ascii")
        + b'","x":'
        + opener * _NESTED_HEADER_DEPTH
        + b"1"
        + closer * _NESTED_HEADER_DEPTH
        + b"}"
    )
    return base64.urlsafe_b64encode(header).rstrip(b"=").decode("ascii") + ".e30.AAAA"


@pytest.mark.parametrize(
    ("opener", "closer"),
    [(b"[", b"]"), (b'{"a":', b"}")],
    ids=["nested-arrays", "nested-objects"],
)
def test_deeply_nested_header_rejected_with_opaque_error_not_recursion_error(
    opener: bytes, closer: bytes
) -> None:
    # GHSA-8wjv-2p76-3863 (fixed in PyJWT 2.14.0): on 2.13.0 this header makes
    # jwt.get_unverified_header raise RecursionError, which is not in
    # authenticate()'s except tuple, so an unauthenticated caller got an unhandled
    # exception instead of the opaque rejection. pytest.raises (inside _rejects)
    # lets any other exception type propagate, so an escaping RecursionError fails
    # this test.
    error = _rejects(f"Bearer {_deeply_nested_header_token(opener, closer)}")
    assert str(error) == _OPAQUE
    # Precondition guard (non-vacuity): the rejection must be PyJWT's own DecodeError
    # wrap of the recursion failure, not a later branch that happened to reject.
    # Exact type: InvalidSignatureError subclasses DecodeError and must not satisfy this guard.
    # And it must wrap the RecursionError itself (PyJWT 2.15.0 raises DecodeError(...) from
    # the RecursionError, so __cause__ holds it): a fixture that is merely invalid JSON yields
    # a DecodeError wrapping JSONDecodeError and must not satisfy this guard.
    cause = error.__cause__
    assert type(cause) is jwt.DecodeError
    assert isinstance(cause.__cause__, RecursionError)


@pytest.mark.parametrize(
    ("opener", "closer"),
    [(b"[", b"]"), (b'{"a":', b"}")],
    ids=["nested-arrays", "nested-objects"],
)
def test_signed_deeply_nested_payload_rejected_with_opaque_error_not_recursion_error(
    opener: bytes, closer: bytes
) -> None:
    # GHSA-42vr-xj54-vc7v (fixed in PyJWT 2.15.0): with verify_signature=True the
    # payload is json.loads-ed only AFTER the signature verifies, so this token is
    # signed by the trusted key (an authenticated DoS, not unauthenticated). On
    # 2.14.0 json.loads raised RecursionError inside jwt.decode, which is not in
    # authenticate()'s except tuple, so it escapes pytest.raises and fails this test.
    payload = opener * _NESTED_HEADER_DEPTH + b"1" + closer * _NESTED_HEADER_DEPTH
    error = _rejects(f"Bearer {mint_raw_payload_activity_token(payload)}")
    assert str(error) == _OPAQUE
    # Exact type: InvalidSignatureError subclasses DecodeError and must not satisfy this guard.
    # And it must wrap the RecursionError itself (PyJWT 2.15.0 raises DecodeError(...) from
    # the RecursionError, so __cause__ holds it): a fixture that is merely invalid JSON yields
    # a DecodeError wrapping JSONDecodeError and must not satisfy this guard.
    cause = error.__cause__
    assert type(cause) is jwt.DecodeError
    assert isinstance(cause.__cause__, RecursionError)


def test_signed_non_finite_exp_rejected_with_opaque_error_not_overflow_error() -> None:
    # PyJWT 2.15.0 also wraps int(payload["exp"]) OverflowError in DecodeError. On
    # 2.14.0 a signed token whose exp is the JSON number 1e400 (json.loads -> inf)
    # made jwt.decode raise OverflowError, which is not in the except tuple either.
    claims = activity_claims()
    del claims["exp"]
    payload = (json.dumps(claims)[:-1] + ', "exp": 1e400}').encode("ascii")
    error = _rejects(f"Bearer {mint_raw_payload_activity_token(payload)}")
    assert str(error) == _OPAQUE
    # Exact type: InvalidSignatureError subclasses DecodeError and must not satisfy this guard.
    # And it must wrap the OverflowError itself (PyJWT 2.15.0 raises DecodeError(...) from
    # None inside the except block, so __cause__ is None and __context__ holds it): a
    # fixture that is merely invalid JSON must not satisfy this guard.
    cause = error.__cause__
    assert type(cause) is jwt.DecodeError
    assert isinstance(cause.__context__, OverflowError)
