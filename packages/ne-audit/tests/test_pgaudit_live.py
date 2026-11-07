# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""PROMOTED live pgAudit proof — must-run, not a nested skip.

Env-gated on ``NE_TEST_PGAUDIT_URL``. Unlike the old nested-skip on
``test_postgres_audit.py``, when this env var IS set a missing pgaudit
extension is a FAILURE (``pytest.fail``), not a skip: this module only runs
against a Postgres that is supposed to have pgAudit precompiled, so a missing
extension means the wrong image is wired up, not "nothing to prove here".

In CI this runs in the ``live-postgres`` job against a SECOND, digest-pinned
service container — the CNCF CloudNativePG "standard" operand image — started
with ``shared_preload_libraries=pgaudit`` and ``logging_collector=on`` (see
``.github/workflows/ci.yml``). The module-level skip (no env var) keeps this
file inert on every other host, exactly like ``test_postgres_audit.py``.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError

from ne.audit.pgaudit import PGAUDIT_SETTINGS, enable_pgaudit, pgaudit_available

_URL = os.environ.get("NE_TEST_PGAUDIT_URL")

pytestmark = pytest.mark.skipif(
    not _URL,
    reason="pgAudit-capable live Postgres not configured (set NE_TEST_PGAUDIT_URL)",
)

_POLL_DEADLINE_S = 15.0
_POLL_INTERVAL_S = 0.2


def _normalize_pgaudit_log(value: str) -> set[str]:
    """Normalize a comma-separated ``pgaudit.log`` value for order-insensitive compare."""
    return {part.strip() for part in value.split(",") if part.strip()}


def _probe_audit_lines(content: str, probe: str) -> list[str]:
    """The audit lines for one probe table — the ONE derivation of that set.

    Both the poll loop and the assertions below read the log through this
    function and the same two class filters, so the wait cannot come to rest on
    a different line than the one the assertion tests.
    """
    return [line for line in content.splitlines() if "AUDIT:" in line and probe in line]


@pytest.fixture
def pgaudit_engine() -> Iterator[sa.Engine]:
    assert _URL is not None
    engine = sa.create_engine(_URL)
    with engine.connect() as conn:
        if not pgaudit_available(conn):
            pytest.fail(
                "NE_TEST_PGAUDIT_URL is set but the pgaudit extension is not "
                "available on this server — wrong image?"
            )
    try:
        yield engine
    finally:
        engine.dispose()


def _enable_pgaudit_live(engine: sa.Engine) -> None:
    """Enable pgaudit on an AUTOCOMMIT connection, then poll until settings apply.

    ``pg_reload_conf()`` is asynchronous, so a ``SHOW`` on a connection opened
    immediately after can briefly observe stale values. Poll fresh connections
    until ``pgaudit.log`` (order-insensitive) matches the configured value.
    """
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        enable_pgaudit(conn)

    wanted = _normalize_pgaudit_log(PGAUDIT_SETTINGS["pgaudit.log"])
    deadline = time.monotonic() + _POLL_DEADLINE_S
    last_seen: str | None = None
    while time.monotonic() < deadline:
        with engine.connect() as conn:
            last_seen = str(conn.exec_driver_sql("SHOW pgaudit.log").scalar())
        if _normalize_pgaudit_log(last_seen) == wanted:
            return
        time.sleep(_POLL_INTERVAL_S)
    raise AssertionError(
        f"pgaudit.log did not converge to {wanted!r} within "
        f"{_POLL_DEADLINE_S}s (last seen: {last_seen!r})"
    )


def test_pgaudit_available(pgaudit_engine: sa.Engine) -> None:
    with pgaudit_engine.connect() as conn:
        assert pgaudit_available(conn) is True
        default_version = conn.execute(
            sa.text("SELECT default_version FROM pg_available_extensions WHERE name = 'pgaudit'")
        ).scalar_one()
    assert default_version is not None


