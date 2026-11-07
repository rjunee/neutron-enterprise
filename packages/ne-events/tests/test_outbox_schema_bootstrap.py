# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Re-running the outbox bootstrap must be a NO-OP, not a crash.

``ensure_outbox_schema`` is called on EVERY governed connector sync — ne-api,
ne-worker's ``*/15`` cron, and a host-side ``ne connect sync`` all run it against
the same database. ``create_all`` is a check-then-create, which is idempotent
only against itself; two roots interleaving between the check and the create
collide on a system catalog, and on Postgres that collision arrives as
``40P01 deadlock_detected`` — i.e. wearing ``OperationalError``'s class, not
``IntegrityError``'s. Its sibling ``ne.brain.lineage.ensure_document_lineage_schema``
already serializes for exactly this reason; this one now does too.

The pair pinned here: a re-run over an already-seeded database succeeds and
changes nothing ⟷ a genuine constraint conflict still fails loudly (no blanket
``except`` was introduced to buy the first half).
"""

from __future__ import annotations

import pathlib
import re
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import event as sa_event
from sqlalchemy.exc import IntegrityError, OperationalError

from ne.events.outbox import (
    _POSTGRES_SCHEMA_LOCK_KEY,
    ensure_outbox_schema,
    ne_outbox_cursors,
)


@pytest.fixture
def sqlite_engine() -> Iterator[sa.Engine]:
    engine = sa.create_engine("sqlite://")
    try:
        yield engine
    finally:
        engine.dispose()


def _cursor_rows(engine: sa.Engine) -> list[Any]:
    with engine.connect() as conn:
        return list(conn.execute(sa.select(ne_outbox_cursors)).mappings())


# --- half one: an already-seeded re-run is a no-op ---------------------------


def test_re_running_the_bootstrap_preserves_an_already_seeded_database(
    sqlite_engine: sa.Engine,
) -> None:
    ensure_outbox_schema(sqlite_engine)
    with sqlite_engine.begin() as conn:
        conn.execute(ne_outbox_cursors.insert().values(consumer="exporter", position=41))

    ensure_outbox_schema(sqlite_engine)
    ensure_outbox_schema(sqlite_engine)

    rows = _cursor_rows(sqlite_engine)
    assert [(row["consumer"], row["position"]) for row in rows] == [("exporter", 41)]


def test_the_fence_columns_are_added_once_and_not_re_added(sqlite_engine: sa.Engine) -> None:
    """The legacy upgrade path is itself a check-then-alter — it must not repeat."""
    with sqlite_engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE ne_outbox_cursors ("
                "consumer VARCHAR PRIMARY KEY, position BIGINT NOT NULL, "
                "event_types JSON, updated_at DATETIME)"
            )
        )

    ensure_outbox_schema(sqlite_engine)
    first = {col["name"] for col in sa.inspect(sqlite_engine).get_columns("ne_outbox_cursors")}
    assert {"fence_xmax", "fence_last_id"} <= first

    # A second ADD COLUMN of the same name is an error on both backends; the
    # existence check must still hold on the already-upgraded table.
    ensure_outbox_schema(sqlite_engine)
    second = {col["name"] for col in sa.inspect(sqlite_engine).get_columns("ne_outbox_cursors")}
    assert second == first


# --- half two: a genuine conflict still fails loudly -------------------------


def test_a_genuine_constraint_conflict_still_raises(sqlite_engine: sa.Engine) -> None:
    """Nothing was swallowed to make the re-run quiet.

    A real duplicate key — the thing a blanket ``except`` around the bootstrap
    would have hidden forever — must still abort its caller.
    """
    ensure_outbox_schema(sqlite_engine)
    with sqlite_engine.begin() as conn:
        conn.execute(ne_outbox_cursors.insert().values(consumer="exporter", position=1))

    with pytest.raises(IntegrityError), sqlite_engine.begin() as conn:
        conn.execute(ne_outbox_cursors.insert().values(consumer="exporter", position=2))


# --- the serialization itself ------------------------------------------------


def test_the_postgres_bootstrap_takes_its_advisory_lock_before_any_ddl(
    sqlite_engine: sa.Engine,
) -> None:
    """On a Postgres dialect the lock is taken FIRST, in the DDL's own transaction.

    Order is the whole point: a lock acquired after the check-then-create has
    already run serializes nothing. The engine is a real SQLAlchemy engine
    executing real statements; only the dialect NAME is swapped, which is the one
    thing the branch reads.
    """
    executed: list[tuple[int, str]] = []

    @sa_event.listens_for(sqlite_engine, "before_cursor_execute")
    def _record(conn: Any, cursor: Any, statement: str, *_rest: Any) -> None:
        executed.append((id(conn.connection.dbapi_connection), statement))

    sqlite_engine.dialect.name = "postgresql"
    try:
        with pytest.raises(OperationalError):
            # SQLite has no pg_advisory_xact_lock: the call fails, which is
            # itself the proof that the branch fired and fired FIRST.
            ensure_outbox_schema(sqlite_engine)
    finally:
        sqlite_engine.dialect.name = "sqlite"

    statements = [statement for _, statement in executed]
    assert any("pg_advisory_xact_lock" in statement for statement in statements)
    assert "pg_advisory_xact_lock" in statements[0], statements
    assert not any(statement.lstrip().upper().startswith("CREATE") for statement in statements)


def test_every_advisory_lock_key_in_the_tree_is_distinct() -> None:
    """Two bootstraps sharing one key serialize needlessly; two taking each
    other's keys in different orders DEADLOCK — which is the exact failure this
    change exists to remove, so a colliding key would reintroduce it.

    Scanned as TEXT rather than by importing the other owners: ne-events sits
    below ne-brain and ne-api in the dependency order and must not import them,
    not even from a test.
    """
    root = pathlib.Path(__file__).resolve().parents[3]
    pattern = re.compile(r"^(_[A-Z0-9_]*LOCK_KEY)\s*(?::[^=]+)?=\s*(0x[0-9A-Fa-f_]+|\d+)", re.M)
    seen: dict[int, str] = {}
    for base in ("packages", "tools"):
        for path in sorted((root / base).glob("*/src/**/*.py")):
            for name, literal in pattern.findall(path.read_text(encoding="utf-8")):
                value = int(literal, 0)
                where = f"{path.relative_to(root)}:{name}"
                assert value not in seen, f"advisory lock key collision: {where} vs {seen[value]}"
                seen[value] = where

    assert _POSTGRES_SCHEMA_LOCK_KEY in seen
    # Postgres advisory keys are signed bigints.
    assert -(2**63) <= _POSTGRES_SCHEMA_LOCK_KEY < 2**63


def test_sqlite_takes_no_advisory_lock(sqlite_engine: sa.Engine) -> None:
    """SQLite has no advisory locks and needs none — the path is unchanged."""
    executed: list[str] = []

    @sa_event.listens_for(sqlite_engine, "before_cursor_execute")
    def _record(_conn: Any, _cursor: Any, statement: str, *_rest: Any) -> None:
        executed.append(statement)

    ensure_outbox_schema(sqlite_engine)

    assert executed
    assert not any("advisory" in statement for statement in executed)
