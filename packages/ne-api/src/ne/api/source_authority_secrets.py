# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The continuation HMAC key loader for 015A0-AUTHORITY-CORE.

Two exact required-secret environment names and nothing else. The result is a
secret-bearing private value, deliberately NOT an ``ApiSettings`` field: settings
are serialized into the frozen product config hash, and key material must never
enter it.

Missing, short or malformed key material fails CONTINUATION readiness on this
replica and returns ``None``. It does not raise and it does not log: a boot-time
message naming which of the two variables was wrong, or how long the value was,
is a disclosure about deployment secrets in every log sink the deployment has.
Continuation is one surface; a root that cannot issue continuations must still
serve the rest of the deployment, so this is not a composition failure either.
"""

from __future__ import annotations

import base64
import binascii
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

__all__ = [
    "SOURCE_AUTHORITY_CONTINUATION_KEY_B64_ENV",
    "SOURCE_AUTHORITY_CONTINUATION_KEY_ID_ENV",
    "SourceAuthorityContinuationKey",
    "load_source_authority_continuation_key",
]

#: Supplied identically to API and worker from `pilot/source-authority-continuation`.
SOURCE_AUTHORITY_CONTINUATION_KEY_ID_ENV = "NE_SOURCE_AUTHORITY_CONTINUATION_HMAC_KEY_ID"
SOURCE_AUTHORITY_CONTINUATION_KEY_B64_ENV = "NE_SOURCE_AUTHORITY_CONTINUATION_HMAC_KEY_B64"

_KEY_ID_PATTERN = re.compile(r"\A[A-Za-z0-9_-]{1,128}\Z")
_MINIMUM_KEY_BYTES = 32


@dataclass(frozen=True)
class SourceAuthorityContinuationKey:
    """A validated continuation key. Its material never reaches a repr or a log."""

    key_id: str
    material: bytes = field(repr=False)

    def __repr__(self) -> str:  # pragma: no cover - exercised via the redaction test
        return f"SourceAuthorityContinuationKey(key_id={self.key_id!r}, material=<redacted>)"

    def __str__(self) -> str:
        return self.__repr__()


def load_source_authority_continuation_key(
    environ: Mapping[str, str] | None = None,
) -> SourceAuthorityContinuationKey | None:
    """Return the configured continuation key, or ``None`` when it is unusable.

    ``None`` is the single outcome for unconfigured, half-configured, malformed
    and short: the caller only ever learns that continuation is not ready on this
    replica, which is exactly what the operator can act on.
    """
    source = os.environ if environ is None else environ
    key_id = source.get(SOURCE_AUTHORITY_CONTINUATION_KEY_ID_ENV, "").strip()
    key_b64 = source.get(SOURCE_AUTHORITY_CONTINUATION_KEY_B64_ENV, "").strip()
    if not key_id or not key_b64:
        return None
    if not _KEY_ID_PATTERN.fullmatch(key_id):
        return None
    try:
        material = base64.urlsafe_b64decode(key_b64 + "=" * (-len(key_b64) % 4))
    except (binascii.Error, ValueError):
        return None
    if base64.urlsafe_b64encode(material).rstrip(b"=").decode("ascii") != key_b64:
        return None
    if len(material) < _MINIMUM_KEY_BYTES:
        return None
    return SourceAuthorityContinuationKey(key_id=key_id, material=material)
