# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""``DbosStreamTurnFrameReader`` dedup semantics (NE-TURN-PROGRESS).

The reader is the FIRST of the two dedup sites between the worker's frame
stream and the browser (the second is ``relay_event_stream``). Both must let a
terminal ``done``/``error`` frame through REGARDLESS of seq: the worker's
raised-turn ``error`` carries seq 0 behind the step's tool frames (a raised step
cannot report how many it wrote), and either site swallowing it freezes the
reader at the last hop forever — a progress display that lies by omission.

The DBOS client is faked at the seam (``read_stream_async`` yielding raw dicts);
everything from validation to dedup is the real reader code.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from ne.api.turn_relay import (
    _STREAM_POLL_INTERVAL_S,
    DbosStreamTurnFrameReader,
    _tighten_stream_poll_interval,
)
from ne.api.turn_wire import TurnFrame, done_frame, error_frame, token_frame, tool_frame

from .conftest import run


class _FakeDbosClient:
    def __init__(self, values: list[dict[str, object]]) -> None:
        self._values = values

    async def read_stream_async(self, _workflow_id: str, _key: str) -> AsyncIterator[object]:
        for value in self._values:
            yield value


def _reader(frames: list[TurnFrame]) -> DbosStreamTurnFrameReader:
    reader = object.__new__(DbosStreamTurnFrameReader)
    reader._client = _FakeDbosClient([f.model_dump(mode="json") for f in frames])  # type: ignore[attr-defined]
    reader._stream_key = "frames"  # type: ignore[attr-defined]
    return reader


def _collect(reader: DbosStreamTurnFrameReader) -> list[TurnFrame]:
    async def _t() -> list[TurnFrame]:
        return [frame async for frame in reader.frames("turn-x")]

    return run(_t())


def test_terminal_error_passes_the_reader_despite_a_colliding_seq() -> None:
    got = _collect(
        _reader(
            [
                tool_frame(0, name="write.internal.demo.note", tier=1, decision="allow"),
                error_frame(0, RuntimeError("model backend fell over mid-turn")),
            ]
        )
    )
    assert [f.type for f in got] == ["tool", "error"], (
        "the seq-0 terminal must not be swallowed by the monotonic dedup"
    )


def test_duplicate_non_terminal_is_dropped_and_the_done_passes() -> None:
    got = _collect(
        _reader(
            [
                token_frame(0, "first"),
                token_frame(0, "duplicate re-delivery"),
                done_frame(1, "SID", "first"),
            ]
        )
    )
    assert [f.type for f in got] == ["token", "done"]
    assert got[0].delta == "first"


class _FakeSystemDatabase:
    _notification_listener_polling_interval_sec = 1.0


class _ClientWithSystemDatabase:
    def __init__(self) -> None:
        self._sys_db = _FakeSystemDatabase()


class _ClientWithoutSystemDatabase:
    pass


def test_reader_tightens_the_clients_fallback_poll_interval() -> None:
    """The dead-air floor is ours, not the vendored 1.0 s default.

    ``DBOSClient`` runs no LISTEN/NOTIFY listener, so an idle gap in a turn's frame
    stream costs exactly this interval — measured at +1.006 s on the first hop
    frame before this, +0.244 s after
    (``packages/ne-worker/tests/test_turn_frame_stream_visibility.py``).
    """
    client = _ClientWithSystemDatabase()
    _tighten_stream_poll_interval(client)
    assert client._sys_db._notification_listener_polling_interval_sec == _STREAM_POLL_INTERVAL_S
    assert _STREAM_POLL_INTERVAL_S < 1.0


def test_tightening_a_client_without_the_knob_is_a_no_op() -> None:
    """A dbos upgrade that renames the attribute must not fail a composition root.

    The reader is constructed inside ``create_app``; raising here would turn a
    frame-stream tuning knob into a boot failure. Slower, never broken.
    """
    _tighten_stream_poll_interval(_ClientWithoutSystemDatabase())
    _tighten_stream_poll_interval(object())
