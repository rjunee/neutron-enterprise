# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-REDTEAM — forged-token battery against the OIDC/token-trust boundary.

Real RSA signatures, no Keycloak, no network (reuses the ne-identity ``conftest``
``mint``/``verifier``/``signing_key`` seams). The classic forgeries — ``alg:none``,
HS256/RS256 confusion, wrong ``iss``/``aud``, expired, missing claims, unknown
``kid``, attacker-key-with-trusted-kid (kid-spoof), garbage — are ALREADY covered
in ``test_verifier_security.py`` and are mapped there in the red-team report; this
file adds only GENUINELY-NEW forgeries.

HONESTY NOTE (also in the report): a stateless JWT verifier CANNOT reject replay of
a still-VALID unexpired token — that is by design; replay is mitigated by short
``exp`` + TLS transport, not by the verifier. We therefore pin the realistic
CAPTURED-STALE-token rejection and explicitly document (and assert) that a valid
token verifies repeatedly. We do NOT write a false "verifier rejects all replays"
test.

Each test states its MUTATION-CHECK proving non-vacuity.
"""

from __future__ import annotations

import base64
import json
import time
from typing import Any

import pytest

from ne.identity.errors import TokenVerificationError
from ne.identity.verifier import OidcTokenVerifier

from .conftest import run


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


# --- (a) tampered payload, ORIGINAL signature kept ----------------------------


def test_tampered_payload_keeps_original_signature_rejected(
    verifier: OidcTokenVerifier, mint: Any
) -> None:
    """Take a VALID token, escalate a claim in its payload segment (``sub`` ->
    attacker), and reassemble it with the ORIGINAL RSA signature. The signature no
    longer covers the mutated payload, so verification fails with the single opaque
    error.

    MUTATION-CHECK: if the verifier skipped RSA signature verification (or verified
    the signature against the OLD/pre-tamper payload), the escalated ``sub`` would
    be accepted.
    """
    header_b64, payload_b64, sig_b64 = mint().split(".")
    payload = json.loads(_b64url_decode(payload_b64))
    payload["sub"] = "user-attacker-escalated"
    tampered = header_b64 + "." + _b64url(json.dumps(payload).encode()) + "." + sig_b64

    with pytest.raises(TokenVerificationError):
        run(verifier.verify(tampered))


# --- (b) RS256 header but stripped (empty) signature --------------------------


def test_stripped_signature_rs256_rejected(verifier: OidcTokenVerifier, mint: Any) -> None:
    """A token whose header still declares RS256 but whose signature segment is
    EMPTY (a "just drop the signature" forgery, distinct from an ``alg:none``
    header) is rejected.

    MUTATION-CHECK: a verifier that treated an empty signature as "nothing to
    check" (verify_signature effectively off) would accept the unsigned token.
    """
    header_b64, payload_b64, _sig = mint().split(".")
    stripped = header_b64 + "." + payload_b64 + "."

    with pytest.raises(TokenVerificationError):
        run(verifier.verify(stripped))


# --- (c) captured-stale replay rejected; valid token replays by design --------


def test_stale_captured_token_rejected_but_valid_replays_by_design(
    verifier: OidcTokenVerifier, mint: Any
) -> None:
    """A captured, now-EXPIRED token (the realistic replay-after-a-while attack) is
    rejected by the ``exp`` check. In the SAME test we honestly document the
    stateless-verifier limitation: a still-valid token verifies repeatedly — replay
    of an unexpired token is NOT (and cannot be) rejected here; short ``exp`` + TLS
    mitigate it.

    MUTATION-CHECK: dropping the ``exp`` verification would accept the stale
    captured token. (No mutation makes the valid-token-replays assertion fail — it
    documents intended stateless behavior, not a defense.)
    """
    now = int(time.time())
    stale = mint({"exp": now - 3600, "iat": now - 7200})
    with pytest.raises(TokenVerificationError):
        run(verifier.verify(stale))

    # Stateless-by-design: a valid token verifies as many times as presented.
    valid = mint()
    first = run(verifier.verify(valid))
    second = run(verifier.verify(valid))
    assert first.subject == "user-1"
    assert second.subject == "user-1"


# --- (d) every red-team rejection is the SAME opaque error --------------------


def test_all_redteam_rejections_share_one_opaque_error(
    verifier: OidcTokenVerifier, mint: Any
) -> None:
    """The new forgeries all fail with the SAME opaque ``TokenVerificationError`` —
    no oracle leaks WHY a token was rejected (mirrors
    ``test_verifier_security.py::test_all_failures_raise_single_opaque_error``).

    MUTATION-CHECK: assert on the public error MESSAGE, not merely the exception
    type. A verifier that kept the ``TokenVerificationError`` class but embedded a
    reason-specific message per failure class (e.g. "bad signature" vs "expired")
    would recreate exactly the oracle this test claims to prevent — and a
    type-only assertion would stay green. Pinning the identical opaque string
    makes that regression fail.
    """
    now = int(time.time())

    header_b64, payload_b64, sig_b64 = mint().split(".")
    payload = json.loads(_b64url_decode(payload_b64))
    payload["sub"] = "attacker"
    tampered = header_b64 + "." + _b64url(json.dumps(payload).encode()) + "." + sig_b64

    hh, pp, _ = mint().split(".")
    stripped = hh + "." + pp + "."

    stale = mint({"exp": now - 3600, "iat": now - 7200})

    messages: set[str] = set()
    for token in (tampered, stripped, stale):
        with pytest.raises(TokenVerificationError) as excinfo:
            run(verifier.verify(token))
        messages.add(str(excinfo.value))

    # Every rejection class collapses to one identical opaque message — no reason
    # leaks through the string representation callers can observe.
    assert messages == {"token verification failed"}