def test_enable_pgaudit_requires_autocommit(pgaudit_engine: sa.Engine) -> None:
    """NE-070, live-verified: ALTER SYSTEM inside a transaction raises 25001."""
    with pytest.raises(DBAPIError) as ei, pgaudit_engine.begin() as conn:
        enable_pgaudit(conn)
    assert getattr(ei.value.orig, "sqlstate", None) == "25001"


def test_pgaudit_settings_apply(pgaudit_engine: sa.Engine) -> None:
    _enable_pgaudit_live(pgaudit_engine)
    with pgaudit_engine.connect() as conn:
        for key, expected in PGAUDIT_SETTINGS.items():
            actual = str(conn.exec_driver_sql(f"SHOW {key}").scalar())
            if key == "pgaudit.log":
                assert _normalize_pgaudit_log(actual) == _normalize_pgaudit_log(expected)
            else:
                assert actual == expected


def test_pgaudit_statement_capture_with_log_parameter_off(pgaudit_engine: sa.Engine) -> None:
    """The SPEC-row proof: real statement capture with log_parameter=off honored.

    A parameterized INSERT carrying a UUID sentinel must appear in the server
    log as an audit line WITHOUT the sentinel's value — psycopg's extended
    protocol binds parameters server-side, and pgaudit logs ``<not logged>``
    in their place when ``pgaudit.log_parameter = off``. The sentinel must
    never appear anywhere in the log content.
    """
    _enable_pgaudit_live(pgaudit_engine)
    probe = f"ne_pgaudit_probe_{uuid.uuid4().hex[:8]}"
    sentinel = f"NE-SENTINEL-{uuid.uuid4().hex}"

    try:
        with pgaudit_engine.begin() as conn:
            conn.exec_driver_sql(f"CREATE TABLE {probe} (id int, secret text)")
            conn.execute(
                sa.text(f"INSERT INTO {probe} (id, secret) VALUES (:i, :s)"),
                {"i": 1, "s": sentinel},
            )

        # The wait must be satisfied by the SAME lines the assertions test. The
        # server flushes its log incrementally, so the DDL line lands first and
        # on its own satisfies any ``probe in content and "AUDIT:" in content``
        # substring check — while the INSERT line this test is actually about is
        # still unwritten. Waiting on the substring therefore exits early on a
        # slow flush and the WRITE assertion fails, which reads as a flake and is
        # not one. Poll on the two derived line sets instead.
        deadline = time.monotonic() + _POLL_DEADLINE_S
        content: str | None = None
        insert_lines: list[str] = []
        ddl_lines: list[str] = []
        while True:
            with pgaudit_engine.connect() as conn:
                logfile = conn.execute(sa.text("SELECT pg_current_logfile()")).scalar()
                assert logfile is not None, (
                    "pg_current_logfile() returned NULL — is logging_collector on?"
                )
                candidate = conn.execute(
                    sa.text("SELECT pg_read_file(:p)"), {"p": logfile}
                ).scalar_one()
            assert isinstance(candidate, str)
            content = candidate
            audit_lines = _probe_audit_lines(content, probe)
            insert_lines = [line for line in audit_lines if "WRITE,INSERT,TABLE" in line]
            ddl_lines = [line for line in audit_lines if "DDL,CREATE TABLE" in line]
            if insert_lines and ddl_lines:
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(_POLL_INTERVAL_S)

        assert content is not None
        audit_lines = _probe_audit_lines(content, probe)
        assert audit_lines, f"no AUDIT lines mentioning {probe!r} found in server log"

        assert insert_lines, f"no WRITE,INSERT,TABLE audit line found: {audit_lines}"
        assert ddl_lines, f"no DDL,CREATE TABLE audit line found: {audit_lines}"

        assert any("<not logged>" in line for line in insert_lines), (
            f"log_parameter=off not honored — expected '<not logged>' in: {insert_lines}"
        )
        assert sentinel not in content, (
            "sentinel value leaked into the server log — log_parameter=off was NOT honored"
        )
    finally:
        with pgaudit_engine.begin() as conn:
            conn.exec_driver_sql(f"DROP TABLE IF EXISTS {probe}")
