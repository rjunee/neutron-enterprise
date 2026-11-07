# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ne-permissions error hierarchy.

``PrincipalSyncError`` deliberately carries only the source name and a redacted
attempt detail — NEVER a token, client secret, or full URL (C12 secrets hygiene;
credential-leak rule mirrored from ``ne.brain.sovereignty``).
"""

from __future__ import annotations

from ne.permissions.sovereignty import AirGapViolationError, UnsafeEndpointError

__all__ = [
    "AirGapViolationError",
    "PermissionsError",
    "PrincipalSyncError",
    "UnsafeEndpointError",
]


class PermissionsError(Exception):
    """Base class for every ne-permissions error."""


class PrincipalSyncError(PermissionsError):
    """A principal sync failed against its source.

    Carries the ``source`` name and a redacted ``detail`` string only; a token,
    client secret, or full request URL is NEVER interpolated (C12).
    """

    def __init__(self, source: str, detail: str) -> None:
        self.source = source
        self.detail = detail
        super().__init__(f"principal sync failed for source {source!r}: {detail}")
