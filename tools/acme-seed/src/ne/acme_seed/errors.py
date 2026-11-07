# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Tool-local errors for the Acme seeder (NE-102).

C12 discipline mirrors the connector errors: seeder failures carry only a
STATUS / a short code / an owner-action string — never a token, key material, a
response body, or a full URL. The connector clients this tool subclasses already
raise ``GraphSyncError`` / ``SalesforceSyncError`` / ``SlackSyncError`` with that
discipline; these two errors cover the seeder's own preflight/config faults.
"""

from __future__ import annotations

__all__ = ["SeedError", "SeedPreflightError"]


class SeedError(Exception):
    """Base for all seeder-local errors."""


class SeedPreflightError(SeedError):
    """A precondition for seeding is unmet, carrying the precise OWNER action.

    Raised BEFORE any tenant write: a missing external-id field on Salesforce, a
    missing M365 group / named-doc user in the principals map, or a missing
    principals file. The message is the exact add-list the owner must apply — the
    tool never auto-creates schema/objects it deems the owner's responsibility.
    """
