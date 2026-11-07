# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""LIVE-PARKED: the C5 kit + ensure_graph DDL + predicate-rung probe over real AGE.

Skips at module level with a LIVE-PARKED message when ``NE_AGE_DSN`` is
unreachable (byte-for-byte the ``test_chunk_store_conformance_server.py`` pattern).
Bring up a container with:

    docker run --name ne-age -e POSTGRES_PASSWORD=agens -p 5455:5432 \
        apache/age:release_PG17_1.6.0

then set ``NE_AGE_DSN=postgresql://postgres:agens@localhost:5455/postgres``.
The probe records which R1 predicate rung the live AGE version selected.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

_DSN = os.environ.get("NE_AGE_DSN", "postgresql://postgres:agens@localhost:5455/postgres")


def _age_reachable() -> bool:
    try:
        from ne.brain_graph import _pg
    except Exception:  # pragma: no cover - psycopg import guard
        return False

    async def probe() -> bool:
        try:
            conn = await _pg.connect(_DSN)
        except Exception:
            return False
        await conn.close()
        return True

    try:
        return asyncio.run(probe())
    except Exception:
        return False


if not _age_reachable():
    pytest.skip(
        "LIVE-PARKED: no reachable Apache AGE service at NE_AGE_DSN "
        f"({_DSN}); start apache/age:release_PG17_1.6.0 to run the server conformance suite",
        allow_module_level=True,
    )

from ne.brain_graph import _pg  # noqa: E402
from ne.brain_graph.age.agtype import decode_agtype  # noqa: E402
from ne.brain_graph.age.query import PredicateStyle  # noqa: E402
from ne.brain_graph.age.store import AgeGraphStore  # noqa: E402
from ne.brain_graph.errors import GraphStoreError  # noqa: E402
from ne.brain_graph.read import EntityGraph  # noqa: E402
from ne.brain_graph.testing.kits import GraphStoreContractKit  # noqa: E402
from ne.contracts.types import Principal  # noqa: E402


class TestAgeGraphStoreConformance(GraphStoreContractKit):
    def create_graph_store(self) -> AgeGraphStore:
        return AgeGraphStore(_DSN, graph_name=f"ne_kit_{uuid.uuid4().hex[:8]}")


def test_probe_selects_a_rung() -> None:
    store = AgeGraphStore(_DSN, graph_name=f"ne_probe_{uuid.uuid4().hex[:8]}")

    async def scenario() -> None:
        await store.ensure_graph()
        # STRENGTHENED (NE-AGE-CYPHER-LITERALS): pinned to a real value, not a
        # vacuous not-None. On the pinned apache/age:release_PG17_1.6.0 image,
        # `any(...)` is a syntax error, so the probe falls through to
        # SIZE_COMPREHENSION (live-verified). An image bump that changes which
        # rung the server accepts must update this pin DELIBERATELY.
        assert store.predicate_style is PredicateStyle.SIZE_COMPREHENSION
        await store.close()

    asyncio.run(scenario())


def test_cypher_text_with_naive_tag_and_percent_round_trips() -> None:
    """Planted RED case (live) for a hardcoded ``$ne$`` tag AND for missing
    ``%``→``%%`` escaping: the query text itself contains the naive tag AND a
    literal ``%``. A hardcoded-tag implementation would misparse the dollar
    quote (premature close); a missing-escape implementation would raise a
    psycopg placeholder error."""
    graph_name = f"ne_tagtest_{uuid.uuid4().hex[:8]}"

    async def scenario() -> None:
        conn = await _pg.connect(_DSN)
        try:
            await _pg.execute_sql(conn, "SELECT create_graph(%s)", (graph_name,))
            cells = await _pg.execute_cypher(
                conn,
                graph_name,
                "RETURN '$ne$x$ne$ and 85%' AS result",
                {},
                result_columns=("result",),
            )
            assert len(cells) == 1
            assert decode_agtype(cells[0][0]) == "$ne$x$ne$ and 85%"
        finally:
            await conn.close()

    asyncio.run(scenario())


def test_graph_name_guard_rejects_injection_before_any_sql() -> None:
    """The guard fires in ``_cypher_sql`` before the server sees anything.

    Argus P3: this test was VACUOUS as first written. It asserted only
    ``pytest.raises(GraphStoreError)``, and deleting ``_GRAPH_NAME_RE``
    entirely left it GREEN — because ``bad'name; --`` also trips AGE's own SQL
    parser, and that driver fault is translated into the same bare
    ``GraphStoreError``. So it could not distinguish "our guard fired before
    any SQL was sent" (what this docstring claims) from "the server rejected
    it after the fact" — a guard satisfiable by exactly the change it forbids,
    for the ninth consecutive unit.

    Matching on the guard's OWN message is what makes the claim testable: the
    server's syntax error does not contain "injection guard".
    """
    store = AgeGraphStore(_DSN, graph_name="bad'name; --")

    async def scenario() -> None:
        with pytest.raises(GraphStoreError, match="injection guard"):
            await store.upsert_nodes(
                [
                    {
                        "key": "guard-probe",
                        "entity_class": "Account",
                        "display_name": "Guard Probe",
                        "source_system": "salesforce",
                        "source_ref": "guard:probe",
                        "acl": ("group-a",),
                        "content_hash": "h0",
                        "config_hash": "c0",
                        "schema_version": "1.0.0",
                        "synced_at": "2026-07-09T00:00:00+00:00",
                    }
                ]
            )
        await store.close()

    asyncio.run(scenario())


def test_params_blob_still_parameterized_round_trip() -> None:
    """A quote/semicolon-laden value travels via the bind, never the text."""
    graph_name = f"ne_paramtest_{uuid.uuid4().hex[:8]}"

    async def scenario() -> None:
        conn = await _pg.connect(_DSN)
        try:
            await _pg.execute_sql(conn, "SELECT create_graph(%s)", (graph_name,))
            cells = await _pg.execute_cypher(
                conn,
                graph_name,
                "RETURN $x AS result",
                {"x": "O'Reilly; DROP"},
                result_columns=("result",),
            )
            assert len(cells) == 1
            assert decode_agtype(cells[0][0]) == "O'Reilly; DROP"
        finally:
            await conn.close()

    asyncio.run(scenario())


def test_entity_discovery_real_age_order_filter_and_cursor() -> None:
    """LIVE-PARKED proof for AGE 1.6 comparison/order/function syntax."""
    store = AgeGraphStore(_DSN, graph_name=f"ne_discovery_{uuid.uuid4().hex[:8]}")

    async def scenario() -> None:
        try:
            await store.ensure_graph()
            await store.upsert_nodes(
                [
                    {
                        "key": key,
                        "entity_class": "Account",
                        "display_name": name,
                        "source_system": "fixture",
                        "source_ref": key,
                        "acl": ("g",),
                        "content_hash": "h",
                        "config_hash": "c",
                        "schema_version": "1.0.0",
                        "synced_at": "2026-08-20T00:00:00+00:00",
                    }
                    for key, name in (("b", "Acme Beta"), ("a", "Acme Alpha"), ("z", "Zulu"))
                ]
            )
            graph = EntityGraph(store, group_expander=None, audit_sink=None)
            first = await graph.discover(
                principal=Principal(id="u", groups=("g",)), q="acme", limit=1
            )
            assert [node.key for node in first.entities] == ["a"]
            assert first.next_cursor is not None
            second = await graph.discover(
                principal=Principal(id="u", groups=("g",)),
                q="acme",
                cursor=first.next_cursor,
                limit=1,
            )
            assert [node.key for node in second.entities] == ["b"]
        finally:
            await store.close()

    asyncio.run(scenario())
