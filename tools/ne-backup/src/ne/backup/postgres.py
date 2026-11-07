# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""DB-native Postgres backup/restore via ``docker exec`` (MVCC-consistent, online).

No cold volume tar and no password handled: the in-container unix socket gives
trust auth (C12). Database names are validated against the SQL-identifier
allowlist before any name is interpolated into a ``DROP``/``CREATE`` statement.

Dumps and restores STREAM through open file handles (``Runner.run_stream``): the
pg_dump/pg_dumpall stdout is redirected straight to the destination file and the
pg_restore/psql stdin is read straight from the source file, so memory stays
O(pipe buffer) instead of buffering the whole (multi-GB) dump in RAM.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import IO

from ne.backup.errors import NeBackupError
from ne.backup.runner import Runner, RunResult

_SQL_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

_LIST_DBS_SQL = (
    "SELECT datname FROM pg_database "
    "WHERE NOT datistemplate AND datname <> 'postgres' ORDER BY datname"
)

_AGE_EXTENSION_VERSION_SQL = (
    "SELECT extversion FROM pg_extension WHERE extname = 'age' ORDER BY extversion"
)

# Apache AGE 1.7 stores graph and label ownership in extension catalogs. pg_dump
# serializes the regnamespace/regclass columns by name, but the plain OID graphid
# and label.graph columns retain the source database's schema OID. Restoring into
# a newly created database therefore needs one closed, transactional OID rebind.
# The script locks both catalogs, snapshots and validates the complete graph/label
# shape, then deletes and reinserts only mismatched rows in constraint-safe order.
# Constraints remain enabled throughout. A second psql invocation verifies the
# committed catalogs and opens every graph through Cypher from a fresh backend.
# Any transactional exception closes the psql session and rolls back.
_AGE_170_RECONCILIATION_SQL = r"""
BEGIN TRANSACTION ISOLATION LEVEL SERIALIZABLE;
SET LOCAL lock_timeout = '30s';
SET LOCAL statement_timeout = '120s';
SET LOCAL idle_in_transaction_session_timeout = '60s';
LOAD 'age';
LOCK TABLE ag_catalog.ag_graph IN ACCESS EXCLUSIVE MODE;
LOCK TABLE ag_catalog.ag_label IN ACCESS EXCLUSIVE MODE;
DO $ne_age_reconcile$
DECLARE
    affected_count bigint;
    expected_count bigint;
    invalid_count bigint;
BEGIN
    IF (SELECT count(*) FROM pg_extension WHERE extname = 'age' AND extversion = '1.7.0') <> 1
    THEN
        RAISE EXCEPTION 'AGE restore reconciliation requires exactly AGE 1.7.0';
    END IF;
    IF to_regclass('ag_catalog.ag_graph') IS NULL
       OR to_regclass('ag_catalog.ag_label') IS NULL
    THEN
        RAISE EXCEPTION 'AGE restore reconciliation catalogs are missing';
    END IF;

    SELECT count(*) INTO invalid_count
    FROM ag_catalog.ag_graph AS graph
    LEFT JOIN pg_namespace AS namespace
      ON namespace.oid = graph.namespace::oid
     AND namespace.nspname::text = graph.name::text
    WHERE namespace.oid IS NULL OR graph.graphid = 0::oid;
    IF invalid_count <> 0 THEN
        RAISE EXCEPTION 'AGE restore graph-to-namespace mapping is missing or foreign';
    END IF;

    SELECT count(*) INTO invalid_count
    FROM (
        SELECT name::text FROM ag_catalog.ag_graph GROUP BY name::text HAVING count(*) <> 1
        UNION ALL
        SELECT graphid::text FROM ag_catalog.ag_graph GROUP BY graphid HAVING count(*) <> 1
        UNION ALL
        SELECT namespace::oid::text
        FROM ag_catalog.ag_graph
        GROUP BY namespace::oid
        HAVING count(*) <> 1
    ) AS duplicate_graph_identity;
    IF invalid_count <> 0 THEN
        RAISE EXCEPTION 'AGE restore graph mapping is not a closed bijection';
    END IF;

    SELECT count(*) INTO invalid_count
    FROM pg_constraint
    WHERE conrelid = 'ag_catalog.ag_label'::regclass
      AND conname = 'fk_graph_oid'
      AND pg_get_constraintdef(oid, false) =
          'FOREIGN KEY (graph) REFERENCES ag_catalog.ag_graph(graphid)';
    IF invalid_count <> 1 THEN
        RAISE EXCEPTION 'AGE restore label foreign-key shape is foreign';
    END IF;

    SELECT count(*) INTO invalid_count
    FROM ag_catalog.ag_label AS label
    LEFT JOIN ag_catalog.ag_graph AS graph ON graph.graphid = label.graph
    LEFT JOIN pg_class AS relation ON relation.oid = label.relation::oid
    LEFT JOIN pg_class AS sequence
      ON sequence.relnamespace = graph.namespace::oid
     AND sequence.relname::text = label.seq_name::text
     AND sequence.relkind = 'S'
    WHERE graph.graphid IS NULL
       OR relation.oid IS NULL
       OR relation.relnamespace <> graph.namespace::oid
       OR relation.relname::text <> label.name::text
       OR sequence.oid IS NULL;
    IF invalid_count <> 0 THEN
        RAISE EXCEPTION 'AGE restore label mapping is missing, foreign, or unmapped';
    END IF;

    SELECT count(*) INTO invalid_count
    FROM ag_catalog.ag_graph AS graph
    WHERE (
        SELECT count(*)
        FROM ag_catalog.ag_label AS label
        WHERE label.graph = graph.graphid
          AND (
              (label.name::text = '_ag_label_vertex'
               AND label.id::bigint = 1
               AND label.kind::text = 'v')
              OR
              (label.name::text = '_ag_label_edge'
               AND label.id::bigint = 2
               AND label.kind::text = 'e')
          )
    ) <> 2;
    IF invalid_count <> 0 THEN
        RAISE EXCEPTION 'AGE restore graph is missing its exact reserved label pair';
    END IF;

    CREATE TEMP TABLE ne_age_graph_snapshot ON COMMIT DROP AS
    SELECT graphid, name, namespace
    FROM ag_catalog.ag_graph;
    CREATE TEMP TABLE ne_age_label_snapshot ON COMMIT DROP AS
    SELECT name, graph, id, kind, relation, seq_name
    FROM ag_catalog.ag_label;
    CREATE TEMP TABLE ne_age_graph_oid_map (
        old_oid oid PRIMARY KEY,
        new_oid oid UNIQUE NOT NULL
    ) ON COMMIT DROP;
    INSERT INTO ne_age_graph_oid_map (old_oid, new_oid)
    SELECT graph.graphid, graph.namespace::oid
    FROM ne_age_graph_snapshot AS graph
    WHERE graph.graphid <> graph.namespace::oid;

    SELECT count(*) INTO invalid_count
    FROM ne_age_graph_oid_map AS mapping
    JOIN ne_age_graph_snapshot AS graph ON graph.graphid = mapping.new_oid
    LEFT JOIN ne_age_graph_oid_map AS displaced ON displaced.old_oid = graph.graphid
    WHERE displaced.old_oid IS NULL;
    IF invalid_count <> 0 THEN
        RAISE EXCEPTION 'AGE restore target graph OID collides with retained identity';
    END IF;

    IF EXISTS (SELECT 1 FROM ne_age_graph_oid_map) THEN
        SELECT count(*) INTO expected_count
        FROM ne_age_label_snapshot AS label
        JOIN ne_age_graph_oid_map AS mapping ON mapping.old_oid = label.graph;
        DELETE FROM ag_catalog.ag_label AS label
        USING ne_age_graph_oid_map AS mapping
        WHERE label.graph = mapping.old_oid;
        GET DIAGNOSTICS affected_count = ROW_COUNT;
        IF affected_count <> expected_count THEN
            RAISE EXCEPTION 'AGE restore label delete count changed under lock';
        END IF;

        SELECT count(*) INTO expected_count FROM ne_age_graph_oid_map;
        DELETE FROM ag_catalog.ag_graph AS graph
        USING ne_age_graph_oid_map AS mapping
        WHERE graph.graphid = mapping.old_oid;
        GET DIAGNOSTICS affected_count = ROW_COUNT;
        IF affected_count <> expected_count THEN
            RAISE EXCEPTION 'AGE restore graph delete count changed under lock';
        END IF;

        INSERT INTO ag_catalog.ag_graph (graphid, name, namespace)
        SELECT mapping.new_oid, snapshot.name, snapshot.namespace
        FROM ne_age_graph_snapshot AS snapshot
        JOIN ne_age_graph_oid_map AS mapping ON mapping.old_oid = snapshot.graphid
        ORDER BY snapshot.name;
        GET DIAGNOSTICS affected_count = ROW_COUNT;
        IF affected_count <> expected_count THEN
            RAISE EXCEPTION 'AGE restore graph insert count changed under lock';
        END IF;

        SELECT count(*) INTO expected_count
        FROM ne_age_label_snapshot AS label
        JOIN ne_age_graph_oid_map AS mapping ON mapping.old_oid = label.graph;
        INSERT INTO ag_catalog.ag_label (name, graph, id, kind, relation, seq_name)
        SELECT
            label.name,
            mapping.new_oid,
            label.id,
            label.kind,
            label.relation,
            label.seq_name
        FROM ne_age_label_snapshot AS label
        JOIN ne_age_graph_oid_map AS mapping ON mapping.old_oid = label.graph
        ORDER BY mapping.new_oid, label.id;
        GET DIAGNOSTICS affected_count = ROW_COUNT;
        IF affected_count <> expected_count THEN
            RAISE EXCEPTION 'AGE restore label insert count changed under lock';
        END IF;
    END IF;

    SELECT count(*) INTO invalid_count FROM (
        (
            SELECT graphid, name, namespace FROM ag_catalog.ag_graph
            EXCEPT ALL
            SELECT
                COALESCE(mapping.new_oid, snapshot.graphid),
                snapshot.name,
                snapshot.namespace
            FROM ne_age_graph_snapshot AS snapshot
            LEFT JOIN ne_age_graph_oid_map AS mapping
              ON mapping.old_oid = snapshot.graphid
        )
        UNION ALL
        (
            SELECT
                COALESCE(mapping.new_oid, snapshot.graphid),
                snapshot.name,
                snapshot.namespace
            FROM ne_age_graph_snapshot AS snapshot
            LEFT JOIN ne_age_graph_oid_map AS mapping
              ON mapping.old_oid = snapshot.graphid
            EXCEPT ALL
            SELECT graphid, name, namespace FROM ag_catalog.ag_graph
        )
    ) AS graph_difference;
    IF invalid_count <> 0 THEN
        RAISE EXCEPTION 'AGE restore final graph snapshot verification failed';
    END IF;

    SELECT count(*) INTO invalid_count FROM (
        (
            SELECT name, graph, id, kind, relation, seq_name FROM ag_catalog.ag_label
            EXCEPT ALL
            SELECT
                snapshot.name,
                COALESCE(mapping.new_oid, snapshot.graph),
                snapshot.id,
                snapshot.kind,
                snapshot.relation,
                snapshot.seq_name
            FROM ne_age_label_snapshot AS snapshot
            LEFT JOIN ne_age_graph_oid_map AS mapping ON mapping.old_oid = snapshot.graph
        )
        UNION ALL
        (
            SELECT
                snapshot.name,
                COALESCE(mapping.new_oid, snapshot.graph),
                snapshot.id,
                snapshot.kind,
                snapshot.relation,
                snapshot.seq_name
            FROM ne_age_label_snapshot AS snapshot
            LEFT JOIN ne_age_graph_oid_map AS mapping ON mapping.old_oid = snapshot.graph
            EXCEPT ALL
            SELECT name, graph, id, kind, relation, seq_name FROM ag_catalog.ag_label
        )
    ) AS label_difference;
    IF invalid_count <> 0 THEN
        RAISE EXCEPTION 'AGE restore final label snapshot verification failed';
    END IF;
END
$ne_age_reconcile$;
COMMIT;
"""

