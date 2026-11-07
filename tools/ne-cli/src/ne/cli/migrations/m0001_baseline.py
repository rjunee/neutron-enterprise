# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Migration 0001 — platform baseline.

Per-package schemas are created by each package's own ``ensure_*_schema()``
helper (e.g. ``ne.runtime.memory_store.ensure_memory_schema``) at "v0" — those
are NOT cross-module migrations and are out of scope for this runner.
Cross-module migrations (this registry) start here, at v1: a trivial,
reversible baseline table that proves the migration MACHINERY (ordering,
transactional apply, tracking, reversibility) works end-to-end before any real
cross-module migration is ever written.

The SQL is deliberately ANSI-portable (no dialect-specific syntax) so the same
migration runs unmodified against sqlite (v1 fixtures/tests) and Postgres
(production).
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ne.cli.migrate import Migration


def _up(conn: Connection) -> None:
    conn.execute(text("CREATE TABLE ne_platform_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"))
    conn.execute(
        text("INSERT INTO ne_platform_meta (key, value) VALUES (:key, :value)"),
        {"key": "schema_baseline", "value": "v1"},
    )


def _down(conn: Connection) -> None:
    conn.execute(text("DROP TABLE ne_platform_meta"))


MIGRATION = Migration(
    version=1,
    name="platform-baseline",
    up=_up,
    down=_down,
)
