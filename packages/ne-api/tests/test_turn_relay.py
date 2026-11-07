# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""relay_event_stream — the cross-process relay loop (NE-103c §4.4).

seq stripped/deduped, terminal stop, keep-alives during silence, the exact timeout
detail WITHOUT cancelling the workflow, and SSE bytes byte-identical to inline
encoding for identical payloads.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Coroutine, Sequence
from typing import Any

from ne.api.sse import KEEPALIVE_FRAME, relay_event_stream, sse_frame
from ne.api.turn_wire import TurnFrame, citations_frame, done_frame, token_frame

_RELAY_TIMEOUT_DETAIL = (
    "turn still running; the approval queue and session history hold the outcome"
)


def run(coro: Coroutine[Any, Any, list[bytes]]) -> list[bytes]:
    return asyncio.run(coro)


async def _from(
    frames: Sequence[TurnFrame], *, delays: Sequence[float] | None = None
) -> AsyncIterator[TurnFrame]:
    for i, frame in enumerate(frames):
        if delays is not None:
            await asyncio.sleep(delays[i])
        yield frame


async def _silent() -> AsyncIterator[TurnFrame]:
    await asyncio.sleep(3600)
    yield token_frame(0, "never")  # pragma: no cover — outlives the timeout


async def _collect(
    frames: AsyncIterator[TurnFrame],
    *,
    keepalive_s: float,
    timeout_s: float,
    trace_id: str | None = None,
) -> list[bytes]:
    out: list[bytes] = []
    async for chunk in relay_event_stream(
        frames,
        keepalive_s=keepalive_s,
        timeout_s=timeout_s,
        trace_id=trace_id,
    ):
        out.append(chunk)
    return out


def test_seq_stripped_and_bytes_match_inline() -> None:
    frames = [token_frame(0, "hello world"), done_frame(1, "sess-1", "hello world")]
    out = run(_collect(_from(frames), keepalive_s=15.0, timeout_s=5.0))
    # Byte-identical to inline-mode sse_frame encoding for the same payloads.
    assert out == [
        sse_frame("token", {"type": "token", "delta": "hello world"}),
        sse_frame("done", {"type": "done", "session_id": "sess-1", "output": "hello world"}),
    ]
    # No 'seq' key leaks onto the wire.
    assert all(b"seq" not in chunk for chunk in out)


def test_citations_payload_is_relayed_between_token_and_done() -> None:
    citations = [
        {
            "chunk_id": "c1",
            "document_id": "d1",
            "source_system": "wiki",
            "source_ref": "ref-1",
        }
    ]
    frames = [
        token_frame(0, "answer"),
        citations_frame(1, citations),
        done_frame(2, "s", "answer"),
    ]
    out = run(_collect(_from(frames), keepalive_s=15.0, timeout_s=5.0))
    assert out == [
        sse_frame("token", {"type": "token", "delta": "answer"}),
        sse_frame("citations", {"type": "citations", "citations": citations}),
        sse_frame("done", {"type": "done", "session_id": "s", "output": "answer"}),
    ]


def test_duplicate_seq_deduped() -> None:
    frames = [
        token_frame(0, "a"),
        token_frame(0, "a"),  # re-exec duplicate (same seq) — dropped
        done_frame(1, "s", "a"),
    ]
    out = run(_collect(_from(frames), keepalive_s=15.0, timeout_s=5.0))
    assert out == [
        sse_frame("token", {"type": "token", "delta": "a"}),
        sse_frame("done", {"type": "done", "session_id": "s", "output": "a"}),
    ]


def test_stops_at_error_frame() -> None:
    err = TurnFrame(seq=0, type="error", detail="ValueError")
    frames = [err, done_frame(1, "s", "x")]  # nothing after error should be read
    out = run(_collect(_from(frames), keepalive_s=15.0, timeout_s=5.0))
    assert out == [sse_frame("error", {"type": "error", "detail": "ValueError"})]


def test_keepalives_during_silence() -> None:
    # A frame arrives after ~0.1s; with a 0.02s keepalive several ticks precede it.
    out = run(
        _collect(_from([done_frame(0, "s", "x")], delays=[0.1]), keepalive_s=0.02, timeout_s=5.0)
    )
    assert out.count(KEEPALIVE_FRAME) >= 1
    assert out[-1] == sse_frame("done", {"type": "done", "session_id": "s", "output": "x"})


def test_timeout_emits_exact_detail_without_cancelling() -> None:
    reader = _silent()
    out = run(_collect(reader, keepalive_s=0.02, timeout_s=0.1))
    # The LAST frame is the exact design §4.4-5 timeout error; the turn is NOT
    # cancelled (relay just stops reading — there is no cancel call anywhere).
    assert out[-1] == sse_frame("error", {"type": "error", "detail": _RELAY_TIMEOUT_DETAIL})
