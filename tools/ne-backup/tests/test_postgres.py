# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Postgres docker-exec command construction (fast unit guards, no live docker)."""

from __future__ import annotations

from pathlib import Path
from typing import IO

import pytest

from ne.backup.errors import NeBackupError
from ne.backup.postgres import _AGE_170_POSTCOMMIT_SQL, _AGE_170_RECONCILIATION_SQL, Postgres
from ne.backup.runner import RunResult


class _CapturingRunner:
    def __init__(self, stdout: bytes = b"") -> None:
        self.calls: list[list[str]] = []
        self._stdout = stdout

    def run(self, args: list[str], *, stdin: bytes | None = None, check: bool = True) -> RunResult:
        self.calls.append(args)
        return RunResult(returncode=0, stdout=self._stdout, stderr=b"")

    def run_stream(
        self,
        args: list[str],
        *,
        stdin_file: IO[bytes] | None = None,
        stdout_file: IO[bytes] | None = None,
        check: bool = True,
    ) -> RunResult:
        self.calls.append(args)
        return RunResult(returncode=0, stdout=b"", stderr=b"")


class _StreamRecordingRunner:
    """Records both the buffered ``run`` (DROP/CREATE) and the streaming
    ``run_stream`` (dump/restore) so P2-17 can assert dumps/restores go through
    OPEN FILE HANDLES, never the buffered capture-and-write_bytes path."""

    def __init__(self, stdout: bytes = b"") -> None:
        self.run_calls: list[list[str]] = []
        self.stream_calls: list[dict[str, object]] = []
        self._stdout = stdout

    def run(self, args: list[str], *, stdin: bytes | None = None, check: bool = True) -> RunResult:
        self.run_calls.append(args)
        return RunResult(returncode=0, stdout=self._stdout, stderr=b"")

    def run_stream(
        self,
        args: list[str],
        *,
        stdin_file: IO[bytes] | None = None,
        stdout_file: IO[bytes] | None = None,
        check: bool = True,
    ) -> RunResult:
        self.stream_calls.append(
            {
                "args": args,
                "stdin_name": getattr(stdin_file, "name", None),
                "stdin_is_file": stdin_file is not None and hasattr(stdin_file, "read"),
                "stdout_name": getattr(stdout_file, "name", None),
                "stdout_is_file": stdout_file is not None and hasattr(stdout_file, "write"),
                "check": check,
            }
        )
        return RunResult(returncode=0, stdout=b"", stderr=b"")


class _SequencedRunner(_StreamRecordingRunner):
    def __init__(self, responses: list[bytes]) -> None:
        super().__init__()
        self._responses = iter(responses)

    def run(self, args: list[str], *, stdin: bytes | None = None, check: bool = True) -> RunResult:
        self.run_calls.append(args)
        return RunResult(returncode=0, stdout=next(self._responses), stderr=b"")


def test_dump_database_streams_stdout_to_file_handle(tmp_path: Path) -> None:
    runner = _StreamRecordingRunner()
    pg = Postgres(runner, container="ne-drill-pg", user="ne")
    dest = tmp_path / "neutron.dump"

    pg.dump_database("neutron", dest)

    call = runner.stream_calls[-1]
    assert call["stdout_name"] == str(dest)
    assert call["stdout_is_file"] is True
    assert call["args"] == ["exec", "ne-drill-pg", "pg_dump", "-U", "ne", "-Fc", "neutron"]
    # The buffered run() path is NEVER used for a dump (revert-tripwire).
    assert runner.run_calls == []


def test_dump_globals_streams_stdout_to_file_handle(tmp_path: Path) -> None:
    runner = _StreamRecordingRunner()
    pg = Postgres(runner, container="ne-drill-pg", user="ne")
    dest = tmp_path / "globals.sql"

    pg.dump_globals(dest)

    call = runner.stream_calls[-1]
    assert call["stdout_name"] == str(dest)
    assert call["stdout_is_file"] is True
    assert call["args"] == ["exec", "ne-drill-pg", "pg_dumpall", "-U", "ne", "--globals-only"]
    assert runner.run_calls == []


