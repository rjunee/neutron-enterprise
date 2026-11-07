# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Regression: the brain read view must serialize chunk metadata that is a nested
frozen tree.

Chunk ``metadata`` is coerced to a frozen structure at validation time, so a
nested mapping (e.g. an ingested ``provenance`` block) becomes a nested
``FrozenDict``. A shallow ``dict(metadata)`` in ``retrieval_result_view`` left
those nested ``FrozenDict`` values in place, and ``model_dump_json`` raised
``PydanticSerializationError: Unable to serialize unknown type FrozenDict`` — so
``/v1/brain/search`` 500'd for any real ingested chunk (only surfaced against a
live stack; fixture CI never exercised provenance-bearing metadata).
"""

from __future__ import annotations

from ne.api.wire import retrieval_result_view
from ne.contracts.types import Chunk, RetrievalResult, ScoredChunk


def test_retrieval_view_serializes_nested_frozen_metadata() -> None:
    chunk = Chunk(
        chunk_id="c1",
        document_id="d1",
        text="the HR incident response SLA is 4 hours",
        # nested mapping -> nested FrozenDict after frozen coercion
        metadata={
            "connector": "acme-fixture",
            "provenance": {"actor": "connect-sync", "source": "connector:acme:doc-1"},
        },
        acl=("all-employees",),
    )
    result = RetrievalResult(chunks=(ScoredChunk(chunk=chunk, score=1.0),), citations=())

    view = retrieval_result_view(result)
    dumped = view.model_dump_json()  # must NOT raise on the nested FrozenDict

    assert "provenance" in dumped
    assert "connect-sync" in dumped
    # deep-thawed to plain JSON structures
    assert view.chunks[0].chunk.metadata["provenance"] == {
        "actor": "connect-sync",
        "source": "connector:acme:doc-1",
    }
