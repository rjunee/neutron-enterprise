# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-137-FU-SSE: the SSE transit-multiplexer terminal-frame race.

The inline PDP lane's ``turn_event_stream`` rides a ``queue.get()`` getter task on
the same ``asyncio.wait`` set as the turn. That getter can COMPLETE while the
generator is suspended at a keep-alive yield (or concurrently with the turn's
terminal signal): once it completes it has already POPPED its :class:`ToolTransit`
off the channel queue, so the pre-terminal ``_drain()`` sees an EMPTY queue and the
completed governed decision — frequently the turn's final SCRAM deny, the LAST
frame before turn end — is silently discarded in the ``finally`` block. That breaks
design §0 law 5 (the Beat-4 surface must show EVERY governed tool decision,
including the last one).

The fix settles the outstanding getter (cancel-and-INSPECT) BEFORE every terminal
frame: a completed getter's transit is emitted as a ``{type:'tool'}`` frame ahead
of the terminal; a still-pending getter is cancelled loss-free (``asyncio.Queue.get``
pops only on success, so its item, if any, stays in the queue for ``_drain``).

``test_timeout_...`` is the genuine race repro and FAILS on unfixed ``sse.py`` (the
tool frame is dropped). The ``done``/``raised``-branch tests are invariant guards
(they pass on unfixed code via the getter-in-``done`` branch, but the §5 row mandates
covering every terminal path and they also prove settle + drain never DOUBLE-emit).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from ne.api.sse import _TIMEOUT_DETAIL, KEEPALIVE_FRAME, sse_frame, turn_event_stream
from ne.api.tool_transit import ToolTransit, TurnTransitChannel
from ne.contracts.runtime import AgentRunResult

from .conftest import run

#: The final governed decision that the race drops — a tier-2 external-write DENY
#: (the SCRAM the hero surface must always show, even as the last pre-terminal frame).
_SCRAM = ToolTransit(name="write.external.demo.send", tier=2, decision="deny", audit_seq=7)


async def _suspend_with_completed_getter(
    gen: AsyncIterator[bytes], channel: TurnTransitChannel
) -> None:
    """Drive ``gen`` into the exact hazardous state: suspended at a keep-alive yield
    with the published transit already POPPED by a COMPLETED getter task.

    After this returns the transit is held ONLY by the finished getter — the queue
    is empty, so a pre-terminal ``_drain()`` alone can no longer see it.
    """
    first = await gen.__anext__()
    assert first == KEEPALIVE_FRAME  # SUSPENDED at the keep-alive yield; getter pending
    channel.publish(_SCRAM)
    # Hand the loop to the outstanding getter task so it consumes (POPS) the transit.
    for _ in range(50):
        if channel.queue.empty():
            break
        await asyncio.sleep(0)
    assert channel.queue.empty(), "the getter task never consumed the transit"


def test_timeout_terminal_emits_transit_held_by_completed_getter() -> None:
    """RED on unfixed sse.py: the transit held by a completed getter is emitted as a
    ``tool`` frame BEFORE the timeout ``error`` terminal (previously dropped)."""

    async def _t() -> None:
        channel = TurnTransitChannel()
        hang = asyncio.Event()

        async def run_turn() -> AgentRunResult:
            await hang.wait()
            raise AssertionError("unreachable")

        gen = turn_event_stream(run_turn(), keepalive_s=0.05, timeout_s=0.5, transits=channel)
        await _suspend_with_completed_getter(gen, channel)
        await asyncio.sleep(0.6)  # burn the whole timeout budget while suspended
        frames = [f async for f in gen if f != KEEPALIVE_FRAME]
        assert frames == [
            sse_frame("tool", _SCRAM.sse_payload()),
            sse_frame("error", {"type": "error", "detail": _TIMEOUT_DETAIL}),
        ]

    run(_t())


def test_done_terminal_emits_transit_held_by_completed_getter() -> None:
    """Done-branch invariant guard: the transit precedes the ``token``/``done``
    terminal AND is emitted EXACTLY once (strict equality ⇒ no settle+drain double)."""

    async def _t() -> None:
        channel = TurnTransitChannel()
        gate = asyncio.Event()

        async def run_turn() -> AgentRunResult:
            await gate.wait()
            return AgentRunResult(session_id="s1", output="ans")

        gen = turn_event_stream(run_turn(), keepalive_s=0.05, timeout_s=30.0, transits=channel)
        await _suspend_with_completed_getter(gen, channel)
        gate.set()
        await asyncio.sleep(0)  # the turn completes while the stream is suspended
        frames = [f async for f in gen if f != KEEPALIVE_FRAME]
        assert frames == [
            sse_frame("tool", _SCRAM.sse_payload()),
            sse_frame("token", {"type": "token", "delta": "ans"}),
            sse_frame("done", {"type": "done", "session_id": "s1", "output": "ans"}),
        ]

    run(_t())


def test_raised_turn_error_terminal_emits_transit_held_by_completed_getter() -> None:
    """Raised-turn error-branch guard: 'any terminal frame' includes the non-timeout
    ``error`` path — the transit precedes ``error(detail='RuntimeError')``."""

    async def _t() -> None:
        channel = TurnTransitChannel()
        gate = asyncio.Event()

        async def run_turn() -> AgentRunResult:
            await gate.wait()
            raise RuntimeError("boom")

        gen = turn_event_stream(run_turn(), keepalive_s=0.05, timeout_s=30.0, transits=channel)
        await _suspend_with_completed_getter(gen, channel)
        gate.set()
        await asyncio.sleep(0)  # the turn raises while the stream is suspended
        frames = [f async for f in gen if f != KEEPALIVE_FRAME]
        assert frames == [
            sse_frame("tool", _SCRAM.sse_payload()),
            sse_frame("error", {"type": "error", "detail": "RuntimeError"}),
        ]

    run(_t())