def test_restore_database_streams_stdin_from_file(tmp_path: Path) -> None:
    runner = _StreamRecordingRunner()
    pg = Postgres(runner, container="ne-drill-pg", user="ne")
    src = tmp_path / "neutron.dump"
    src.write_bytes(b"fake -Fc dump")

    pg.restore_database("neutron", src)

    # DROP then CREATE precede the streamed pg_restore.
    assert 'DROP DATABASE IF EXISTS "neutron" WITH (FORCE)' in runner.run_calls[0]
    assert 'CREATE DATABASE "neutron"' in runner.run_calls[1]
    call = runner.stream_calls[-1]
    assert call["stdin_name"] == str(src)
    assert call["stdin_is_file"] is True
    assert call["args"] == [
        "exec",
        "-i",
        "ne-drill-pg",
        "pg_restore",
        "-U",
        "ne",
        "-d",
        "neutron",
        "--no-owner",
        "--exit-on-error",
    ]


def test_non_age_restore_is_an_exact_reconciliation_noop(tmp_path: Path) -> None:
    runner = _SequencedRunner([b"", b"", b""])
    pg = Postgres(runner, container="ne-drill-pg", user="ne")
    src = tmp_path / "neutron.dump"
    src.write_bytes(b"fake -Fc dump")

    pg.restore_database("neutron", src)

    assert len(runner.run_calls) == 3
    assert runner.run_calls[-1] == [
        "exec",
        "ne-drill-pg",
        "psql",
        "-U",
        "ne",
        "-d",
        "neutron",
        "-At",
        "-c",
        "SELECT extversion FROM pg_extension WHERE extname = 'age' ORDER BY extversion",
    ]


def test_age_170_restore_reconciles_then_verifies_from_a_fresh_backend(tmp_path: Path) -> None:
    runner = _SequencedRunner([b"", b"", b"1.7.0\n", b"", b""])
    pg = Postgres(runner, container="ne-drill-pg", user="ne")
    src = tmp_path / "neutron.dump"
    src.write_bytes(b"fake -Fc dump")

    pg.restore_database("neutron", src)

    reconciliation = runner.run_calls[-2]
    assert reconciliation[:-1] == [
        "exec",
        "ne-drill-pg",
        "psql",
        "-v",
        "ON_ERROR_STOP=1",
        "-U",
        "ne",
        "-d",
        "neutron",
        "-c",
    ]
    assert reconciliation[-1] == _AGE_170_RECONCILIATION_SQL
    postcommit = runner.run_calls[-1]
    assert postcommit[:-1] == reconciliation[:-1]
    assert postcommit[-1] == _AGE_170_POSTCOMMIT_SQL


def test_foreign_age_version_refuses_before_catalog_write(tmp_path: Path) -> None:
    runner = _SequencedRunner([b"", b"", b"1.6.0\n"])
    pg = Postgres(runner, container="ne-drill-pg", user="ne")
    src = tmp_path / "neutron.dump"
    src.write_bytes(b"fake -Fc dump")

    with pytest.raises(NeBackupError, match="unsupported Apache AGE catalog version"):
        pg.restore_database("neutron", src)

    assert len(runner.run_calls) == 3


