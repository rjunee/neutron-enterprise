# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-TURN-PROGRESS — a hop frame is READABLE WHILE THE STEP IS STILL RUNNING.

WHY THIS FILE EXISTS. ``test_turn_progress_frames.py`` proves incrementality at the
WRITER seam, against :class:`ne.worker.relay.RecordingFrameWriter` — an in-memory
list. That is the strongest statement an in-memory writer can make, and it is
structurally blind to the one question the demo actually asked: does a frame the
worker writes from INSIDE ``chat.turn.execute`` become readable to ne-api's reader
BEFORE that step commits? An in-memory list answers yes by construction whatever
the durability engine does, so a regression there would ship green — and the
post-demo backlog attributed 45 s of dead screen to exactly that unproven gap.

So this test crosses the real seam with the real parts:

* the REAL :class:`ne.worker.relay.DbosStreamFrameWriter` writing from inside a real
  ``@DBOS.step`` that has NOT returned;
* the REAL :class:`ne.worker.workflows._TransitFrameDrain` draining a real
  :class:`ne.api.tool_transit.TurnTransitChannel` of real
  :class:`ne.api.tool_transit.ToolTransit` values;
* the REAL :class:`ne.api.turn_relay.DbosStreamTurnFrameReader` — a ``DBOSClient``
  on its own connection pool, exactly as ne-api reads;
* a real DBOS runtime over a SQLite system database. DBOS 2.26 supports one
  (``dbos._sys_db.SystemDatabase.create``), so this runs in the ordinary CI lane
  instead of only in ``live-postgres``.

THE DECISIVE ASSERTION IS AN ORDERING, NOT A DURATION — this box's load makes wall
clocks lie. The step parks on a ``threading.Event`` that ONLY the reader sets, and
the reader can only set it after it has already pulled the first ``tool`` frame off
the stream. If step-context writes were invisible until step commit the two would
deadlock and the test fails on its timeout, not on a flaky threshold. The observed
latency is still measured and printed, for the record.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from ne.api.tool_transit import ToolTransit, TurnTransitChannel
from ne.api.turn_relay import DbosStreamTurnFrameReader
from ne.api.turn_wire import TurnFrame, done_frame, error_frame, token_frame, tool_frame
from ne.worker.relay import DbosStreamFrameWriter
from ne.worker.workflows import _TransitFrameDrain

from .conftest import run

#: How long the parked step waits for the reader. Generous on purpose: it is a
#: deadlock detector, never a performance threshold.
_RELEASE_TIMEOUT_S = 30.0

_QUEUE_NAME = "test-chat-turns"

#: Cross-loop handshake. The DBOS queue executor runs the workflow on its OWN event
#: loop, so an ``asyncio.Event`` would be unsafe to share; threading is loop-agnostic.
_released = threading.Event()
_step_returned = threading.Event()
_first_write_at: list[float] = []


def _transit(name: str) -> ToolTransit:
    """A transit shaped exactly like the ones the observing sink publishes."""
    return ToolTransit(name=name, tier=1, decision="allow", audit_seq=7)


@pytest.fixture(autouse=True)
def _reset_handshake() -> None:
    """Per-test handshake state. The DBOS runtime itself is module-scoped."""
    _released.clear()
    _step_returned.clear()
    _first_write_at.clear()


