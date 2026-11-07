# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""LIVE-PARKABLE Postgres parity for the principal store.

Env-gated on ``NE_TEST_POSTGRES_URL`` (the build host has no Docker daemon, so
this module auto-skips — pending-live-verification ledger). When a real Postgres
endpoint is configured it re-runs the store behaviors whose dialect semantics
differ from SQLite: schema create, upsert/tombstone cascade, membership diff,
nested-group expansion, and snapshot-transaction atomicity.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa

from ne.permissions.expand import PrincipalExpander
from ne.permissions.models import PrincipalKind, SyncedPrincipal
from ne.permissions.store import PrincipalStore, ensure_permissions_schema, metadata

_URL = os.environ.get("NE_TEST_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    not _URL,
    reason="live Postgres not configured (set NE_TEST_POSTGRES_URL); "
    "pending-live-verification ledger",
)

NOW = datetime(2026, 7, 6, tzinfo=UTC)
LATER = datetime(2026, 7, 6, 13, tzinfo=UTC)


@pytest.fixture
def pg_store() -> Iterator[PrincipalStore]:
    assert _URL is not None
    engine = sa.create_engine(_URL)
    metadata.drop_all(engine)
    ensure_permissions_schema(engine)
    try:
        yield PrincipalStore(engine)
    finally:
        metadata.drop_all(engine)
        engine.dispose()


def _user(pid: str) -> SyncedPrincipal:
    return SyncedPrincipal(principal_id=pid, kind=PrincipalKind.USER)


def test_upsert_and_tombstone_cascade(pg_store: PrincipalStore) -> None:
    pg_store.upsert_principals([_user("u1")], source="entra", now=NOW)
    pg_store.set_member_groups("u1", ["g1", "g2"], source="entra", now=NOW)
    assert pg_store.direct_groups_of(["u1"])["u1"] == frozenset({"g1", "g2"})
    pg_store.mark_removed(["u1"], now=LATER)
    assert not pg_store.is_known("u1")
    assert pg_store.direct_groups_of(["u1"]) == {}


def test_membership_diff(pg_store: PrincipalStore) -> None:
    pg_store.set_group_members("g", ["a", "b"], source="s", now=NOW)
    added, removed = pg_store.set_group_members("g", ["b", "c"], source="s", now=LATER)
    assert added == frozenset({"c"})
    assert removed == frozenset({"a"})


def test_nested_expansion(pg_store: PrincipalStore) -> None:
    pg_store.upsert_principals([_user("u")], source="s", now=NOW)
    pg_store.set_member_groups("u", ["g1"], source="s", now=NOW)
    pg_store.set_member_groups("g1", ["g2"], source="s", now=NOW)
    assert {"u", "g1", "g2"} <= PrincipalExpander(pg_store).expand("u")


def test_transaction_atomicity(pg_store: PrincipalStore) -> None:
    with pytest.raises(RuntimeError), pg_store.transaction():
        pg_store.upsert_principals([_user("u1")], source="s", now=NOW)
        raise RuntimeError("rollback")
    assert not pg_store.is_known("u1")
