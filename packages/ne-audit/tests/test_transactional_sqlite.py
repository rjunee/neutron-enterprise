# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Executable proof of the SPEC §11 pysqlite dual-append stranding gap + its fix.

The strand probe reproduces the governed dual-append shape: an idempotent inner
write inside a ``conn.begin_nested()`` SAVEPOINT, then a FAILING leg that triggers
an outer ``engine.begin()`` rollback. On plain pysqlite the SAVEPOINT write is
emitted before the deferred outer BEGIN and survives the rollback (stranded,
count==1); with :func:`ne.audit.ensure_transactional_sqlite` the SAVEPOINT nests
inside a real outer transaction and the rollback undoes it (count==0).

Engines are FILE-BACKED — in-memory ``sqlite://`` gives each pooled connection
its own database, which would invalidate the reconnect-and-count check.
"""

from __future__ import annotations

import types
from pathlib import Path
from typing import cast

import pytest
import sqlalchemy as sa
from sqlalchemy import event as sa_event

import ne.audit.engine
from ne.audit import ensure_transactional_sqlite
from ne.audit.sink import PostgresAuditSink
from ne.audit.store import ensure_audit_schema


class _Boom(Exception):
    """The simulated audit-append failure, raised after the SAVEPOINT is released."""


def _strand_probe(engine: sa.Engine) -> int:
    """Run the dual-append shape with a failing leg; return surviving row count.

    A begin_nested() SAVEPOINT write, the SAVEPOINT released, then a raise inside
    the outer ``engine.begin()`` block forcing an outer rollback. Reconnects on a
    fresh connection and returns how many probe rows survived (0 = correctly
    rolled back, 1 = stranded).
    """
    md = sa.MetaData()
    probe = sa.Table("ne_clean2_probe", md, sa.Column("id", sa.Integer, primary_key=True))
    md.create_all(engine)
    try:
        with engine.begin() as conn:
            with conn.begin_nested():
                conn.execute(probe.insert().values(id=1))
            # SAVEPOINT released; now the audit leg fails -> outer rollback.
            raise _Boom()
    except _Boom:
        pass
    with engine.connect() as conn:
        return int(conn.execute(sa.select(sa.func.count()).select_from(probe)).scalar_one())


def _file_engine(tmp_path: Path, name: str = "probe.db") -> sa.Engine:
    return sa.create_engine(f"sqlite:///{tmp_path / name}")


def test_savepoint_write_stranded_without_recipe(tmp_path: Path) -> None:
    """RED-BEFORE: an un-recipe'd pysqlite engine strands the SAVEPOINT row."""
    engine = _file_engine(tmp_path)
    assert _strand_probe(engine) == 1


def test_recipe_restores_outer_rollback_of_savepoint_writes(tmp_path: Path) -> None:
    """GREEN-AFTER: with the recipe applied, the outer rollback undoes the write."""
    engine = _file_engine(tmp_path)
    ensure_transactional_sqlite(engine)
    assert _strand_probe(engine) == 0


def test_recipe_covers_pre_pooled_connections(tmp_path: Path) -> None:
    """The checkout event (not connect) covers connections pooled BEFORE the patch."""
    engine = _file_engine(tmp_path)
    # Force a connection into the pool BEFORE the helper is installed.
    with engine.connect() as conn:
        conn.execute(sa.select(sa.literal(1)))
    ensure_transactional_sqlite(engine)
    assert _strand_probe(engine) == 0


def test_idempotent_double_application(tmp_path: Path) -> None:
    """Re-applying the helper does not stack listeners (no double-BEGIN error)."""
    engine = _file_engine(tmp_path)
    ensure_transactional_sqlite(engine)
    ensure_transactional_sqlite(engine)
    assert _strand_probe(engine) == 0


def test_noop_on_non_sqlite_dialect(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Guard direction pinned: zero listener registrations on a non-sqlite dialect."""
    recorded: list[str] = []
    # ``ne.audit.engine`` does ``from sqlalchemy import event`` and calls
    # ``event.listens_for`` — the SAME module object as ``sa_event`` here, so
    # patching this attribute is observed by the helper.
    real_listens_for = sa_event.listens_for

    def _recorder(target: object, identifier: str, *args: object, **kwargs: object) -> object:
        recorded.append(identifier)
        return real_listens_for(target, identifier, *args, **kwargs)

    monkeypatch.setattr(sa_event, "listens_for", _recorder)

    fake_pg = cast(
        sa.Engine,
        types.SimpleNamespace(dialect=types.SimpleNamespace(name="postgresql")),
    )
    ensure_transactional_sqlite(fake_pg)
    assert recorded == []
    assert fake_pg not in ne.audit.engine._patched_engines

    real_sqlite = _file_engine(tmp_path)
    ensure_transactional_sqlite(real_sqlite)
    assert recorded == ["checkout", "begin"]
    assert real_sqlite in ne.audit.engine._patched_engines


def test_sink_constructor_applies_recipe(tmp_path: Path) -> None:
    """PostgresAuditSink.__init__ patches a handed sqlite engine defensively."""
    engine = _file_engine(tmp_path)
    ensure_audit_schema(engine)
    PostgresAuditSink(engine)
    assert _strand_probe(engine) == 0


def test_skips_engine_with_foreign_begin_listener(tmp_path: Path) -> None:
    """Codex NE-CLEAN2 P2: an engine already carrying a foreign ``begin`` listener
    (e.g. a ``BEGIN IMMEDIATE`` fixture or the migration helper's explicit BEGIN)
    must NOT get a second stacked ``begin`` listener — a duplicate BEGIN inside an
    open transaction raises sqlite3.OperationalError."""
    engine = _file_engine(tmp_path)

    # A foreign, complete transaction recipe already installed by other code.
    @sa_event.listens_for(engine, "connect")
    def _foreign_connect(dbapi_connection, _record):  # type: ignore[no-untyped-def]
        dbapi_connection.isolation_level = None

    @sa_event.listens_for(engine, "begin")
    def _foreign_begin(conn):  # type: ignore[no-untyped-def]
        conn.exec_driver_sql("BEGIN IMMEDIATE")

    before = len(list(engine.dispatch.begin))
    ensure_transactional_sqlite(engine)
    # No second begin listener stacked, and the engine was NOT recorded as ours.
    assert len(list(engine.dispatch.begin)) == before == 1
    assert engine not in ne.audit.engine._patched_engines

    # And the pre-instrumented engine still works — no double-BEGIN crash.
    ensure_audit_schema(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("SELECT 1")
