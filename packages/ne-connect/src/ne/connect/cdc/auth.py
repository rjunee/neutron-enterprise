# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Standard-Webhooks HMAC verification for the CDC sink (NE-084) — pure stdlib.

Debezium Server's HTTP sink authenticates every POST with the Standard Webhooks
scheme (``debezium.sink.http.authentication.type=standard-webhooks``, verified
in the debezium-server source ``StandardWebhooksAuthenticator.java``). This
module re-implements the VERIFIER so the ne-api sink endpoint can reject any
unauthenticated / replayed / tampered change-event POST fail-closed — the NE-089
SCIM bearer-gate precedent: every failure returns the SAME negative result with
no distinguishing detail, and the comparison is constant-time.

Scheme (Standard Webhooks):

* secret string = ``whsec_`` + base64(24..64 raw bytes) — the prefix is optional
  on decode; the raw length bound mirrors Debezium's own check.
* signed content = ``f"{webhook-id}.{webhook-timestamp}.{body}"``.
* signature header = one or more space-separated ``v1,<base64(HMAC-SHA256)>``
  tokens; a POST is authentic iff ANY token matches (key rotation overlap).
* ``webhook-timestamp`` (epoch seconds) must be within ``tolerance_s`` of now
  (replay window).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import time

__all__ = ["decode_webhook_secret", "verify_standard_webhook"]

_SECRET_PREFIX = "whsec_"
_MIN_SECRET_BYTES = 24
_MAX_SECRET_BYTES = 64
_DEFAULT_TOLERANCE_S = 300


def decode_webhook_secret(secret: str) -> bytes:
    """Decode a ``whsec_``-prefixed base64 webhook secret to its raw key bytes.

    The ``whsec_`` prefix is optional. Raises :class:`ValueError` if the value
    is not valid base64 or the decoded length is outside ``24..64`` bytes
    (Debezium's own bound). The error NEVER echoes the secret material.
    """
    body = secret[len(_SECRET_PREFIX) :] if secret.startswith(_SECRET_PREFIX) else secret
    try:
        raw = base64.b64decode(body, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("webhook secret is not valid base64") from exc
    if not (_MIN_SECRET_BYTES <= len(raw) <= _MAX_SECRET_BYTES):
        raise ValueError(
            f"webhook secret must decode to {_MIN_SECRET_BYTES}..{_MAX_SECRET_BYTES} bytes"
        )
    return raw


def _expected_signature(secret: bytes, msg_id: str, timestamp: str, body: bytes) -> str:
    signed = f"{msg_id}.{timestamp}.".encode() + body
    digest = hmac.new(secret, signed, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode("ascii")


def verify_standard_webhook(
    *,
    secret: bytes,
    msg_id: str | None,
    timestamp: str | None,
    signature_header: str | None,
    body: bytes,
    now: int | None = None,
    tolerance_s: int = _DEFAULT_TOLERANCE_S,
) -> bool:
    """Return ``True`` iff the POST is an authentic, in-window Standard-Webhooks
    signature. Every failure path returns ``False`` (no distinguishing detail —
    the caller 401s uniformly). The comparison is constant-time.
    """
    if not msg_id or not timestamp or not signature_header:
        return False
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    reference = int(time.time()) if now is None else now
    if abs(reference - ts) > tolerance_s:
        return False
    expected = _expected_signature(secret, msg_id, timestamp, body)
    for candidate in signature_header.split(" "):
        if candidate and hmac.compare_digest(candidate, expected):
            return True
    return False
