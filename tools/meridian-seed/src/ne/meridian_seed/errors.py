# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Meridian seed failure types."""


class SeedError(RuntimeError):
    """Base error for deterministic slice or seed failures."""


class SeedPreflightError(SeedError):
    """Tenant prerequisites are incomplete; no write should have occurred."""


class SeedSafetyError(SeedError):
    """A destructive target failed the Meridian ownership boundary."""
