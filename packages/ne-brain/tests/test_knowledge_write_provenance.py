# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-023 provenance-class round-trip (the NE-037 promotion-path contract)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from ne.brain.testing.embedding import HashEmbedder
from ne.brain.write import CuratedKnowledgeItem, ProvenanceClass, provenance_from_chunk

from .conftest import WriteStack

_CLASSES = [
    ("ing", ProvenanceClass.INGESTED, "ingested"),
    ("cur", ProvenanceClass.CURATED, "curated"),
    ("agent", ProvenanceClass.LEARNED_BY_AGENT, "learned-by-agent"),
]


def test_provenance_class_round_trip(
    write_stack: Callable[..., WriteStack], frozen_config: str
) -> None:
    stack = write_stack()

    async def scenario() -> None:
        await stack.writer.upsert(
            [
                CuratedKnowledgeItem(
                    item_id=item_id,
                    content=f"note {item_id}",
                    provenance="src",
                    provenance_class=cls,
                )
                for item_id, cls, _ in _CLASSES
            ]
        )
        for item_id, cls, value in _CLASSES:
            stored = await stack.store.get([item_id])
            stamp = provenance_from_chunk(stored[0])
            assert stamp is not None
            assert stamp.provenance_class == cls
            # Independently filterable by class.
            dense = list((await HashEmbedder(32).embed([f"note {item_id}"]))[0])
            hits = await stack.store.query(
                dense=dense,
                filters={"metadata.provenance.provenance_class": value},
            )
            ids = {h.chunk.chunk_id for h in hits}
            assert item_id in ids

    asyncio.run(scenario())


def test_stamp_shape_records_admitting_contract(
    write_stack: Callable[..., WriteStack], frozen_config: str
) -> None:
    stack = write_stack()

    async def scenario() -> None:
        await stack.writer.upsert(
            [CuratedKnowledgeItem(item_id="s1", content="body", provenance="src")]
        )
        stamp = provenance_from_chunk((await stack.store.get(["s1"]))[0])
        assert stamp is not None
        assert stamp.shape == "ne.knowledge.note@1"

    asyncio.run(scenario())
