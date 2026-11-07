# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Error hierarchy for ne-audit.

Note the deliberate asymmetry: *appending* to a broken/misused chain raises, but
*verification* NEVER raises on a broken chain — :func:`ne.audit.verify.verify_chain`
returns a :class:`~ne.audit.verify.ChainVerification` naming the exact break so the
caller (and the ``ne audit verify`` CLI) can report it. Raising on a tampered chain
would let an attacker turn tamper-detection into a denial-of-service.
"""

from __future__ import annotations


class AuditError(Exception):
    """Base class for ne-audit faults."""


class AppendOnlyViolationError(AuditError):
    """Raised when the append seam is misused (e.g. a non-event payload)."""


class CheckpointSignatureError(AuditError):
    """Raised when a checkpoint's signature cannot be produced or is required and invalid."""
