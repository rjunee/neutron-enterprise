# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Tests for the cross-module migration runner + reversible-migration policy."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import cast

import pytest
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from ne.cli.migrate import (
    Migration,
    MigrationDriftError,
    MigrationPolicyError,
    applied_versions,
    downgrade,
    ensure_migrations_schema,
    upgrade,
    validate_registry,
)
from ne.cli.migrations import REGISTRY

# --- policy ------------------------------------------------------------


def test_policy_refuses_unmarked_irreversible() -> None:
    bad = Migration(version=1, name="bad", up=lambda c: None)
    with pytest.raises(MigrationPolicyError, match="unmarked irreversible"):
        validate_registry((bad,))


def test_policy_refuses_blank_justification() -> None:
    bad = Migration(version=1, name="bad", up=lambda c: None, irreversible_justification="   ")
    with pytest.raises(MigrationPolicyError, match="unmarked irreversible"):
        validate_registry((bad,))


def test_policy_refuses_contradictory_marking() -> None:
    bad = Migration(
        version=1,
        name="bad",
        up=lambda c: None,
        down=lambda c: None,
        irreversible_justification="cannot actually revert this",
    )
    with pytest.raises(MigrationPolicyError, match="contradictory"):
        validate_registry((bad,))


def test_registry_versions_strictly_increasing_and_unique() -> None:
    dup = Migration(version=1, name="a", up=lambda c: None, down=lambda c: None)
    dup2 = Migration(version=1, name="b", up=lambda c: None, down=lambda c: None)
    with pytest.raises(MigrationPolicyError, match="duplicate"):
        validate_registry((dup, dup2))

    out_of_order = Migration(version=1, name="a", up=lambda c: None, down=lambda c: None)
    lower = Migration(version=0, name="b", up=lambda c: None, down=lambda c: None)
    with pytest.raises(MigrationPolicyError):
        validate_registry((out_of_order, lower))


# --- runner fixture registry --------------------------------------------


def _create_table_up(conn: Connection) -> None:
    conn.execute(text("CREATE TABLE widgets (id INTEGER PRIMARY KEY, name TEXT NOT NULL)"))


def _create_table_down(conn: Connection) -> None:
    conn.execute(text("DROP TABLE widgets"))


def _add_column_up(conn: Connection) -> None:
    conn.execute(text("ALTER TABLE widgets ADD COLUMN color TEXT"))
    conn.execute(text("UPDATE widgets SET color = 'unknown' WHERE color IS NULL"))


def _add_column_down(conn: Connection) -> None:
    conn.execute(text("ALTER TABLE widgets DROP COLUMN color"))


def _irreversible_up(conn: Connection) -> None:
    conn.execute(text("CREATE TABLE widget_archive (id INTEGER PRIMARY KEY)"))


def _failing_up(conn: Connection) -> None:
    conn.execute(text("CREATE TABLE will_be_rolled_back (id INTEGER PRIMARY KEY)"))
    raise RuntimeError("boom")


def _failing_multi_ddl_up(conn: Connection) -> None:
    conn.execute(text("CREATE TABLE mid_ddl_a (id INTEGER PRIMARY KEY)"))
    conn.execute(text("CREATE TABLE mid_ddl_b (id INTEGER PRIMARY KEY)"))
    raise RuntimeError("boom-mid-ddl")


@pytest.fixture
def fixture_registry() -> tuple[Migration, ...]:
    return validate_registry(
        (
            Migration(
                version=1, name="create-widgets", up=_create_table_up, down=_create_table_down
            ),
            Migration(version=2, name="add-widget-color", up=_add_column_up, down=_add_column_down),
            Migration(
                version=3,
                name="archive-widgets",
                up=_irreversible_up,
                irreversible_justification="widget_archive is append-only audit data; dropping it "
                "would destroy history we must retain",
            ),
        )
    )


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return sa.create_engine(f"sqlite:///{tmp_path / 'test.db'}")


# --- runner: upgrade -----------------------------------------------------


def test_up_applies_in_order_and_records_versions(
    engine: Engine, fixture_registry: tuple[Migration, ...]
) -> None:
    applied = upgrade(engine, fixture_registry)
    assert applied == [1, 2, 3]
    assert applied_versions(engine) == [1, 2, 3]
    with engine.connect() as conn:
        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(widgets)"))}
        assert {"id", "name", "color"} <= cols