@pytest.fixture(scope="module")
def dbos_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """A launched real DBOS over a SQLite system database, destroyed after.

    MODULE-SCOPED because DBOS's workflow/queue registry is a process global that
    ``DBOS.destroy()`` does not clear — a second construction in the same process
    raises on the re-declared queue. One runtime, distinct workflow ids per test.
    """
    from dbos import DBOS, DBOSConfig, Queue

    tmp_path: Path = tmp_path_factory.mktemp("dbos")
    url = f"sqlite:///{tmp_path / 'sys.db'}"
    config: DBOSConfig = {
        "name": "ne-turn-frame-visibility",
        "system_database_url": url,
        # The admin server is OFF everywhere in this repo (T3 asserts :3001 refuses).
        "run_admin_server": False,
    }
    DBOS(config=config)
    Queue(_QUEUE_NAME)

    @DBOS.step(name="test.chat.turn.execute")
    async def _execute_turn_step(turn_id: str) -> int:
        """``execute_turn_step``'s frame shape, with the real drain and writer."""
        channel = TurnTransitChannel()
        drain = _TransitFrameDrain(channel, DbosStreamFrameWriter(turn_id))
        drain.start()
        try:
            channel.publish(_transit("knowledge.search"))
            _first_write_at.append(time.monotonic())
            # PARK INSIDE THE STEP. Nothing written here can have been committed BY
            # the step, because the step has not returned. The reader releases us.
            deadline = time.monotonic() + _RELEASE_TIMEOUT_S
            while not _released.is_set() and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            channel.publish(_transit("knowledge.verified_read"))
            await drain.flush()
            return drain.seqs_used
        finally:
            await drain.flush()
            _step_returned.set()

    @DBOS.workflow(name="test.chat.turn")
    async def _run_turn_workflow(turn_id: str) -> str:
        emitted = await _execute_turn_step(turn_id)
        writer = DbosStreamFrameWriter(turn_id)
        # The terminal pair from WORKFLOW context, numbered AFTER the step's frames
        # — the production ordering (``run_turn_workflow``).
        await writer.emit(token_frame(emitted, "the answer"))
        await writer.emit(done_frame(emitted + 1, "session-1", "the answer"))
        await writer.close()
        return "ok"

    @DBOS.workflow(name="test.chat.turn.raised")
    async def _raised_turn_workflow(turn_id: str) -> str:
        """A turn that dies after two hops — the colliding-seq terminal case."""
        writer = DbosStreamFrameWriter(turn_id)
        await writer.emit(tool_frame(0, name="a", tier=1, decision="allow", audit_seq=1))
        await writer.emit(tool_frame(1, name="b", tier=1, decision="allow", audit_seq=2))
        # A re-executed step re-emits from seq 0; the reader's dedup must drop this.
        await writer.emit(tool_frame(1, name="b", tier=1, decision="allow", audit_seq=2))
        # A RAISED step cannot report how many frames it wrote, so its terminal keeps
        # seq 0 — behind every tool frame already on the stream.
        await writer.emit(error_frame(0, RuntimeError("boom")))
        await writer.close()
        return "ok"

    DBOS.launch()
    try:
        yield url
    finally:
        DBOS.destroy()


async def _enqueue(url: str, workflow_name: str, turn_id: str) -> None:
    from dbos import DBOSClient, EnqueueOptions

    client = DBOSClient(system_database_url=url)
    options: EnqueueOptions = {
        "queue_name": _QUEUE_NAME,
        "workflow_name": workflow_name,
        "workflow_id": turn_id,
    }
    await client.enqueue_async(options, turn_id)


async def _read_until_terminal(url: str, turn_id: str) -> list[TurnFrame]:
    reader = DbosStreamTurnFrameReader(system_database_url=url)
    seen: list[TurnFrame] = []
    async for frame in reader.frames(turn_id):
        seen.append(frame)
        if frame.type in ("done", "error"):
            break
    return seen


def test_hop_frame_is_readable_before_the_step_commits(dbos_url: str) -> None:
    turn_id = "turn-visibility-1"

    async def _drive() -> tuple[list[TurnFrame], float]:
        await _enqueue(dbos_url, "test.chat.turn", turn_id)
        reader = DbosStreamTurnFrameReader(system_database_url=dbos_url)
        seen: list[TurnFrame] = []
        first_latency = -1.0
        async for frame in reader.frames(turn_id):
            if frame.type == "tool" and not seen:
                # THE PROPERTY. The step is parked and cannot have committed; this
                # frame reached an independent reader anyway.
                assert not _step_returned.is_set(), (
                    "the first hop frame only became readable after the step returned "
                    "— per-hop progress is buffered to step commit"
                )
                first_latency = time.monotonic() - _first_write_at[0]
                _released.set()
            seen.append(frame)
            if frame.type in ("done", "error"):
                break
        return seen, first_latency

    frames, first_latency = run(asyncio.wait_for(_drive(), timeout=120))

    assert [frame.type for frame in frames] == ["tool", "tool", "token", "done"]
    assert [frame.seq for frame in frames] == [0, 1, 2, 3]
    assert [frame.name for frame in frames[:2]] == [
        "knowledge.search",
        "knowledge.verified_read",
    ]
    # Reported, never asserted as a threshold: a loaded box makes this number lie,
    # and the ordering assertion above is what actually holds the property.
    print(f"\ntime-to-first-visible-frame: +{first_latency:.3f}s after the worker wrote it")


def test_terminal_reaches_the_reader_behind_the_hop_frames(dbos_url: str) -> None:
    """A raised turn's seq-0 ``error`` is delivered behind higher-seq tool frames.

    ``DbosStreamTurnFrameReader.frames`` passes terminals regardless of ``seq`` for
    exactly this case, and drops a re-executed step's duplicate non-terminal. Both
    rules are pinned here against the REAL stream rather than a hand-built iterator.
    """
    turn_id = "turn-visibility-2"

    async def _drive() -> list[TurnFrame]:
        await _enqueue(dbos_url, "test.chat.turn.raised", turn_id)
        return await _read_until_terminal(dbos_url, turn_id)

    frames = run(asyncio.wait_for(_drive(), timeout=120))
    assert [(frame.type, frame.seq) for frame in frames] == [
        ("tool", 0),
        ("tool", 1),
        ("error", 0),
    ]
