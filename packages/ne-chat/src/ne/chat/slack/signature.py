# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Slack v0 request-signature verification — pure, deterministic, stdlib-only.

Verifies the ``X-Slack-Signature`` / ``X-Slack-Request-Timestamp`` pair Slack
attaches to every Events API and interactivity request, per
https://docs.slack.dev/authentication/verifying-requests-from-slack.

This runs FIRST on the raw request bytes, BEFORE any JSON parsing or agent /
model-adjacent processing (C12: deterministic code before judgment). It rejects
unsigned, forged, and stale (replayed) requests. Anti-replay rejects BOTH past
and future timestamp skew.

C12 secret hygiene: every failure raises with a CLASS-level constant message that
names ONLY the failure category — never the signing secret, the computed digest,
the provided signature, or the request body.
"""

from __future__ import annotations

import hashlib
import hmac
import math

from ne.chat.errors import SignatureVerificationError, StaleTimestampError

__all__ = ["verify_slack_signature"]

_MISSING_HEADERS = "missing signature headers"
_BAD_TIMESTAMP = "malformed request timestamp"
_STALE = "request timestamp outside the anti-replay tolerance window"
_BAD_VERSION = "unsupported signature version"
_MISMATCH = "request signature does not match"

_VERSION = "v0"


def verify_slack_signature(
    *,
    raw_body: bytes,
    timestamp_header: str | None,
    signature_header: str | None,
    signing_secret: str,
    now: float,
    tolerance_s: float,
) -> None:
    """Verify a Slack v0 request signature, or raise (status-only, C12).

    Order (fail closed at the first failing step):

    1. either header missing/empty ⇒ :class:`SignatureVerificationError`;
    2. timestamp not parseable as a *finite* number (rejects ``nan``/``inf``) ⇒
       :class:`SignatureVerificationError`;
    3. ``abs(now - ts) > tolerance_s`` (past OR future) ⇒
       :class:`StaleTimestampError` (anti-replay);
    4. signature not prefixed ``v0=`` ⇒ :class:`SignatureVerificationError`;
    5. constant-time HMAC comparison fails ⇒ :class:`SignatureVerificationError`.
    """
    if not timestamp_header or not signature_header:
        raise SignatureVerificationError(_MISSING_HEADERS)

    try:
        ts = float(timestamp_header)
    except (TypeError, ValueError):
        raise SignatureVerificationError(_BAD_TIMESTAMP) from None

    # Reject non-finite timestamps (``nan``/``inf``): ``abs(now - nan) > tol`` is
    # False, so a NaN would otherwise slip past the anti-replay window and blow up
    # at ``int(ts)`` below as an uncaught ValueError (surfacing as a 500 instead of
    # a clean status-only rejection). Fail closed here.
    if not math.isfinite(ts):
        raise SignatureVerificationError(_BAD_TIMESTAMP)

    if abs(now - ts) > tolerance_s:
        raise StaleTimestampError(_STALE)

    if not signature_header.startswith(f"{_VERSION}="):
        raise SignatureVerificationError(_BAD_VERSION)

    basestring = f"{_VERSION}:{int(ts)}:".encode() + raw_body
    digest = hmac.new(signing_secret.encode(), basestring, hashlib.sha256).hexdigest()
    expected = f"{_VERSION}={digest}"
    if not hmac.compare_digest(expected, signature_header):
        raise SignatureVerificationError(_MISMATCH)
