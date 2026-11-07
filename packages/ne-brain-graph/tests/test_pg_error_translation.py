# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The ``_pg`` chokepoint translates psycopg driver faults to GraphStoreError.

Always-on (no live AGE): a fresh AGE graph has no per-label tables until the
first vertex, so ``CREATE INDEX`` raises ``psycopg.errors.UndefinedTable``. That
must surface as :class:`GraphStoreError` (not a raw driver exception) so
``AgeGraphStore._ensure_indexes`` can treat index creation as best-effort and
``ensure_graph()`` does not crash before the first upsert.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import psycopg
import pytest

from ne.brain_graph import _pg
from ne.brain_graph.errors import GraphStoreError


class _RaisingCursor:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def __aenter__(self) -> _RaisingCursor:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def execute(self, *_args: object, **_kwargs: object) -> None:
        raise self._exc

    async def fetchall(self) -> list[tuple[Any, ...]]:  # pragma: no cover - never reached
        return []

    @property
    def description(self) -> None:  # pragma: no cover - never reached
        return None


class _RaisingConn:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def cursor(self) -> _RaisingCursor:
        return _RaisingCursor(self._exc)


def test_execute_sql_translates_undefined_table_to_graph_store_error() -> None:
    conn = cast(Any, _RaisingConn(psycopg.errors.UndefinedTable("relation does not exist")))
    with pytest.raises(GraphStoreError) as excinfo:
        asyncio.run(_pg.execute_sql(conn, "CREATE INDEX foo ON bar (baz)"))
    assert isinstance(excinfo.value.__cause__, psycopg.Error)


def test_execute_cypher_translates_driver_error_to_graph_store_error() -> None:
    # A valid identifier is required here: the graph name is now interpolated as
    # a literal (see _pg.py), guarded BEFORE the driver ever sees a statement, so
    # this must pass that guard to exercise what the test is actually about — a
    # driver-side fault translating to GraphStoreError with a psycopg.Error cause.
    conn = cast(Any, _RaisingConn(psycopg.errors.SyntaxError("bad cypher")))
    with pytest.raises(GraphStoreError) as excinfo:
        asyncio.run(_pg.execute_cypher(conn, "ne_graph", "MATCH (n) RETURN n", {}))
    assert isinstance(excinfo.value.__cause__, psycopg.Error)
