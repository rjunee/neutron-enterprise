# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""KnowledgeWrite conformance kit — governed upsert/curate/quarantine.

Async contract methods are exercised via ``asyncio.run`` (pytest-asyncio is not
a dependency and must not be added). ``create_writer`` is a SYNCHRONOUS factory
(invoked from a plain sync context, before the per-test ``asyncio.run``): it
wires a real backing ``ChunkStore`` behind the frozen ``KnowledgeWrite`` seam and
returns ``(writer, store)`` so the kit can assert Protocol-observable behavior
(receipt partitioning, store round-trips) without reaching past the seam.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

from ne.brain.write.contracts import CuratedKnowledgeItem, ShapeContract
from ne.contracts.knowledge import ChunkStore, KnowledgeItem, KnowledgeWrite

_NOTE_SHAPE = "ne.knowledge.note"
_NOTE_CONTRACT = ShapeContract(
    name=_NOTE_SHAPE,
    version="1",
    json_schema={
        "type": "object",
        "required": ["content"],
        "properties": {
            "content": {"type": "string", "minLength": 1},
            "metadata": {"type": "object"},
        },
    },
)


class KnowledgeWriteContractKit:
    """Subclass and override :meth:`create_writer` to prove KnowledgeWrite conformance."""

    def create_writer(
        self, contracts: Mapping[str, ShapeContract], *, default_shape: str
    ) -> tuple[KnowledgeWrite, ChunkStore]:
        """Return a ``(writer, store)`` pair over a fresh backing store."""
        raise NotImplementedError

    def _writer(self) -> tuple[KnowledgeWrite, ChunkStore]:
        return self.create_writer({_NOTE_SHAPE: _NOTE_CONTRACT}, default_shape=_NOTE_SHAPE)

    def test_upsert_valid_items_accepted_and_fetchable(self) -> None:
        writer, store = self._writer()
        items = [
            KnowledgeItem(item_id="k1", content="alpha note", provenance="unit-test"),
            KnowledgeItem(item_id="k2", content="beta note", provenance="unit-test"),
        ]

        async def scenario() -> None:
            receipt = await writer.upsert(items)
            assert receipt.accepted == ("k1", "k2")
            assert receipt.quarantined == ()
            fetched = await store.get(["k1", "k2"])
            assert {c.chunk_id for c in fetched} == {"k1", "k2"}

        asyncio.run(scenario())

    def test_upsert_invalid_item_quarantined_batch_continues(self) -> None:
        writer, store = self._writer()
        items = [
            KnowledgeItem(item_id="ok", content="valid note", provenance="unit-test"),
            KnowledgeItem(item_id="bad", content="", provenance="unit-test"),
        ]

        async def scenario() -> None:
            receipt = await writer.upsert(items)
            assert receipt.accepted == ("ok",)
            assert receipt.quarantined == ("bad",)
            fetched = await store.get(["ok", "bad"])
            assert {c.chunk_id for c in fetched} == {"ok"}

        asyncio.run(scenario())

    def test_curate_updates_content(self) -> None:
        writer, store = self._writer()

        async def scenario() -> None:
            await writer.upsert(
                [KnowledgeItem(item_id="c1", content="original", provenance="unit-test")]
            )
            await writer.curate("c1", {"content": "revised"}, provenance="curator")
            fetched = await store.get(["c1"])
            assert fetched[0].text == "revised"

        asyncio.run(scenario())

    def test_quarantine_removes_item_from_store(self) -> None:
        writer, store = self._writer()

        async def scenario() -> None:
            await writer.upsert(
                [CuratedKnowledgeItem(item_id="q1", content="to be pulled", provenance="unit-test")]
            )
            await writer.quarantine("q1", "curator pull")
            fetched = await store.get(["q1"])
            assert fetched == [] or list(fetched) == []

        asyncio.run(scenario())
