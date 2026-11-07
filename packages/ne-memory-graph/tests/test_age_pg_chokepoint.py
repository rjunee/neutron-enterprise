# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Always-on pins for the ``graphiti_age._pg`` psycopg chokepoint (fake connection).

Proves the composed SQL is the live-verified AGE form — graph name + cypher text
INLINED (graph as a quoted literal, text inside the ``$ne083c$`` dollar-quote), with
ONLY the params map as the ``%s::agtype`` bind parameter — that the AS-clause labels
are generated ``col{i}`` names (never caller aliases, so a dynamic RETURN alias is
never rejected), that params serialize to canonical JSON (``sort_keys``), that an
unsafe graph name / a reserved-tag collision raises, and that a ``psycopg.Error`` is
translated to ``AgeQueryError`` — all without a live Postgres. Uses ``asyncio.run`` in
sync tests (the NE-081 ``test_pg_error_translation.py`` pattern — the repo does not
configure pytest-asyncio).
"""

from __future__ import annotations

import asyncio
import json

import psycopg
import pytest

from ._sidecar_loader import load_sidecar_module

_pg = load_sidecar_module("graphiti_age/_pg.py", "_ne083c_pg")


class _FakeCursor:
    def __init__(self, rows: list[tuple[object, ...]], raise_error: bool = False) -> None:
        self._rows = rows
        self._raise = raise_error
        self.executed: list[tuple[bytes, tuple[object, ...]]] = []
        self.description: object = object()

    async def __aenter__(self) -> _FakeCursor:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def execute(self, statement: bytes, args: tuple[object, ...] | None = None) -> None:
        if self._raise:
            raise psycopg.OperationalError("boom")
        self.executed.append((statement, args or ()))

    async def fetchall(self) -> list[tuple[object, ...]]:
        return self._rows


class _FakeConn:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _FakeCursor:
        return self._cursor


def test_cypher_sql_is_inlined_agtype_form() -> None:
    cur = _FakeCursor(rows=[("42",)])
    rows = asyncio.run(
        _pg.execute_cypher(
            _FakeConn(cur),
            "ne_memory",
            "RETURN 1 AS result",
            {"b": 2, "a": 1},
            column_count=1,
        )
    )
    assert rows == [("42",)]
    statement, args = cur.executed[0]
    # graph name + cypher text inlined; ONLY the params map is a bind placeholder.
    assert statement == (
        b"SELECT * FROM ag_catalog.cypher('ne_memory', "
        b"$ne083c$RETURN 1 AS result$ne083c$, %s::agtype) AS (col0 agtype)"
    )
    assert args == (json.dumps({"a": 1, "b": 2}, sort_keys=True),)


def test_multiple_columns_are_generated_positional_names() -> None:
    cur = _FakeCursor(rows=[])
    asyncio.run(_pg.execute_cypher(_FakeConn(cur), "ne_memory", "RETURN a, b", {}, column_count=2))
    statement, _ = cur.executed[0]
    assert statement == (
        b"SELECT * FROM ag_catalog.cypher('ne_memory', $ne083c$RETURN a, b$ne083c$, "
        b"%s::agtype) AS (col0 agtype, col1 agtype)"
    )


def test_dynamic_return_aliases_never_reach_sql() -> None:
    # A RETURN alias like 'ok' / 'name' / 'count' (or a hostile string) used to be
    # rejected by an allowlist; now it is irrelevant — only col{i} appears in the SQL.
    for text in ("RETURN 1 AS ok", "RETURN e.name AS name", "RETURN count(*) AS count"):
        sql = _pg._cypher_sql("ne_memory", text, 1)
        assert "col0 agtype" in sql
        assert "AS (col0 agtype)" in sql


def test_unsafe_graph_name_rejected() -> None:
    with pytest.raises(_pg.AgeQueryError, match="unsafe AGE graph name"):
        _pg._cypher_sql("ag_catalog; DROP", "RETURN 1", 1)


def test_reserved_dollar_tag_collision_rejected() -> None:
    with pytest.raises(_pg.AgeQueryError, match="reserved dollar-quote tag"):
        _pg._cypher_sql("ne_memory", "RETURN $ne083c$ AS x", 1)


def test_psycopg_error_becomes_age_query_error() -> None:
    cur = _FakeCursor(rows=[], raise_error=True)
    with pytest.raises(_pg.AgeQueryError):
        asyncio.run(_pg.execute_cypher(_FakeConn(cur), "ne_memory", "RETURN 1", {}))


def test_execute_sql_error_translated() -> None:
    cur = _FakeCursor(rows=[], raise_error=True)
    with pytest.raises(_pg.AgeQueryError):
        asyncio.run(_pg.execute_sql(_FakeConn(cur), "CREATE TABLE x ()"))
