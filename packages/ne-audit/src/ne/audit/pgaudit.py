# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""pgAudit wiring — statement-level DB audit UNDER the app hash chain.

DEFENSE IN DEPTH: pgAudit (a PostgreSQL extension, PostgreSQL License) logs
statement-level activity at the database layer. It is COMPLEMENTARY to — not a
substitute for — the app-level hash chain: the chain is the tamper-EVIDENCE
mechanism (with signed WORM checkpoints), while pgAudit captures the raw SQL an
operator or app ran against the database. Together they answer both "was the
audit log tampered with?" (chain + checkpoints) and "what statements touched the
database?" (pgAudit).

The dev-stack image (``postgres:18.6-alpine``) does NOT bundle pgAudit, and
pgAudit must be loaded via ``shared_preload_libraries='pgaudit'`` at SERVER
START (it cannot be enabled purely at runtime). The live proof runs in CI's
live-postgres job against a SECOND, pgAudit-capable Postgres service
container (``packages/ne-audit/tests/test_pgaudit_live.py``, env-gated on
``NE_TEST_PGAUDIT_URL``) — it is a must-run proof there, not a skip. To run
Postgres with pgAudit under docker, add the flag::

    -c shared_preload_libraries=pgaudit
"""

from __future__ import annotations

import sqlalchemy as sa

#: Recommended pgAudit settings (write/DDL/role changes, relation-level, no params).
PGAUDIT_SETTINGS: dict[str, str] = {
    "pgaudit.log": "write, ddl, role",
    "pgaudit.log_catalog": "off",
    "pgaudit.log_parameter": "off",
    "pgaudit.log_relation": "on",
}


def pgaudit_available(conn: sa.Connection) -> bool:
    """True if the pgAudit extension is available in this Postgres server."""
    row = conn.execute(
        sa.text("SELECT 1 FROM pg_available_extensions WHERE name = 'pgaudit'")
    ).first()
    return row is not None


def enable_pgaudit(conn: sa.Connection) -> None:
    """Create the extension and apply the recommended settings via ``ALTER SYSTEM``.

    Requires ``shared_preload_libraries='pgaudit'`` at server start (see the module
    docstring) and superuser privileges; ``ALTER SYSTEM`` persists to
    ``postgresql.auto.conf`` and ``pg_reload_conf()`` applies the reloadable ones.

    LIVE-VALIDATED contract (NE-070): ``ALTER SYSTEM`` cannot run inside a
    transaction block on Postgres, and SQLAlchemy's default ``connect()`` opens
    one — so calling this on a plain connection raises
    ``25001 (active SQL transaction)``. Callers MUST use an AUTOCOMMIT
    connection, e.g.::

        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
            enable_pgaudit(c)

    Both halves of this contract are pinned by live tests against a real,
    pgAudit-capable Postgres server:
    ``test_pgaudit_live.py::test_enable_pgaudit_requires_autocommit`` pins the
    in-transaction failure (sqlstate 25001), and
    ``test_pgaudit_live.py::test_pgaudit_settings_apply`` pins the AUTOCOMMIT
    success path.
    """
    conn.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS pgaudit")
    for key, value in PGAUDIT_SETTINGS.items():
        conn.exec_driver_sql(f"ALTER SYSTEM SET {key} = '{value}'")
    conn.exec_driver_sql("SELECT pg_reload_conf()")