_AGE_170_POSTCOMMIT_SQL = r"""
BEGIN TRANSACTION ISOLATION LEVEL SERIALIZABLE READ ONLY;
SET LOCAL lock_timeout = '30s';
SET LOCAL statement_timeout = '120s';
SET LOCAL idle_in_transaction_session_timeout = '60s';
LOAD 'age';
LOCK TABLE ag_catalog.ag_graph IN ACCESS SHARE MODE;
LOCK TABLE ag_catalog.ag_label IN ACCESS SHARE MODE;
DO $ne_age_postcommit$
DECLARE
    graph_record record;
    invalid_count bigint;
BEGIN
    IF (SELECT count(*) FROM pg_extension WHERE extname = 'age' AND extversion = '1.7.0') <> 1
    THEN
        RAISE EXCEPTION 'AGE post-restore verification requires exactly AGE 1.7.0';
    END IF;
    IF to_regclass('ag_catalog.ag_graph') IS NULL
       OR to_regclass('ag_catalog.ag_label') IS NULL
    THEN
        RAISE EXCEPTION 'AGE post-restore verification catalogs are missing';
    END IF;

    SELECT count(*) INTO invalid_count
    FROM ag_catalog.ag_graph AS graph
    LEFT JOIN pg_namespace AS namespace
      ON namespace.oid = graph.namespace::oid
     AND namespace.nspname::text = graph.name::text
    WHERE namespace.oid IS NULL OR graph.graphid <> graph.namespace::oid;
    IF invalid_count <> 0 THEN
        RAISE EXCEPTION 'AGE post-restore graph mapping verification failed';
    END IF;

    SELECT count(*) INTO invalid_count
    FROM ag_catalog.ag_label AS label
    LEFT JOIN ag_catalog.ag_graph AS graph ON graph.graphid = label.graph
    LEFT JOIN pg_class AS relation ON relation.oid = label.relation::oid
    LEFT JOIN pg_class AS sequence
      ON sequence.relnamespace = graph.namespace::oid
     AND sequence.relname::text = label.seq_name::text
     AND sequence.relkind = 'S'
    WHERE graph.graphid IS NULL
       OR relation.oid IS NULL
       OR relation.relnamespace <> graph.namespace::oid
       OR relation.relname::text <> label.name::text
       OR sequence.oid IS NULL;
    IF invalid_count <> 0 THEN
        RAISE EXCEPTION 'AGE post-restore label mapping verification failed';
    END IF;

    SELECT count(*) INTO invalid_count
    FROM ag_catalog.ag_graph AS graph
    WHERE (
        SELECT count(*)
        FROM ag_catalog.ag_label AS label
        WHERE label.graph = graph.graphid
          AND (
              (label.name::text = '_ag_label_vertex'
               AND label.id::bigint = 1
               AND label.kind::text = 'v')
              OR
              (label.name::text = '_ag_label_edge'
               AND label.id::bigint = 2
               AND label.kind::text = 'e')
          )
    ) <> 2;
    IF invalid_count <> 0 THEN
        RAISE EXCEPTION 'AGE post-restore graph is missing its exact reserved label pair';
    END IF;

    PERFORM set_config('search_path', 'ag_catalog, public', true);
    FOR graph_record IN SELECT name::text AS name FROM ag_catalog.ag_graph ORDER BY name
    LOOP
        EXECUTE format(
            'SELECT count(*) FROM ag_catalog.cypher(%L, '
            '$cypher$ MATCH (n) RETURN count(n) $cypher$) '
            'AS (vertex_count ag_catalog.agtype)',
            graph_record.name
        ) INTO invalid_count;
        IF invalid_count <> 1 THEN
            RAISE EXCEPTION 'AGE post-restore Cypher continuity failed for graph %',
                graph_record.name;
        END IF;
    END LOOP;
END
$ne_age_postcommit$;
COMMIT;
"""


