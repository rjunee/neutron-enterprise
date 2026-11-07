# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
# pyright: reportMissingImports=false, reportMissingModuleSource=false
"""The psycopg chokepoint of the sidecar — the ONLY graphiti_age module importing
psycopg.

Adapted from ``packages/ne-brain-graph/src/ne/brain_graph/_pg.py`` (NE-081);
replicated (not imported) because the sidecar venv cannot import ``ne-brain-graph``.
Session setup runs ``LOAD 'age'`` + the ``ag_catalog`` search path.

AGE ``cypher()`` INVOCATION FORM (live-verified against ``apache/age:release_PG17_1.6.0``
at build time — NE-083c): AGE requires the FIRST argument (graph name) and the SECOND
argument (the openCypher text) to be *inlined literals* — a bound parameter in either
slot raises ``a name constant is expected`` / ``a dollar-quoted string constant is
expected``. Only the THIRD argument (the params map) may be a real bind parameter.
So the graph name is validated (identifier regex) and inlined as a quoted literal, the
cypher text is inlined inside a fixed ``$ne083c$…$ne083c$`` dollar-quote block (the tag
is asserted absent from the text — Graphiti queries use single ``$param`` refs, never
that tag), and the canonical-JSON params travel as the sole ``%s::agtype`` placeholder.
This corrects the NE-081 ``cypher(%s, %s, %s)`` three-bind form, which AGE rejects.

The AS-clause column labels are *generated* positional names (``col0``, ``col1``, …)
derived only from the column COUNT — caller-supplied / Graphiti-derived RETURN aliases
are NEVER echoed into the SQL, so a dynamic alias like ``ok`` / ``name`` / ``count``
cannot be rejected and cannot be an injection vector (they map back to record keys in
the driver, not here).

Every :class:`psycopg.Error` is translated to :class:`AgeQueryError` so no psycopg
exception type escapes the chokepoint (the sidecar cannot import the platform ``ne``
error hierarchy, so the error type is module-local).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

import psycopg

__all__ = [
    "AgeQueryError",
    "connect",
    "execute_cypher",
    "execute_sql",
]

# The graph name is inlined into the SQL as a quoted literal, so it must be a strict
# identifier (never caller free-text). AGE also rejects graph names shorter than 3
# characters ("graph name is invalid"); real graphs are ``ne_memory`` / ``ne_conf_*``.
_GRAPH_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{2,62}$")

# Fixed dollar-quote tag wrapping the inlined cypher text. Graphiti's Neo4j cypher uses
# single ``$param`` references and never this multi-char tag; presence is a hard error.
_DOLLAR_TAG = "$ne083c$"


class AgeQueryError(RuntimeError):
    """A psycopg fault while executing SQL/cypher against AGE (chokepoint boundary)."""


async def connect(dsn: str) -> psycopg.AsyncConnection[Any]:
    """Open an AGE-ready async connection (autocommit, ag_catalog search path)."""
    conn = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
    async with conn.cursor() as cur:
        await cur.execute("LOAD 'age';")
        await cur.execute('SET search_path = ag_catalog, "$user", public;')
    return conn


async def execute_sql(
    conn: psycopg.AsyncConnection[Any],
    statement: str,
    params: Sequence[Any] | None = None,
) -> list[tuple[Any, ...]]:
    """Run a plain SQL statement (DDL / side-table ops); return any rows.

    Driver faults are translated to :class:`AgeQueryError` so a caller can catch DDL
    faults (e.g. ``UndefinedTable`` for a not-yet-materialised label table) on one
    domain error.
    """
    try:
        async with conn.cursor() as cur:
            await cur.execute(statement.encode(), params)  # type: ignore[arg-type]
            if cur.description is None:
                return []
            return await cur.fetchall()
    except psycopg.Error as exc:
        raise AgeQueryError(str(exc)) from exc


def _cypher_sql(graph_name: str, query_text: str, column_count: int) -> str:
    """Compose the AGE ``cypher()`` SELECT with the graph name + text inlined.

    The graph name is validated and inlined as a quoted literal; the cypher text is
    inlined inside the fixed dollar-quote tag; the params map is left as the sole
    ``%s::agtype`` bind placeholder. The AS-clause labels are generated ``col{i}``
    names (never caller aliases). ``column_count`` is floored at 1 (AGE requires at
    least one output column).
    """
    if not _GRAPH_NAME_RE.match(graph_name):
        raise AgeQueryError(f"unsafe AGE graph name {graph_name!r}")
    if _DOLLAR_TAG in query_text:
        raise AgeQueryError("cypher text contains the reserved dollar-quote tag")
    n = max(1, int(column_count))
    cols = ", ".join(f"col{i} agtype" for i in range(n))
    return (
        f"SELECT * FROM ag_catalog.cypher('{graph_name}', "
        f"{_DOLLAR_TAG}{query_text}{_DOLLAR_TAG}, %s::agtype) AS ({cols})"
    )


async def execute_cypher(
    conn: psycopg.AsyncConnection[Any],
    graph_name: str,
    query_text: str,
    params: Mapping[str, Any] | None = None,
    *,
    column_count: int = 1,
    client_side: bool = False,
) -> list[tuple[str, ...]]:
    """Execute openCypher via AGE; return the raw agtype text of each result cell.

    Only the params map is a bind parameter (canonical JSON, ``sort_keys``); the graph
    name and cypher text are inlined per AGE's requirement (see the module docstring).
    ``column_count`` fixes the arity of the generated ``AS (col0 agtype, …)`` clause.
    """
    agtype_params = json.dumps(dict(params or {}), sort_keys=True, default=str)
    statement = _cypher_sql(graph_name, query_text, column_count).encode()
    args = (agtype_params,)
    try:
        if client_side:
            async with psycopg.AsyncClientCursor(conn) as cur:
                await cur.execute(statement, args)  # type: ignore[arg-type]
                rows = await cur.fetchall()
        else:
            async with conn.cursor() as cur:
                await cur.execute(statement, args)  # type: ignore[arg-type]
                rows = await cur.fetchall()
    except psycopg.Error as exc:
        raise AgeQueryError(str(exc)) from exc
    return [tuple(row) for row in rows]
