# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Always-on (no live AGE) proof of the ``ag_catalog.cypher()`` literal invocation
form: graph name inlined as a validated identifier literal, query text
dollar-quoted with a collision-safe tag, and the params map left as the SOLE
bind parameter (NE-AGE-CYPHER-LITERALS).

These tests exercise ``_pg._cypher_sql`` / ``_pg._dollar_tag`` / ``_pg.execute_cypher``
directly against fakes — the live server proof lives in
``test_graph_store_conformance_server.py`` (LIVE-PARKED). Every guard here has a
planted RED case per the injection-shaped-change quality bar.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from ne.brain_graph import _pg
from ne.brain_graph.errors import GraphStoreError


def test_cypher_sql_inlines_name_and_dollar_quotes_text() -> None:
    sql = _pg._cypher_sql(("result",), "ne_graph", "RETURN 1 AS result")
    assert "cypher('ne_graph', $ne$RETURN 1 AS result$ne$, %s::agtype)" in sql
    # Bidirectional: interpolating the params blob instead of binding it would
    # drop this to 0; re-binding name/text as placeholders would raise it to 2+.
    assert sql.count("%s") == 1
    assert "'RETURN" not in sql  # text is dollar-quoted, never single-quoted


@pytest.mark.parametrize(
    "bad",
    [
        "g'; DROP TABLE ag_graph; --",
        'g"n',
        "g name",
        "g\nname",
        # TRAILING newline specifically (Argus P2): Python's `$` also matches
        # immediately BEFORE a trailing newline, so `re.compile(r"...$")` accepted
        # this while the module docstring and README both called the guard strict
        # and fail-closed. The mid-string case above did NOT catch it. Fixed by
        # anchoring with \\A/\\Z; this case is what keeps it fixed.
        "ne_graph\n",
        "g$x",
        "g;x",
        "",
        "ab",
        "a" * 64,
        "1abc",
        "g-h",
    ],
)
def test_graph_name_injection_rejected(bad: str) -> None:
    with pytest.raises(GraphStoreError):
        _pg._cypher_sql(("result",), bad, "RETURN 1 AS result")


@pytest.mark.parametrize("good", ["ne_graph", "ne_t2x_deadbeef", "_g1"])
def test_graph_name_injection_rejected_controls_pass(good: str) -> None:
    # Controls: valid identifiers must NOT raise.
    sql = _pg._cypher_sql(("result",), good, "RETURN 1 AS result")
    assert f"cypher('{good}'," in sql


def test_dollar_tag_extends_until_absent() -> None:
    assert _pg._dollar_tag("RETURN 1") == "ne"

    t1 = "RETURN '$ne$' AS result"
    tag1 = _pg._dollar_tag(t1)
    assert f"${tag1}$" not in t1 + "$"
    assert tag1 != "ne"

    t2 = "has $ne$ and also $nee$ inside"
    assert _pg._dollar_tag(t2) == "neee"

    # Boundary case: text ENDS in "$ne" — the closing delimiter's own leading
    # "$" would otherwise complete "$ne$" prematurely. RED if the "+ \"$\""
    # scan in _dollar_tag is dropped.
    assert _pg._dollar_tag("RETURN 1 AS x$ne") != "ne"


def test_percent_escaped_in_query_text() -> None:
    sql = _pg._cypher_sql(("result",), "ne_graph", "RETURN '85%' AS result")
    assert "85%%" in sql
    assert sql.count("%s") == 1


class _RecordingCursor:
    def __init__(self, sink: dict[str, Any]) -> None:
        self._sink = sink

    async def __aenter__(self) -> _RecordingCursor:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def execute(self, statement: bytes, args: tuple[Any, ...]) -> None:
        self._sink["statement"] = statement
        self._sink["args"] = args

    async def fetchall(self) -> list[tuple[Any, ...]]:
        return []


class _RecordingConn:
    def __init__(self, sink: dict[str, Any]) -> None:
        self._sink = sink

    def cursor(self) -> _RecordingCursor:
        return _RecordingCursor(self._sink)


def test_execute_cypher_binds_exactly_the_params_blob() -> None:
    sink: dict[str, Any] = {}
    conn = cast(Any, _RecordingConn(sink))
    asyncio.run(_pg.execute_cypher(conn, "ne_graph", "RETURN $x AS result", {"x": 1}))
    # Planted RED for any future value-interpolation shortcut: the params blob is
    # the ONLY bind — a one-tuple, never zero (interpolated) or 2+ (name/text
    # re-bound).
    assert sink["args"] == ('{"x": 1}',)
    assert b"$ne$RETURN $x AS result$ne$" in sink["statement"]
