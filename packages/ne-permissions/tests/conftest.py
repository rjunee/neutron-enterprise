# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures for ne-permissions tests.

A real sqlite-file engine (no mock past the store), the ne-brain/ne-connect
``frozen_config`` pattern, and a REAL ne-audit ``PostgresAuditSink`` over sqlite
(the audit dev-dep seam — events land in the hash-chained store, not a mock).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa

from ne.audit import PostgresAuditSink, ensure_audit_schema
from ne.contracts import freeze_config_hash, reset_config_hash_for_testing
from ne.contracts.config import NeSettings
from ne.events import ensure_outbox_schema
from ne.permissions.store import PrincipalStore, ensure_permissions_schema

FIXTURES = Path(__file__).parent / "fixtures"


class _DemoSettings(NeSettings):
    demo: int = 7


@pytest.fixture
def frozen_config() -> Iterator[str]:
    """Freeze a deterministic config_hash for the duration of a test."""
    reset_config_hash_for_testing()
    digest = freeze_config_hash(_DemoSettings())
    try:
        yield digest
    finally:
        reset_config_hash_for_testing()


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[sa.Engine]:
    """A sqlite-file engine with the permissions schema ensured."""
    eng = sa.create_engine(f"sqlite:///{tmp_path / 'perms.db'}")
    ensure_permissions_schema(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def store(engine: sa.Engine) -> PrincipalStore:
    return PrincipalStore(engine)


@pytest.fixture
def audit_sink() -> Iterator[PostgresAuditSink]:
    """A REAL hash-chained audit sink over an in-memory sqlite DB (dev-dep seam)."""
    eng = sa.create_engine("sqlite://")
    ensure_outbox_schema(eng)
    ensure_audit_schema(eng)
    try:
        yield PostgresAuditSink(eng)
    finally:
        eng.dispose()
