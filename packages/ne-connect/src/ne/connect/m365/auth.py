# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Certificate-signed client-assertion JWT for Graph app-only auth (NE-044).

Builds the RS256 ``client_assertion`` an Entra app-registration presents in the
client-credentials flow when a certificate (not a shared secret) is the
credential. The assertion is a JWT whose header carries the cert's SHA-1 (``x5t``)
and SHA-256 (``x5t#S256``) thumbprints and whose claims bind it to the token
endpoint (``aud``) and the app (``iss`` = ``sub`` = client_id), single-use via a
random ``jti`` and short ``exp``.

C12 discipline: key material only ever crosses this seam as ``bytes`` — it is
never logged, never placed in an exception message, and never returned. Only the
signed compact JWT (public by design) leaves.

ENCODE-ONLY: this module uses ``jwt.encode`` with a local private key. It never
calls ``PyJWKClient`` / the JWKS fetch-and-decode paths (the surface the 2026-05
and 2026-09 PyJWT advisory batches concern; the lock pins 2.15.0).
"""

from __future__ import annotations

import base64
import time
import uuid

import jwt
from cryptography.hazmat.primitives.hashes import SHA1, SHA256
from cryptography.x509 import load_pem_x509_certificate

__all__ = ["build_client_assertion"]


def _b64url_no_pad(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def build_client_assertion(
    client_id: str,
    token_url: str,
    certificate_pem: bytes,
    private_key_pem: bytes,
    *,
    now: float | None = None,
    lifetime_seconds: int = 600,
) -> str:
    """Return a signed RS256 client-assertion JWT for the client-credentials flow.

    ``certificate_pem`` / ``private_key_pem`` are raw PEM bytes (the cert supplies
    the ``x5t`` / ``x5t#S256`` header thumbprints; the private key signs). ``now``
    is injectable for deterministic tests. The returned compact JWT is safe to
    place in the token POST body; the inputs never are.
    """
    issued = int(now if now is not None else time.time())
    cert = load_pem_x509_certificate(certificate_pem)
    headers = {
        "x5t": _b64url_no_pad(cert.fingerprint(SHA1())),
        "x5t#S256": _b64url_no_pad(cert.fingerprint(SHA256())),
    }
    claims = {
        "aud": token_url,
        "iss": client_id,
        "sub": client_id,
        "jti": uuid.uuid4().hex,
        "iat": issued,
        "nbf": issued,
        "exp": issued + lifetime_seconds,
    }
    return jwt.encode(claims, private_key_pem, algorithm="RS256", headers=headers)
