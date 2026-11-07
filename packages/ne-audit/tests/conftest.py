# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures for the ne-audit tests.

The chain/verify/checkpoint logic runs against a REAL in-memory SQLite engine
(the SAME SQLAlchemy statements, including the append-only triggers) and against
the ``ne.contracts.testing`` memory adapters (real implementations of the frozen
ObjectStore/SecretStore seams) — nothing is mocked past the seam (plan §1.4).
``NE_TEST_POSTGRES_URL`` re-runs the parkable subset on real Postgres (see
``test_postgres_audit.py``). Async seams are exercised via ``asyncio.run``;
pytest-asyncio is NOT a dependency and must not be added.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import event

from ne.audit import ensure_audit_schema
from ne.contracts.testing.memory import MemoryObjectStore, MemorySecretStore
from ne.schema import AuditEvent, AuditOutcome, CostEvent

# A valid 64-char lowercase-hex SHA-256 digest for direct construction.
HEX_HASH = "a" * 64
_TS = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def engine() -> Iterator[sa.Engine]:
    eng = sa.create_engine("sqlite://")
    ensure_audit_schema(eng)
    try:
        yield eng
    finally:
        eng.dispose()


def _make_serialized(url: str) -> sa.Engine:
    """A file-backed SQLite engine that serializes writers (WAL + BEGIN IMMEDIATE).

    pysqlite's autocommit-emulation defers the write lock until the first write,
    so two concurrent appends could both read the head before either writes. We
    disable that emulation and force every transaction to ``BEGIN IMMEDIATE`` so
    the head lock is taken up front; ``busy_timeout`` makes the loser wait rather
    than raise. This is the documented SQLAlchemy recipe for SQLite write
    concurrency.
    """
    eng = sa.create_engine(url)

    @event.listens_for(eng, "connect")
    def _on_connect(dbapi_conn: Any, _rec: Any) -> None:  # pragma: no cover - driver glue
        dbapi_conn.isolation_level = None
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()

    @event.listens_for(eng, "begin")
    def _on_begin(conn: sa.Connection) -> None:  # pragma: no cover - driver glue
        conn.exec_driver_sql("BEGIN IMMEDIATE")

    return eng


@pytest.fixture
def file_engine(tmp_path: Path) -> Iterator[sa.Engine]:
    url = f"sqlite:///{tmp_path / 'audit.db'}"
    eng = _make_serialized(url)
    ensure_audit_schema(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def mem_object_store() -> MemoryObjectStore:
    return MemoryObjectStore()


@pytest.fixture
def mem_secret_store() -> MemorySecretStore:
    return MemorySecretStore()


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
        "cost_usd": 0.5,
    }
    fields.update(over)
    return CostEvent(**fields)


@pytest.fixture
def audit() -> Callable[..., AuditEvent]:
    return make_audit


@pytest.fixture
def cost() -> Callable[..., CostEvent]:
    return make_cost
