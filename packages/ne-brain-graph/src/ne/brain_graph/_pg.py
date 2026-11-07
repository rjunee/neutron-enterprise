# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The psycopg chokepoint — the ONLY module importing psycopg (C3).

Confines the LGPL psycopg surface to one auditable, swappable file (the
``ne.gateway._litellm`` / ``ne.mcp._sdk`` pattern; enforced by the additive
``.importlinter`` c3-psycopg-chokepoint contract). Everything above talks agtype
via :mod:`ne.brain_graph.age.agtype`, never psycopg.

Session setup runs ``LOAD 'age'`` + the ``ag_catalog`` search path.

**AGE cypher() invocation form (live-verified against apache/age:release_PG17_1.6.0
in NE-AGE-CYPHER-LITERALS).** AGE's parser hook for ``ag_catalog.cypher()`` requires
its first two arguments — the graph name and the cypher query text — as LITERALS
resolved at PARSE time, not bind parameters: a server-side bind on either raises
``a name constant is expected``; client-side single-quote interpolation of the
query text raises ``a dollar-quoted string constant is expected`` (AGE demands a
dollar-quoted string constant, not a single-quoted one). Neither argument is
parameterizable. Accordingly:

* the graph name is validated against :data:`_GRAPH_NAME_RE` (a strict identifier
  charset, fail-closed — REJECTED, never sanitized, if it does not match) and
  inlined as a name literal;
* the query text is wrapped in a dollar-quote block whose tag is chosen by
  :func:`_dollar_tag` to be collision-safe against the text's own content, with
  every literal ``%`` escaped to ``%%`` (psycopg treats a bare ``%`` as
  placeholder syntax whenever any bind parameters are passed);
* the params map remains the **sole** bind parameter (the third, ``%s::agtype``,
  argument) — every value and label list still travels via the parameterized
  blob, canonical JSON, NEVER interpolated into the cypher text.

There is no ClientCursor fallback: AGE rejects a client-interpolated third
argument outright (``third argument of cypher function must be a parameter``), so
a driver-side-quoting path can never execute against a real server and is not
offered here.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

import psycopg

from ne.brain_graph.errors import GraphStoreError

__all__ = ["connect", "execute_cypher", "execute_sql"]

# Column identifiers are internal builder constants (node / path_nodes /
# path_rels), never caller input — safe to compose into the AS clause.
_ALLOWED_RESULT_COLUMNS = frozenset({"node", "path_nodes", "path_rels", "result", "probe"})

#: Strict AGE graph-name identifier: letter/underscore start, alnum/underscore
#: body, total length 3-63 (AGE rejects names shorter than 3 chars). A graph name
#: is interpolated as a name literal (not parameterizable — see module docstring),
#: so anything outside this charset is REJECTED, never sanitized-by-stripping
#: (injection guard, fail-closed).
#: NOTE the ``\\Z`` anchor, not ``$``: Python's ``$`` also matches immediately
#: BEFORE a trailing newline, so ``re.compile(r"...$").match("ne_graph\\n")``
#: SUCCEEDS. That admitted a trailing newline through a guard this module's
#: docstring and the package README both call strict and fail-closed — the
#: claim was false as written (Argus P2). Unexploitable on its own (a lone
#: trailing newline cannot terminate the single-quoted literal, so the worst
#: case is a wrong-graph lookup error), but a guard whose stated contract
#: does not match its behaviour is exactly the defect class this repo keeps
#: shipping, so it is fixed rather than documented around.
_GRAPH_NAME_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]{2,62}\Z")


def _dollar_tag(query_text: str) -> str:
    """Pick a dollar-quote tag that provably cannot appear in ``query_text``.

    Starts at ``ne`` and extends (``nee``, ``neee``, ...) until
    ``f"${tag}$"`` is absent from ``query_text``. The scan runs over
    ``query_text + "$"`` — NOT the bare text — because text ENDING in ``$ne``
    immediately followed by the dollar-quote block's own closing delimiter's
    first ``$`` would otherwise form ``$ne$`` and prematurely terminate the
    quote; appending a trailing ``$`` to the scanned text reproduces that
    boundary so it is caught here rather than live, at the server.
    """
    tag = "ne"
    haystack = query_text + "$"
    while f"${tag}$" in haystack:
        tag += "e"
    return tag


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
    """Run a plain SQL statement (DDL/index management); return any rows.

    Driver faults (e.g. ``UndefinedTable`` when a label table has not been
    materialised yet) are translated to :class:`GraphStoreError` so no psycopg
    exception type escapes the chokepoint and callers can catch DDL faults on the
    one domain error (errors.py: "Store/DDL faults are GraphStoreError").
    """
    try:
        async with conn.cursor() as cur:
            await cur.execute(statement.encode(), params)  # type: ignore[arg-type]
            if cur.description is None:
                return []
            return await cur.fetchall()
    except psycopg.Error as exc:
        raise GraphStoreError(str(exc)) from exc


def _cypher_sql(result_columns: Sequence[str], graph_name: str, query_text: str) -> str:
    """Build the ``ag_catalog.cypher()`` SELECT with graph name + text INLINED.

    Both are parse-time literals AGE requires (see module docstring); the params
    map is left as the sole ``%s::agtype`` bind. The graph name is validated
    against :data:`_GRAPH_NAME_RE` and REJECTED (never sanitized) if it does not
    match — an injection guard, since this value is now interpolated text rather
    than a bind parameter. The query text is dollar-quoted with a collision-safe
    tag (:func:`_dollar_tag`) and every literal ``%`` is escaped to ``%%`` so
    psycopg's placeholder scanner does not misparse it once params are bound.
    """
    if not _GRAPH_NAME_RE.match(graph_name):
        raise GraphStoreError(
            f"unsafe AGE graph name {graph_name!r}: not a valid identifier literal "
            "(injection guard)"
        )
    for col in result_columns:
        if col not in _ALLOWED_RESULT_COLUMNS:
            raise ValueError(f"disallowed AGE result column {col!r}")
    cols = ", ".join(f"{col} agtype" for col in result_columns)
    tag = _dollar_tag(query_text)
    body = query_text.replace("%", "%%")
    return (
        f"SELECT * FROM ag_catalog.cypher('{graph_name}', "
        f"${tag}${body}${tag}$, %s::agtype) AS ({cols})"
    )


async def execute_cypher(
    conn: psycopg.AsyncConnection[Any],
    graph_name: str,
    query_text: str,
    params: Mapping[str, Any] | None = None,
    *,
    result_columns: Sequence[str] = ("result",),
) -> list[tuple[str, ...]]:
    """Execute openCypher via AGE; return the raw agtype text of each result cell.

    The label list and every value travel via the parameterized ``::agtype``
    params-map argument — the SOLE bind parameter — never interpolated into the
    cypher text. The graph name and query text are inlined as parse-time literals
    (AGE requires this; see module docstring), guarded by :func:`_cypher_sql`.
    """
    agtype_params = json.dumps(dict(params or {}), sort_keys=True, default=str)
    args = (agtype_params,)
    statement = _cypher_sql(result_columns, graph_name, query_text).encode()
    try:
        async with conn.cursor() as cur:
            await cur.execute(statement, args)  # type: ignore[arg-type]
            rows = await cur.fetchall()
    except psycopg.Error as exc:
        raise GraphStoreError(str(exc)) from exc
    return [tuple(row) for row in rows]
