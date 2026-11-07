# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""P3-2: the frozen ``AgeGraphStore`` upsert chokepoint allowlist-validates every
cypher identifier (node ``entity_class`` / edge ``edge_kind``) BEFORE it is
interpolated into a ``MERGE (n:{label} ...)`` / ``[e:{kind}]`` statement.

A metacharacter/backtick-shaped identifier derived from a record payload would
otherwise become executed cypher (a C7 tamper path — rewrite ACLs / delete nodes)
at the frozen GraphStore write boundary. The rejection happens BEFORE any database
connection, so these tests need no live AGE container.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from ne.brain_graph.age.store import AgeGraphStore, _require_props
from ne.brain_graph.errors import GraphStoreError
from ne.schema import EDGE_KINDS

_UNUSED_DSN = "postgresql://unused.invalid:5432/x"


def _node(entity_class: str) -> dict[str, object]:
    return {
        "key": "k",
        "entity_class": entity_class,
        "display_name": "d",
        "source_system": "s",
        "source_ref": "r",
        "acl": ["g"],
        "content_hash": "h",
        "schema_version": "1",
        "config_hash": "c",
        "synced_at": "t",
    }


def _edge(edge_kind: str, *, key: str = "k") -> dict[str, object]:
    return {
        "key": key,
        "edge_kind": edge_kind,
        "source_system": "s",
        "source_ref": "r",
        "acl": ["g"],
        "config_hash": "c",
        "synced_at": "t",
        "from_key": "a",
        "to_key": "b",
    }


def test_upsert_nodes_rejects_injection_shaped_entity_class() -> None:
    store = AgeGraphStore(_UNUSED_DSN)
    injection = _node("Account`) DETACH DELETE (n) //")
    with pytest.raises(GraphStoreError):
        asyncio.run(store.upsert_nodes([injection]))
    # A plain unknown class is also refused (allowlist, not just metachar-scan).
    with pytest.raises(GraphStoreError):
        asyncio.run(store.upsert_nodes([_node("Widget")]))


def test_upsert_edges_rejects_injection_shaped_edge_kind() -> None:
    store = AgeGraphStore(_UNUSED_DSN)
    injection = _edge("WORKS_AT`]->() DELETE //")
    with pytest.raises(GraphStoreError):
        asyncio.run(store.upsert_edges([injection]))
    with pytest.raises(GraphStoreError):
        asyncio.run(store.upsert_edges([_edge("FRIENDS_WITH")]))


def test_edge_kind_allowlist_accepts_schema_kinds_and_same_as() -> None:
    # Every schema EDGE_KINDS member plus the ER lane's SAME_AS validates with no
    # raise (the er-merge lane keeps working through the same chokepoint).
    for kind in (*sorted(EDGE_KINDS), "SAME_AS"):
        _require_props(_edge(kind), tuple(_edge(kind).keys()), "edge")


def test_confirmed_edge_upsert_refuses_age_zero_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def zero_match(*_args: object, **_kwargs: object) -> list[tuple[str, ...]]:
        return []

    monkeypatch.setattr("ne.brain_graph.age.store._pg.execute_cypher", zero_match)
    store = AgeGraphStore(_UNUSED_DSN)
    store._conn = object()

    with pytest.raises(GraphStoreError, match="exact persisted edge acknowledgment"):
        asyncio.run(store.upsert_edges_confirmed([_edge("WORKS_AT")]))


@pytest.mark.parametrize(
    "returned_keys",
    [
        ("k1",),
        ("k1", "k1"),
        ("k1", "k2", "unexpected"),
    ],
    ids=("partial", "duplicate", "extra"),
)
def test_confirmed_edge_upsert_rejects_inexact_age_rows(
    monkeypatch: pytest.MonkeyPatch,
    returned_keys: tuple[str, ...],
) -> None:
    async def inexact_rows(*_args: object, **_kwargs: object) -> list[tuple[str, ...]]:
        return [(json.dumps(key),) for key in returned_keys]

    monkeypatch.setattr("ne.brain_graph.age.store._pg.execute_cypher", inexact_rows)
    store = AgeGraphStore(_UNUSED_DSN)
    store._conn = object()

    with pytest.raises(GraphStoreError, match="exact persisted edge acknowledgment"):
        asyncio.run(
            store.upsert_edges_confirmed([_edge("WORKS_AT", key="k1"), _edge("WORKS_AT", key="k2")])
        )


def test_confirmed_edge_upsert_returns_only_exact_age_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, tuple[str, ...]]] = []

    async def exact_rows(
        _conn: object,
        _graph_name: str,
        text: str,
        params: dict[str, object],
        *,
        result_columns: tuple[str, ...],
    ) -> list[tuple[str, ...]]:
        calls.append((text, result_columns))
        rows = params["rows"]
        assert isinstance(rows, list)
        return [(json.dumps(str(row["key"])),) for row in rows]

    monkeypatch.setattr("ne.brain_graph.age.store._pg.execute_cypher", exact_rows)
    store = AgeGraphStore(_UNUSED_DSN)
    store._conn = object()

    acknowledged = asyncio.run(
        store.upsert_edges_confirmed([_edge("WORKS_AT", key="k1"), _edge("WORKS_AT", key="k2")])
    )

    assert acknowledged == ("k1", "k2")
    assert len(calls) == 1
    assert calls[0][1] == ("result",)
    assert "RETURN e.key AS result" in calls[0][0]
    assert "RETURN row.key" not in calls[0][0]
