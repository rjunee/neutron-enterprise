# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""GC sweep — audit-then-delete orphaned document tuples, fail-closed on sink."""

from __future__ import annotations

import asyncio

import pytest

from ne.permissions_rebac.errors import RebacAuditError
from ne.permissions_rebac.gc import run_gc_sweep
from ne.permissions_rebac.testing.fakes import (
    FailingAuditSink,
    InMemoryOpenFga,
    NeverExists,
    RecordingAuditSink,
)

_LIVE = ("user:jane", "viewer", "document:live")
_DEAD1 = ("user:jane", "viewer", "document:dead")
_DEAD2 = ("user:bob", "owner", "document:dead")


class _SelectiveExists:
    def __init__(self, existing: set[str]) -> None:
        self.existing = existing

    async def exists(self, document_id: str) -> bool:
        return document_id in self.existing


def test_sweep_removes_dead_keeps_live_and_audits_before_delete() -> None:
    client = InMemoryOpenFga(tuples={_LIVE, _DEAD1, _DEAD2})
    sink = RecordingAuditSink()
    removed = asyncio.run(run_gc_sweep(client, _SelectiveExists({"live"}), sink))
    assert removed == 2
    assert client.tuples == {_LIVE}
    assert len(sink.events) == 2
    assert all(e.action == "rebac.gc.removed" for e in sink.events)
    assert all(e.actor == "system:rebac-gc" for e in sink.events)


def test_sweep_with_all_live_removes_nothing() -> None:
    client = InMemoryOpenFga(tuples={_LIVE, _DEAD1})
    sink = RecordingAuditSink()
    removed = asyncio.run(run_gc_sweep(client, _SelectiveExists({"live", "dead"}), sink))
    assert removed == 0
    assert client.tuples == {_LIVE, _DEAD1}
    assert sink.events == []


def test_failing_sink_aborts_with_tuples_intact() -> None:
    client = InMemoryOpenFga(tuples={_DEAD1, _DEAD2})
    with pytest.raises(RebacAuditError):
        asyncio.run(run_gc_sweep(client, NeverExists(), FailingAuditSink()))
    assert client.tuples == {_DEAD1, _DEAD2}  # nothing deleted
