# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures for the ne-events outbox tests.

The outbox logic runs against a real in-memory SQLite engine (the SAME
SQLAlchemy statements, including SAVEPOINT) — nothing is mocked past the seam
(plan §1.4). ``NE_TEST_POSTGRES_URL`` re-runs the parkable subset on real
Postgres; see ``test_outbox_postgres.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
import sqlalchemy as sa

from ne.contracts import freeze_config_hash, reset_config_hash_for_testing
from ne.contracts.config import NeSettings
from ne.events import ensure_outbox_schema
from ne.schema import AuditEvent, AuditOutcome, CostEvent

# A valid 64-char lowercase-hex SHA-256 digest for direct construction (tests
# that do not exercise config-hash freezing use this explicit hash).
HEX_HASH = "a" * 64
_TS = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _utc(dt: datetime) -> datetime:
    """Normalize a possibly-naive datetime to UTC (SQLite drops tzinfo)."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


class _DemoSettings(NeSettings):
    demo: int = 7


@pytest.fixture
def utc() -> Callable[[datetime], datetime]:
    return _utc


@pytest.fixture
def hexhash() -> str:
    return HEX_HASH


@pytest.fixture
def engine() -> Iterator[sa.Engine]:
    eng = sa.create_engine("sqlite://")
    ensure_outbox_schema(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def frozen_config() -> Iterator[str]:
    """Freeze a deterministic config_hash for the duration of a test."""
    reset_config_hash_for_testing()
    digest = freeze_config_hash(_DemoSettings())
    try:
        yield digest
    finally:
        reset_config_hash_for_testing()


def make_audit(*, event_id: str | None = None, actor: str = "sys", **over: object) -> AuditEvent:
    fields: dict[str, Any] = {
        "event_id": event_id if event_id is not None else str(uuid.uuid4()),
        "event_type": "audit",
        "config_hash": HEX_HASH,
        "actor": actor,
        "provenance": {},
        "occurred_at": _TS,
        "action": "tool.invoke",
        "resource": "urn:tool:search",
        "outcome": AuditOutcome.success,
    }
    fields.update(over)
    return AuditEvent(**fields)


def make_cost(*, event_id: str | None = None, actor: str = "sys", **over: object) -> CostEvent:
    fields: dict[str, Any] = {
        "event_id": event_id if event_id is not None else str(uuid.uuid4()),
        "event_type": "cost",
        "config_hash": HEX_HASH,
        "actor": actor,
        "provenance": {},
        "occurred_at": _TS,
        "model": "claude-opus",
        "lane": "frontier-api",
    }
    fields.update(over)
    return CostEvent(**fields)


@pytest.fixture
def audit() -> Callable[..., AuditEvent]:
    return make_audit


@pytest.fixture
def cost() -> Callable[..., CostEvent]:
    return make_cost
