# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-023 curate: re-stamp, ACL preservation, invalid-patch semantics."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from ne.brain.write import (
    CuratedKnowledgeItem,
    InvalidPatchError,
    KnowledgeItemNotFoundError,
    ProvenanceClass,
    ShapeValidationError,
    provenance_from_chunk,
)
from ne.contracts.knowledge import KnowledgeItem
from ne.contracts.types import Chunk
from ne.events import read_batch

from .conftest import STRICT_NOTE_CONTRACT, WriteStack


def _shape_violations(stack: WriteStack) -> list[dict[str, object]]:
    with stack.engine.begin() as conn:
        records, _ = read_batch(conn, "curate-consumer")
    return [r.payload for r in records if r.event_type == "shape_violation"]


def test_curate_patches_content_and_metadata_restamps_curated(
    write_stack: Callable[..., WriteStack], frozen_config: str
) -> None:
    stack = write_stack()

    async def scenario() -> None:
        await stack.writer.upsert(
            [
                CuratedKnowledgeItem(
                    item_id="p1",
                    content="original",
                    provenance="ingest-src",
                    metadata={"keep": "me"},
                    provenance_class=ProvenanceClass.INGESTED,
                )
            ]
        )
        before = provenance_from_chunk((await stack.store.get(["p1"]))[0])
        assert before is not None

        await stack.writer.curate(
            "p1", {"content": "revised", "metadata": {"added": "yes"}}, provenance="curator-src"
        )
        chunk = (await stack.store.get(["p1"]))[0]
        assert chunk.text == "revised"
        assert chunk.metadata["keep"] == "me"
        assert chunk.metadata["added"] == "yes"

        after = provenance_from_chunk(chunk)
        assert after is not None
        assert after.provenance_class == ProvenanceClass.CURATED
        assert after.source == "curator-src"
        assert after.write_id != before.write_id

    asyncio.run(scenario())


def test_curate_preserves_acl(write_stack: Callable[..., WriteStack], frozen_config: str) -> None:
    stack = write_stack()

    async def scenario() -> None:
        await stack.writer.upsert(
            [
                CuratedKnowledgeItem(
                    item_id="p2", content="body", provenance="src", acl=("group-a", "group-b")
                )
            ]
        )
        await stack.writer.curate("p2", {"content": "edited"}, provenance="cur")
        assert (await stack.store.get(["p2"]))[0].acl == ("group-a", "group-b")

    asyncio.run(scenario())


def test_curate_invalid_patch_leaves_store_unchanged_and_signals(
    write_stack: Callable[..., WriteStack], frozen_config: str
) -> None:
    stack = write_stack(default_shape=STRICT_NOTE_CONTRACT.name)

    async def scenario() -> None:
        await stack.writer.upsert(
            [
                KnowledgeItem(
                    item_id="p3", content="body", metadata={"topic": "orig"}, provenance="src"
                )
            ]
        )
        with pytest.raises(ShapeValidationError):
            await stack.writer.curate("p3", {"metadata": {"topic": ""}}, provenance="cur")

        chunk = (await stack.store.get(["p3"]))[0]
        assert chunk.text == "body"
        assert chunk.metadata["topic"] == "orig"
        assert stack.quarantine.get("p3") is None
        violations = _shape_violations(stack)
        assert len(violations) == 1
        assert violations[0]["quarantined"] is False

    asyncio.run(scenario())


def test_curate_unknown_item_raises(
    write_stack: Callable[..., WriteStack], frozen_config: str
) -> None:
    stack = write_stack()

    async def scenario() -> None:
        with pytest.raises(KnowledgeItemNotFoundError):
            await stack.writer.curate("nope", {"content": "x"}, provenance="cur")

    asyncio.run(scenario())


def test_curate_reserved_provenance_key_rejected(
    write_stack: Callable[..., WriteStack], frozen_config: str
) -> None:
    stack = write_stack()

    async def scenario() -> None:
        await stack.writer.upsert([KnowledgeItem(item_id="p4", content="body", provenance="src")])
        with pytest.raises(InvalidPatchError):
            await stack.writer.curate(
                "p4", {"metadata": {"provenance": {"spoofed": True}}}, provenance="cur"
            )

    asyncio.run(scenario())


def test_curate_unknown_patch_key_rejected(
    write_stack: Callable[..., WriteStack], frozen_config: str
) -> None:
    stack = write_stack()

    async def scenario() -> None:
        await stack.writer.upsert([KnowledgeItem(item_id="p5", content="body", provenance="src")])
        with pytest.raises(InvalidPatchError):
            await stack.writer.curate("p5", {"acl": ("group-a",)}, provenance="cur")

    asyncio.run(scenario())


# -- NE-CKC2 / NE-023 fold: colon-ID quarantine + curate guards ----------------
#
# ':' is reserved for NE-026 spine chunk ids. All three governed-write surfaces
# (upsert — existing; quarantine + curate — NE-CKC2) reject a colon-namespaced
# item_id BEFORE any store fetch/park/delete, so a governed write can never
# remove or rewrite a spine chunk that shares the store.


def test_quarantine_rejects_colon_namespaced_item_id(
    write_stack: Callable[..., WriteStack], frozen_config: str
) -> None:
    stack = write_stack()

    async def scenario() -> None:
        # Seed the store directly with a spine-shaped (colon) chunk.
        await stack.store.upsert_chunks(
            [Chunk(chunk_id="doc:0001", document_id="doc", text="spine chunk", acl=())]
        )
        with pytest.raises(ValueError, match="reserved for NE-026"):
            await stack.writer.quarantine("doc:0001", "oops")
        # The spine chunk is untouched (no delete) and nothing was parked.
        assert (await stack.store.get(["doc:0001"]))[0].text == "spine chunk"
        assert stack.quarantine.get("doc:0001") is None

    asyncio.run(scenario())


def test_curate_rejects_colon_namespaced_item_id(
    write_stack: Callable[..., WriteStack], frozen_config: str
) -> None:
    stack = write_stack()

    async def scenario() -> None:
        await stack.store.upsert_chunks(
            [Chunk(chunk_id="doc:0001", document_id="doc", text="spine chunk", acl=())]
        )
        with pytest.raises(ValueError, match="reserved for NE-026"):
            await stack.writer.curate("doc:0001", {"content": "x"}, provenance="t")
        assert (await stack.store.get(["doc:0001"]))[0].text == "spine chunk"

    asyncio.run(scenario())
