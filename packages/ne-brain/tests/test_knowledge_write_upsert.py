# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-023 upsert: provenance stamping, queryability, ACL, idempotency."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from ne.brain.testing.embedding import HashEmbedder
from ne.brain.write import CuratedKnowledgeItem, provenance_from_chunk
from ne.contracts.knowledge import KnowledgeItem

from .conftest import WriteStack

_STAMP_FIELDS = {"provenance_class", "source", "actor", "written_at", "write_id", "shape"}


def test_upsert_stamps_provenance_and_is_queryable(
    write_stack: Callable[..., WriteStack], frozen_config: str
) -> None:
    stack = write_stack()

    async def scenario() -> None:
        receipt = await stack.writer.upsert(
            [KnowledgeItem(item_id="k1", content="quarterly revenue report", provenance="src-a")]
        )
        assert receipt.accepted == ("k1",)

        stored = await stack.store.get(["k1"])
        prov = stored[0].metadata.get("provenance")
        assert prov is not None
        assert set(prov) == _STAMP_FIELDS
        assert prov["provenance_class"] == "curated"
        assert prov["source"] == "src-a"
        assert prov["shape"] == "ne.knowledge.note@1"

        dense = list((await HashEmbedder(32).embed(["quarterly revenue report"]))[0])
        hits = await stack.store.query(
            dense=dense,
            filters={"metadata.provenance.provenance_class": "curated"},
        )
        assert "k1" in {h.chunk.chunk_id for h in hits}

    asyncio.run(scenario())


def test_upsert_carries_acl_onto_stored_chunks(
    write_stack: Callable[..., WriteStack], frozen_config: str
) -> None:
    stack = write_stack()

    async def scenario() -> None:
        await stack.writer.upsert(
            [
                CuratedKnowledgeItem(
                    item_id="acl1", content="restricted note", provenance="src", acl=("group-a",)
                ),
                KnowledgeItem(item_id="acl2", content="open note", provenance="src"),
            ]
        )
        by_id = {c.chunk_id: c for c in await stack.store.get(["acl1", "acl2"])}
        assert by_id["acl1"].acl == ("group-a",)
        assert by_id["acl2"].acl == ()

    asyncio.run(scenario())


def test_upsert_duplicate_item_ids_rejected(
    write_stack: Callable[..., WriteStack], frozen_config: str
) -> None:
    stack = write_stack()

    async def scenario() -> None:
        with pytest.raises(ValueError, match="duplicate"):
            await stack.writer.upsert(
                [
                    KnowledgeItem(item_id="dup", content="one", provenance="src"),
                    KnowledgeItem(item_id="dup", content="two", provenance="src"),
                ]
            )
        assert await stack.store.get(["dup"]) == []

    asyncio.run(scenario())


def test_upsert_colon_item_id_rejected(
    write_stack: Callable[..., WriteStack], frozen_config: str
) -> None:
    stack = write_stack()

    async def scenario() -> None:
        with pytest.raises(ValueError, match="':'"):
            await stack.writer.upsert(
                [KnowledgeItem(item_id="doc:0001", content="spine-shaped", provenance="src")]
            )

    asyncio.run(scenario())


def test_upsert_same_item_id_overwrites_in_place(
    write_stack: Callable[..., WriteStack], frozen_config: str
) -> None:
    stack = write_stack()

    async def scenario() -> None:
        await stack.writer.upsert(
            [KnowledgeItem(item_id="ovr", content="first version", provenance="src")]
        )
        await stack.writer.upsert(
            [KnowledgeItem(item_id="ovr", content="second version", provenance="src")]
        )
        stored = await stack.store.get(["ovr"])
        assert len(stored) == 1
        assert stored[0].text == "second version"
        prov = provenance_from_chunk(stored[0])
        assert prov is not None and prov.source == "src"

    asyncio.run(scenario())


def test_successful_upsert_clears_prior_quarantine_row(
    write_stack: Callable[..., WriteStack], frozen_config: str
) -> None:
    stack = write_stack()

    async def scenario() -> None:
        await stack.writer.upsert([KnowledgeItem(item_id="fix", content="", provenance="src")])
        assert stack.quarantine.get("fix") is not None

        await stack.writer.upsert(
            [KnowledgeItem(item_id="fix", content="now valid", provenance="src")]
        )
        assert stack.quarantine.get("fix") is None
        assert (await stack.store.get(["fix"]))[0].text == "now valid"

    asyncio.run(scenario())
