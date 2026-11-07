# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""C12-style secret hygiene: deterministic credential-shape redaction.

This is a DELIBERATE LOCAL COPY of the pattern set in
``packages/ne-flywheel/src/ne/flywheel/hygiene.py``, not an import
of ``ne.flywheel`` — this package must stay dependency-free of SIBLING
PLATFORM PACKAGES (no ``ne.flywheel``, no ``ne.contracts``; U2 adds
``pyyaml`` — a serialization library, not a platform package — and a
subprocess-only dependency on ``ne-schema``'s codegen driver, see
:mod:`ne.schema_bootstrap.roundtrip`) so it can run standalone against a
delivery laptop's local sample directory with a small, auditable install
surface. If the two hygiene copies drift, that is a defect to fix by
re-syncing this copy, not a reason to import across packages.

Pure, stdlib-``re``-only, no I/O, no LLM involvement — pattern match and
truncate only, so exemplar retention (§3.3) never leaks a credential-shaped
value into a persisted/reviewed profile artifact.
"""

from __future__ import annotations

import re

_PATTERNS: tuple[re.Pattern[str], ...] = (
    # AWS access key ids.
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    # GitHub tokens (ghp_/gho_/ghu_/ghs_/ghr_...).
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    # Slack tokens (xoxb-/xoxa-/xoxp-/xoxr-/xoxs-...).
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    # Bearer auth headers.
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    # JWTs (three dot-separated base64url segments).
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}\b"),
    # PEM private-key blocks, including a dangling BEGIN line with no END.
    re.compile(r"-----BEGIN[A-Z ]*PRIVATE KEY-----[\s\S]*?-----END[A-Z ]*PRIVATE KEY-----"),
    re.compile(r"-----BEGIN[A-Z ]*PRIVATE KEY-----[\s\S]*"),
    # key=value / key: value credential assignments.
    re.compile(r"(?i)\b(password|passwd|secret|api[_-]?key|token|authorization)\s*[=:]\s*\S+"),
)

_REDACTED = "[REDACTED]"


def scrub_text(text: str, *, max_chars: int = 4000) -> str:
    """Redact credential-shaped substrings in ``text``, then truncate.

    Every pattern in :data:`_PATTERNS` is applied in order, each match
    replaced with ``"[REDACTED]"``. Truncation to ``max_chars`` happens AFTER
    redaction, so a redaction can never be cut off mid-token by a premature
    truncate. Pure function; no I/O.
    """
    scrubbed = text
    for pattern in _PATTERNS:
        scrubbed = pattern.sub(_REDACTED, scrubbed)
    if len(scrubbed) > max_chars:
        scrubbed = scrubbed[:max_chars]
    return scrubbed
