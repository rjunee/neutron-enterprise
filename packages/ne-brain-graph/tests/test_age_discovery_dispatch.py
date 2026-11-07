# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""AGE result-column dispatch for the entity-discovery read surface."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from ne.brain_graph.age.query import PredicateStyle, TrimmedCypherBuilder
from ne.brain_graph.age.store import AgeGraphStore


def test_age_discovery_requests_the_single_node_result_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, tuple[str, ...]]] = []
    props = {
        "key": "customer-1",
        "entity_class": "Customer",
        "display_name": "Customer One",
    }

    async def one_node(
        _conn: object,
        _graph_name: str,
        text: str,
        _params: dict[str, Any],
        *,
        result_columns: tuple[str, ...],
    ) -> list[tuple[str]]:
        calls.append((text, result_columns))
        return [(json.dumps(props),)]

    monkeypatch.setattr("ne.brain_graph.age.store._pg.execute_cypher", one_node)
    store = AgeGraphStore("postgresql://unused")
    store._conn = object()
    compiled = TrimmedCypherBuilder(PredicateStyle.POST_FILTER).build_discovery(
        ["role:ops"], limit=2
    )

    rows = asyncio.run(store.cypher(compiled.text, compiled.params))

    assert rows == [{"node": props}]
    assert calls == [(compiled.text, ("node",))]
