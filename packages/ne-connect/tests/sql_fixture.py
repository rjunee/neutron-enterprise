# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Shared file-backed SQLite fixture DB + settings factory (NE-046 test suites).

``build_sql_fixture_db`` creates three tables proving every ACL mode and the
row-shaping/cursor semantics the SQL connector must honor:

* ``docs`` — static-principals ACL mode; a ``row_filter`` excludes the one
  ``draft`` row; a string cursor on ``updated_at``.
* ``notes`` — per-row ``principal_column`` ACL mode (comma-separated
  multi-principal support); an int cursor on ``id``. Row 2's principal is
  ``NULL`` (quarantine: no principal) and row 9's is the reserved
  ``acl:tenant-all`` label FORGED IN DATA (quarantine: NE-CKD2 — a source may
  never mint a reserved label). Ids 9 vs 10 deliberately prove a TYPED int
  cursor compare (10 > 9), not a lexicographic string compare (where "10" < "9").
* ``announcements`` — static ``acl:tenant-all`` (org-wide visible), no cursor
  configured (always a full re-scan).

A file-backed sqlite (never ``sqlite://``/``:memory:``) is required: dlt's
chunked reader opens its own connections against the engine, and an in-memory
sqlite database is scoped to a single connection.

Expected first-run numbers used across the NE-046 test suites: ``written=7``
(3 published docs + 2 resolvable notes + 2 announcements),
``acl_quarantined=2`` (notes id 2 and id 9).
"""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa

from ne.connect.sql.config import SqlConnectorSettings, SqlTableAclConfig, SqlTableConfig

__all__ = ["build_sql_fixture_db", "sql_settings"]


def build_sql_fixture_db(path: Path) -> None:
    """Create + populate the fixture SQLite database at ``path``."""
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "CREATE TABLE docs ("
                    "id INTEGER PRIMARY KEY, title TEXT, body TEXT, "
                    "status TEXT, updated_at TEXT)"
                )
            )
            conn.execute(
                sa.text(
                    "INSERT INTO docs (id, title, body, status, updated_at) VALUES "
                    "(1, 'T1', 'B1', 'published', '2026-01-01T00:00:00'), "
                    "(2, 'T2', 'B2', 'published', '2026-01-02T00:00:00'), "
                    "(3, 'T3', 'B3', 'published', '2026-01-03T00:00:00'), "
                    "(4, 'T4', 'B4', 'draft', '2026-01-04T00:00:00')"
                )
            )
            conn.execute(
                sa.text(
                    "CREATE TABLE notes (id INTEGER PRIMARY KEY, note TEXT, owner_principal TEXT)"
                )
            )
            conn.execute(
                sa.text(
                    "INSERT INTO notes (id, note, owner_principal) VALUES "
                    "(1, 'n1', 'u-bob'), "
                    "(2, 'n2', NULL), "
                    "(9, 'n9', 'acl:tenant-all'), "
                    "(10, 'n10', 'u-ada,g-eng')"
                )
            )
            conn.execute(
                sa.text("CREATE TABLE announcements (id INTEGER PRIMARY KEY, message TEXT)")
            )
            conn.execute(
                sa.text("INSERT INTO announcements (id, message) VALUES (1, 'a1'), (2, 'a2')")
            )
    finally:
        engine.dispose()


def sql_settings(db_path: Path, **overrides: object) -> SqlConnectorSettings:
    """A valid, fixture-pointed settings object (``allow_sqlite=True``)."""
    tables = (
        SqlTableConfig(
            table="docs",
            id_column="id",
            content_columns=("title", "body"),
            metadata_columns=("updated_at",),
            cursor_column="updated_at",
            cursor_type="string",
            row_filter="status = 'published'",
            acl=SqlTableAclConfig(principals=("g-eng",)),
        ),
        SqlTableConfig(
            table="notes",
            id_column="id",
            content_columns=("note",),
            cursor_column="id",
            cursor_type="int",
            acl=SqlTableAclConfig(principal_column="owner_principal", principal_separator=","),
        ),
        SqlTableConfig(
            table="announcements",
            id_column="id",
            content_columns=("message",),
            acl=SqlTableAclConfig(principals=("acl:tenant-all",)),
        ),
    )
    kwargs: dict[str, object] = {
        "allow_sqlite": True,
        "connection_url": f"sqlite:///{db_path}",
        "owner": "data-eng@acme.test",
        "source_name": "sql-acme",
        "tables": tables,
    }
    kwargs.update(overrides)
    return SqlConnectorSettings(**kwargs)  # type: ignore[arg-type]
