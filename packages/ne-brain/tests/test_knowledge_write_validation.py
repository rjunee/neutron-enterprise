# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-023 shape validation: reject-before-store, quarantine, ShapeViolation."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from ne.brain.write import CuratedKnowledgeItem, ShapeContract
from ne.contracts.knowledge import KnowledgeItem
from ne.events import read_batch

from .conftest import STRICT_NOTE_CONTRACT, WriteStack


def _shape_violations(stack: WriteStack) -> list[dict[str, object]]:
    with stack.engine.begin() as conn:
        records, _ = read_batch(conn, "test-consumer")
    return [r.payload for r in records if r.event_type == "shape_violation"]


def test_valid_write_enters_store(
    write_stack: Callable[..., WriteStack], frozen_config: str
) -> None:
    stack = write_stack()

    async def scenario() -> None:
        receipt = await stack.writer.upsert(
            [KnowledgeItem(item_id="v1", content="valid", provenance="src")]
        )
        assert receipt.accepted == ("v1",)
        assert (await stack.store.get(["v1"]))[0].text == "valid"

    asyncio.run(scenario())


def test_invalid_write_rejected_quarantined_and_violation_emitted(
    write_stack: Callable[..., WriteStack], frozen_config: str
) -> None:
    stack = write_stack(default_shape=STRICT_NOTE_CONTRACT.name)

    async def scenario() -> None:
        # Missing required metadata.topic under the strict contract.
        receipt = await stack.writer.upsert(
            [KnowledgeItem(item_id="bad", content="body", provenance="src")]
        )
        assert receipt.quarantined == ("bad",)
        # (i) does NOT enter the store.
        assert await stack.store.get(["bad"]) == []
        # (ii) inspectable quarantine lane WITH full payload + reason.
        parked = stack.quarantine.get("bad")
        assert parked is not None
        assert parked.payload["content"] == "body"
        assert "topic" in parked.reason
        # (iii) exactly one shape_violation, quarantined=True, valid config_hash.
        violations = _shape_violations(stack)
        assert len(violations) == 1
        ev = violations[0]
        assert ev["quarantined"] is True
        assert ev["schema_name"] == STRICT_NOTE_CONTRACT.name
        assert ev["schema_version"] == STRICT_NOTE_CONTRACT.version
        assert ev["subject_ref"] == "bad"
        assert ev["config_hash"] == frozen_config
        assert len(str(ev["config_hash"])) == 64

    asyncio.run(scenario())


def test_unknown_shape_name_quarantines(
    write_stack: Callable[..., WriteStack], frozen_config: str
) -> None:
    stack = write_stack()

    async def scenario() -> None:
        receipt = await stack.writer.upsert(
            [
                CuratedKnowledgeItem(
                    item_id="u1", content="body", provenance="src", shape="ne.knowledge.absent"
                )
            ]
        )
        assert receipt.quarantined == ("u1",)
        assert await stack.store.get(["u1"]) == []
        violations = _shape_violations(stack)
        assert len(violations) == 1
        assert violations[0]["schema_version"] == "unregistered"

    asyncio.run(scenario())


def test_batch_continues_past_quarantined_item(
    write_stack: Callable[..., WriteStack], frozen_config: str
) -> None:
    stack = write_stack()

    async def scenario() -> None:
        receipt = await stack.writer.upsert(
            [
                KnowledgeItem(item_id="a", content="alpha", provenance="src"),
                KnowledgeItem(item_id="b", content="", provenance="src"),
                KnowledgeItem(item_id="c", content="gamma", provenance="src"),
            ]
        )
        assert receipt.accepted == ("a", "c")
        assert receipt.quarantined == ("b",)
        assert {c.chunk_id for c in await stack.store.get(["a", "b", "c"])} == {"a", "c"}

    asyncio.run(scenario())


def test_quarantine_lane_inspectable(
    write_stack: Callable[..., WriteStack], frozen_config: str
) -> None:
    stack = write_stack()

    async def scenario() -> None:
        await stack.writer.upsert(
            [
                KnowledgeItem(item_id="q1", content="", provenance="src"),
                KnowledgeItem(item_id="q2", content="", provenance="src"),
            ]
        )
        parked = {p.item_id for p in stack.quarantine.list_quarantined()}
        assert parked == {"q1", "q2"}

    asyncio.run(scenario())


def test_invalid_contract_schema_fails_at_construction() -> None:
    with pytest.raises(ValueError, match="invalid JSON-Schema"):
        ShapeContract(name="broken", version="1", json_schema={"type": 42})