def test_age_reconciliation_guards_closed_shape_and_rolls_back_on_error() -> None:
    sql = _AGE_170_RECONCILIATION_SQL
    assert sql.count("BEGIN TRANSACTION ISOLATION LEVEL SERIALIZABLE;") == 1
    assert sql.count("COMMIT;") == 1
    assert "SET LOCAL lock_timeout = '30s'" in sql
    assert "SET LOCAL statement_timeout = '120s'" in sql
    assert "LOCK TABLE ag_catalog.ag_graph IN ACCESS EXCLUSIVE MODE" in sql
    assert "LOCK TABLE ag_catalog.ag_label IN ACCESS EXCLUSIVE MODE" in sql
    assert "ON_ERROR_STOP" not in sql  # owned by the exact psql argv above
    assert "extversion = '1.7.0'" in sql
    assert "graph mapping is not a closed bijection" in sql
    assert "graph-to-namespace mapping is missing or foreign" in sql
    assert "label foreign-key shape is foreign" in sql
    assert "label mapping is missing, foreign, or unmapped" in sql
    assert "graph is missing its exact reserved label pair" in sql
    assert "WHERE graph.graphid <> graph.namespace::oid" in sql
    assert "IF EXISTS (SELECT 1 FROM ne_age_graph_oid_map) THEN" in sql
    assert "CREATE TEMP TABLE ne_age_graph_snapshot" in sql
    assert "CREATE TEMP TABLE ne_age_label_snapshot" in sql
    assert "target graph OID collides with retained identity" in sql
    assert "DELETE FROM ag_catalog.ag_label" in sql
    assert "DELETE FROM ag_catalog.ag_graph" in sql
    assert "INSERT INTO ag_catalog.ag_graph" in sql
    assert "INSERT INTO ag_catalog.ag_label" in sql
    assert "EXCEPT ALL" in sql
    assert "final graph snapshot verification failed" in sql
    assert "final label snapshot verification failed" in sql
    assert "session_replication_role" not in sql
    assert "DROP CONSTRAINT" not in sql
    assert "ALTER TABLE" not in sql
    assert "ag_catalog.cypher" not in sql
    assert sql.index("DELETE FROM ag_catalog.ag_label") < sql.index(
        "DELETE FROM ag_catalog.ag_graph"
    )
    assert sql.index("INSERT INTO ag_catalog.ag_graph") < sql.index(
        "INSERT INTO ag_catalog.ag_label"
    )
    assert sql.index("final label snapshot verification failed") < sql.index("COMMIT;")


def test_age_postcommit_verification_is_read_only_structural_and_cypher() -> None:
    sql = _AGE_170_POSTCOMMIT_SQL
    assert sql.count("BEGIN TRANSACTION ISOLATION LEVEL SERIALIZABLE READ ONLY;") == 1
    assert sql.count("COMMIT;") == 1
    assert "extversion = '1.7.0'" in sql
    assert "LOCK TABLE ag_catalog.ag_graph IN ACCESS SHARE MODE" in sql
    assert "LOCK TABLE ag_catalog.ag_label IN ACCESS SHARE MODE" in sql
    assert "post-restore graph mapping verification failed" in sql
    assert "post-restore label mapping verification failed" in sql
    assert "post-restore graph is missing its exact reserved label pair" in sql
    assert "ag_catalog.cypher" in sql
    assert "INSERT " not in sql
    assert "UPDATE " not in sql
    assert "DELETE " not in sql


def test_restore_globals_streams_stdin_check_false(tmp_path: Path) -> None:
    runner = _StreamRecordingRunner()
    pg = Postgres(runner, container="ne-drill-pg", user="ne")
    src = tmp_path / "globals.sql"
    src.write_bytes(b"-- roles")

    pg.restore_globals(src)

    call = runner.stream_calls[-1]
    assert call["check"] is False  # already-existing roles tolerated
    assert call["stdin_name"] == str(src)
    assert call["args"] == [
        "exec",
        "-i",
        "ne-drill-pg",
        "psql",
        "-U",
        "ne",
        "-d",
        "postgres",
        "-f",
        "-",
    ]


def test_list_databases_connects_to_the_postgres_maintenance_db() -> None:
    """Regression for the restore-drill FATAL: database "ne" does not exist.

    ``psql`` with no ``-d`` defaults to a database named after the connecting
    user; that DB need not exist (the pilot app DB is e.g. ``neutron``). The
    pg_database query MUST connect to the always-present ``postgres`` maintenance
    DB instead.
    """
    runner = _CapturingRunner(stdout=b"neutron\n")
    pg = Postgres(runner, container="ne-drill-pg", user="ne")

    dbs = pg.list_databases()

    assert dbs == ["neutron"]
    args = runner.calls[0]
    assert "psql" in args
    assert "-d" in args, "list_databases must pass an explicit -d database"
    assert args[args.index("-d") + 1] == "postgres"