def test_up_is_idempotent(engine: Engine, fixture_registry: tuple[Migration, ...]) -> None:
    upgrade(engine, fixture_registry)
    with engine.connect() as conn:
        before = conn.execute(text("SELECT COUNT(*) FROM ne_migrations")).scalar_one()
    applied_again = upgrade(engine, fixture_registry)
    assert applied_again == []
    with engine.connect() as conn:
        after = conn.execute(text("SELECT COUNT(*) FROM ne_migrations")).scalar_one()
    assert before == after == 3


def test_up_to_target_version_stops_there(
    engine: Engine, fixture_registry: tuple[Migration, ...]
) -> None:
    applied = upgrade(engine, fixture_registry, to=2)
    assert applied == [1, 2]
    assert applied_versions(engine) == [1, 2]


def test_failed_migration_rolls_back_atomically(engine: Engine) -> None:
    registry = validate_registry(
        (Migration(version=1, name="failer", up=_failing_up, down=lambda c: None),)
    )
    with pytest.raises(RuntimeError, match="boom"):
        upgrade(engine, registry)
    assert applied_versions(engine) == []
    with engine.connect() as conn:
        tables = {
            row[0]
            for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        }
    assert "will_be_rolled_back" not in tables


def test_transactional_ddl_recipe_covers_pre_pooled_connections(engine: Engine) -> None:
    """The checkout-event recipe repairs connections pooled BEFORE the runner
    ever touched the engine (Codex P2-A mechanism proof). Pre-fix (a
    ``"connect"``-event recipe), the DBAPI connection pooled here keeps
    pysqlite's legacy isolation_level of ``''`` because ``"connect"`` never
    fires for it again — only newly-created DBAPI connections get patched.
    """
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))  # pool now holds a pre-recipe DBAPI connection

    ensure_migrations_schema(engine)  # first runner touchpoint: installs the recipe

    raw = engine.raw_connection()
    try:
        dbapi = cast(sqlite3.Connection, raw.dbapi_connection)
        assert dbapi.isolation_level is None
    finally:
        raw.close()


def test_atomicity_holds_on_pre_used_engine(engine: Engine) -> None:
    """Codex P2-A invariant: an already-used (pre-pooled) engine + a migration
    that fails mid-DDL (after two CREATE TABLEs) leaves ZERO partial DDL and
    no tracking row — full rollback, even though the pool held a connection
    from before the runner installed its transactional-DDL recipe."""
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))  # pre-use the engine before any runner call

    registry = validate_registry(
        (Migration(version=1, name="failer", up=_failing_multi_ddl_up, down=lambda c: None),)
    )
    with pytest.raises(RuntimeError, match="boom-mid-ddl"):
        upgrade(engine, registry)
    assert applied_versions(engine) == []
    with engine.connect() as conn:
        tables = {
            row[0]
            for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        }
    assert "mid_ddl_a" not in tables
    assert "mid_ddl_b" not in tables


# --- runner: downgrade -----------------------------------------------------


def test_down_reverts_in_reverse_order(
    engine: Engine, fixture_registry: tuple[Migration, ...]
) -> None:
    two_step = fixture_registry[:2]
    upgrade(engine, two_step)
    reverted = downgrade(engine, two_step, to=0)
    assert reverted == [2, 1]
    assert applied_versions(engine) == []
    with engine.connect() as conn:
        tables = {
            row[0]
            for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        }
    assert "widgets" not in tables


def test_down_refuses_crossing_irreversible_migration(
    engine: Engine, fixture_registry: tuple[Migration, ...]
) -> None:
    upgrade(engine, fixture_registry)
    with pytest.raises(MigrationPolicyError, match="widget_archive is append-only") as excinfo:
        downgrade(engine, fixture_registry, to=0)
    assert "3" in str(excinfo.value)
    # Nothing was reverted: all three versions remain applied.
    assert applied_versions(engine) == [1, 2, 3]


def test_registry_database_drift_fails_closed(
    engine: Engine, fixture_registry: tuple[Migration, ...]
) -> None:
    upgrade(engine, fixture_registry)
    shrunk_registry = fixture_registry[:2]
    with pytest.raises(MigrationDriftError):
        upgrade(engine, shrunk_registry)
    with pytest.raises(MigrationDriftError):
        downgrade(engine, shrunk_registry, to=0)


# --- the real central registry --------------------------------------------


def test_real_registry_passes_policy_and_applies_cleanly(engine: Engine) -> None:
    # REGISTRY is already validated at import time; re-validating is itself
    # part of the proof (idempotent, no exception).
    validated = validate_registry(REGISTRY)
    applied = upgrade(engine, validated)
    assert len(applied) == len(validated)
    assert applied_versions(engine) == [m.version for m in validated]
    # Re-applying is a no-op.
    assert upgrade(engine, validated) == []
