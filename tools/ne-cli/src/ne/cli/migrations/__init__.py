# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The CENTRAL cross-module migration registry (NE-069).

The registry lives in ``tools/ne-cli`` (not in any ``packages/ne-*`` source
tree) — release/upgrade tooling is a platform-level concern, and centralizing
it here means a cross-module migration never has to reach into a package's
internals. Every real migration module (``mNNNN_<name>.py``) defines a single
module-level ``MIGRATION: Migration``; this module assembles them, in version
order, into :data:`REGISTRY`, and validates the reversible-migration policy +
ordering ONCE, at import time, so a malformed registry fails as early as
possible.
"""

from __future__ import annotations

from ne.cli.migrate import Migration, validate_registry
from ne.cli.migrations import m0001_baseline

REGISTRY: tuple[Migration, ...] = validate_registry((m0001_baseline.MIGRATION,))

__all__ = ["REGISTRY"]
