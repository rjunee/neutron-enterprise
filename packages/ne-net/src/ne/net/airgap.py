# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The single consolidated AIR_GAP egress guard (NE-CKC10 / P2-23).

This module is the ONE home of the ``AIR_GAP`` environment guard. Before
NE-CKC10 the same 5-line read (``os.environ.get("AIR_GAP", "").strip().lower()``)
and the ``AirGapViolationError`` type were hand-copied into ``ne.brain.sovereignty``,
``ne.permissions.sovereignty``, ``ne.permissions_rebac.sovereignty``,
``ne.gateway.egress``, and both ne-chat clients (slack/teams) — a security-code
drift hazard. Those copies are now thin ``from ne.net.airgap import ...``
re-exports/aliases.

``AIR_GAP`` semantics are FAIL-CLOSED: an unset / empty / whitespace-only value is
``False`` (no restriction), and the flag can only ever ADD restriction, never
remove it. The truthy token set is byte-identical to every former copy.

``ne.gateway.errors.AirGapViolationError`` is DELIBERATELY NOT this class: it is a
distinct ``EgressBlockedError`` subclass so the gateway's egress-policy hierarchy
stays intact. Do not merge the two.

Pure stdlib (``os`` only) — keeps ``ne-net`` a leaf (the ``ne-net-leaf``
import-linter contract forbids any first-party / provider import here).
"""

from __future__ import annotations

import os

__all__ = ["AirGapViolationError", "air_gap_engaged"]

_AIR_GAP_TRUE = {"1", "true", "yes", "on"}
"""Truthy ``AIR_GAP`` tokens — the single platform-wide definition."""


class AirGapViolationError(RuntimeError):
    """An operation would egress from an AIR_GAP-engaged (air-gapped) deployment."""


def air_gap_engaged() -> bool:
    """True when ``AIR_GAP`` is engaged.

    Fail-closed: an unset / empty / whitespace-only value is ``False`` (no
    restriction); AIR_GAP can only ever ADD restriction, never remove it.
    """
    return os.environ.get("AIR_GAP", "").strip().lower() in _AIR_GAP_TRUE
