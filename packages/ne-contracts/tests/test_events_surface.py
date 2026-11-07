# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Event surfaces are structural: a non-inheriting object satisfies EventLike.

Proves NE-005's LinkML-generated event classes will conform without importing
anything from ne-contracts.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ne.contracts import AuditEventLike, EventLike


class PlainEvent:
    """A plain object with the six EventLike attributes — no inheritance."""

    def __init__(self) -> None:
        self.event_type = "audit.tool_call"
        self.config_hash = "abc123"
        self.kpi_tag = "latency"
        self.actor = "agent://demo"
        self.provenance = {"source": "runtime"}
        self.occurred_at = datetime.now(UTC)


class MissingConfigHash:
    def __init__(self) -> None:
        self.event_type = "audit.tool_call"
        self.kpi_tag = None
        self.actor = "agent://demo"
        self.provenance = {}
        self.occurred_at = datetime.now(UTC)


def test_plain_object_satisfies_event_like() -> None:
    assert isinstance(PlainEvent(), EventLike)


def test_plain_object_satisfies_named_subsurface() -> None:
    assert isinstance(PlainEvent(), AuditEventLike)


def test_object_missing_config_hash_is_not_event_like() -> None:
    assert not isinstance(MissingConfigHash(), EventLike)
    assert not isinstance(MissingConfigHash(), AuditEventLike)