def assert_sql_identifier(name: str) -> str:
    if not _SQL_IDENTIFIER.match(name):
        raise NeBackupError(f"refusing unsafe database identifier: {name!r}")
    return name


class Postgres:
    def __init__(self, runner: Runner, container: str, user: str = "ne") -> None:
        self._runner = runner
        self._container = container
        self._user = user

    def _exec(self, args: list[str], *, stdin: bytes | None = None, check: bool = True):
        flags = ["exec"]
        if stdin is not None:
            flags.append("-i")
        return self._runner.run([*flags, self._container, *args], stdin=stdin, check=check)

    def _exec_stream(
        self,
        args: list[str],
        *,
        stdin_file: IO[bytes] | None = None,
        stdout_file: IO[bytes] | None = None,
        check: bool = True,
    ) -> RunResult:
        flags = ["exec"]
        if stdin_file is not None:
            flags.append("-i")
        return self._runner.run_stream(
            [*flags, self._container, *args],
            stdin_file=stdin_file,
            stdout_file=stdout_file,
            check=check,
        )

    def list_databases(self) -> list[str]:
        # Connect to the always-present "postgres" maintenance DB (like restore
        # does) — psql with no -d defaults to a DB named after the user, which
        # need not exist (drill: FATAL: database "ne" does not exist).
        res = self._exec(["psql", "-U", self._user, "-d", "postgres", "-At", "-c", _LIST_DBS_SQL])
        return [line for line in res.stdout.decode("utf-8").splitlines() if line]

    def dump_globals(self, dest: Path) -> None:
        with dest.open("wb") as out:
            self._exec_stream(["pg_dumpall", "-U", self._user, "--globals-only"], stdout_file=out)

    def dump_database(self, db: str, dest: Path) -> None:
        assert_sql_identifier(db)
        with dest.open("wb") as out:
            self._exec_stream(["pg_dump", "-U", self._user, "-Fc", db], stdout_file=out)

    def restore_globals(self, src: Path) -> None:
        # Globals (roles/tablespaces) may already exist — allow "already exists".
        with src.open("rb") as inp:
            self._exec_stream(
                ["psql", "-U", self._user, "-d", "postgres", "-f", "-"],
                stdin_file=inp,
                check=False,
            )

    def restore_database(self, db: str, src: Path) -> None:
        assert_sql_identifier(db)
        self._exec(
            [
                "psql",
                "-U",
                self._user,
                "-d",
                "postgres",
                "-c",
                f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)',
            ]
        )
        self._exec(["psql", "-U", self._user, "-d", "postgres", "-c", f'CREATE DATABASE "{db}"'])
        with src.open("rb") as inp:
            self._exec_stream(
                ["pg_restore", "-U", self._user, "-d", db, "--no-owner", "--exit-on-error"],
                stdin_file=inp,
            )
        self._reconcile_age_graph_oids(db)

    def _reconcile_age_graph_oids(self, db: str) -> None:
        version = self.scalar(db, _AGE_EXTENSION_VERSION_SQL)
        if not version:
            return
        if version != "1.7.0":
            raise NeBackupError(
                f"refusing unsupported Apache AGE catalog version during restore: {version!r}"
            )
        self._exec(
            [
                "psql",
                "-v",
                "ON_ERROR_STOP=1",
                "-U",
                self._user,
                "-d",
                db,
                "-c",
                _AGE_170_RECONCILIATION_SQL,
            ]
        )
        # A separate docker-exec launches a fresh psql backend. AGE does not
        # publish a cache-invalidation contract for direct catalog repairs, so
        # only that fresh backend may attest structural and Cypher continuity.
        self._exec(
            [
                "psql",
                "-v",
                "ON_ERROR_STOP=1",
                "-U",
                self._user,
                "-d",
                db,
                "-c",
                _AGE_170_POSTCOMMIT_SQL,
            ]
        )

    def scalar(self, db: str, sql: str) -> str:
        """Run a scalar query and return the trimmed single value (verify/drill)."""
        assert_sql_identifier(db)
        res = self._exec(["psql", "-U", self._user, "-d", db, "-At", "-c", sql])
        return res.stdout.decode("utf-8").strip()

    def user_table_count(self, db: str) -> int:
        value = self.scalar(
            db,
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema NOT IN ('pg_catalog', 'information_schema')",
        )
        return int(value or "0")
