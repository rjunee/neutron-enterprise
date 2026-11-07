# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The worker-side frame writer seam — ``TurnFrameWriter`` (NE-103c §4.3/§5.1).

The turn workflow emits transport-independent :class:`ne.api.turn_wire.TurnFrame`
records; ne-api's SOLE ``sse_frame`` encoder renders them to the browser. This
module holds the write half of the transport-agnostic seam:

* :class:`DbosStreamFrameWriter` (primary) — ``DBOS.write_stream_async("frames", …)`` /
  ``DBOS.close_stream_async("frames")``. Terminal frames are written from WORKFLOW
  context = exactly-once (checkpointed like any workflow action, design §5.1);
  NE-TURN-PROGRESS ``tool`` frames are written from STEP context (DBOS supports
  step-stream writes; ``write_stream_from_step``) = at-least-once, deduplicated by
  the relay's monotonic ``seq``. The turn workflow is an ``async def`` DBOS
  workflow, so the ASYNC stream API is mandatory — the sync ``DBOS.write_stream``
  raises ``check_async`` when a loop is running. ``dbos`` is imported LAZILY.
* :class:`RecordingFrameWriter` — an in-memory list, the ``durability='off'`` / T5
  passthrough writer (proves upgrade-in-place is a tested property).

The stream API is verified present in the locked ``dbos>=2.26.0`` (componentVerification),
so the durable ``ne_turn_frames`` fallback table (impl B) is NOT built — the Protocol
seam IS the fallback insurance (design §2.2/§4.3: decide in round 1, do not carry both).
"""

from __future__ import annotations

from typing import Protocol

from ne.api.turn_wire import TurnFrame


class TurnFrameWriter(Protocol):
    """Emit ordered frames for one turn, then close the stream (terminal).

    ``emit``/``close`` are async: the primary DBOS implementation must use the async
    stream API from inside the async turn workflow's running event loop.
    """

    async def emit(self, frame: TurnFrame) -> None: ...

    async def close(self) -> None: ...


class DbosStreamFrameWriter:
    """Write frames onto the workflow's ``"frames"`` DBOS stream (exactly-once)."""

    def __init__(self, turn_id: str, *, stream_key: str = "frames") -> None:
        # turn_id is implicit in the workflow context; kept for symmetry/logging.
        self._turn_id = turn_id
        self._stream_key = stream_key

    async def emit(self, frame: TurnFrame) -> None:
        from dbos import DBOS

        await DBOS.write_stream_async(self._stream_key, frame.model_dump(mode="json"))

    async def close(self) -> None:
        from dbos import DBOS

        await DBOS.close_stream_async(self._stream_key)


class RecordingFrameWriter:
    """Collect frames in memory (the ``durability='off'`` / T5 writer)."""

    def __init__(self, turn_id: str) -> None:
        self.turn_id = turn_id
        self.frames: list[TurnFrame] = []
        self.closed = False

    async def emit(self, frame: TurnFrame) -> None:
        self.frames.append(frame)

    async def close(self) -> None:
        self.closed = True
