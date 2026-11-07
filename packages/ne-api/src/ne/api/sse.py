# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""SSE frame encoder + the turn-then-stream keep-alive multiplexer (spec §2).

v1 is TURN-THEN-STREAM (R7): ``DepartmentAgentSession.run`` is non-streaming, so
we open the event-stream immediately, emit a comment keep-alive every
``keepalive_s`` while the turn is in flight (the console's parser drops comment
lines — sse.ts:109), then on completion emit ONE ``token`` frame carrying the full
output as ``delta`` followed by a terminal ``done`` frame. Every payload carries
BOTH the SSE ``event:`` field AND a matching ``"type"`` key (the console routes on
``event:`` with a payload-``type`` fallback — client.ts:172-178). A successful
governed knowledge search adds zero or one ``citations`` frame after ``token`` and
before ``done``. A turn that outruns ``timeout_s`` (R9) is cancelled and emits one
terminal ``error`` frame — the pending approval row survives and stays actionable.
Post-stream failures are terminal ``error`` frames carrying safe messages / error
class names only (never tracebacks or backend URLs).

NE-137 (ADDITIVE): when the chat route passes a :class:`TurnTransitChannel`, the
multiplexer ALSO drains REAL C6 tool-transit events into ``tool`` frames
(``{type:'tool', name, tier, decision, audit_seq}`` + ``approval_id`` on pending),
interleaved BEFORE the terminal frame. The ``token``/``citations``/``done``/``error``
frames stay BYTE-IDENTICAL to the pre-NE-137 contract (proven by unchanged
``test_sse``/``test_hitl_e2e``): the ``tool`` frame is a new event type, never a
change to the existing ones.

NE-137-FU-SSE (race fix): the getter that already POPPED a transit when the turn
reaches its terminal is SETTLED (cancel-and-inspect) before EVERY terminal frame,
so the turn's LAST governed decision (often the final SCRAM deny) is emitted, not
silently dropped in ``finally``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Coroutine, Mapping
from typing import TYPE_CHECKING, Any

from ne.api.citation_capture import current_citations
from ne.contracts.runtime import AgentRunResult
from ne.gateway.errors import AirGapViolationError
from ne.runtime.errors import SessionDepartmentMismatchError

if TYPE_CHECKING:
    from ne.api.tool_transit import ToolTransit, TurnTransitChannel
    from ne.api.turn_wire import TurnFrame

#: A comment keep-alive frame; the console parser skips lines starting with ':'.
KEEPALIVE_FRAME = b": keep-alive\n\n"

_TIMEOUT_DETAIL = "turn timed out; any pending approval remains actionable from the approvals queue"

_AIR_GAP_DETAIL = "AIR_GAP is engaged; frontier models are unavailable; select a private-vllm model"

#: The cross-process relay wall-clock-timeout detail (design §4.4-5). The turn KEEPS
#: RUNNING durably — the pending approval row stays actionable and the completed
#: output lands in session history; only the browser leg closes.
_RELAY_TIMEOUT_DETAIL = (
    "turn still running; the approval queue and session history hold the outcome"
)


def sse_frame(event: str, payload: Mapping[str, object]) -> bytes:
    """Encode one SSE frame: ``event: <event>`` + single-line-JSON ``data:``."""
    data = json.dumps(payload, separators=(",", ":"))
    return f"event: {event}\ndata: {data}\n\n".encode()


def safe_error_detail(exc: BaseException) -> str:
    """A safe error detail: the library message for a known-safe class, else the
    error CLASS NAME only (never a repr / traceback / backend URL).

    Public because the cross-process frame relay (:mod:`ne.api.turn_wire`) reuses
    the SAME discipline when the worker maps a raised turn to an ``error`` frame,
    so browser-visible details stay byte-identical between inline and enqueue modes.
    """
    if isinstance(exc, SessionDepartmentMismatchError):
        return str(exc)
    if isinstance(exc, AirGapViolationError):
        # This exact gateway subtype earns a static actionable refusal. Do not
        # copy even its message: exception construction is public and a future
        # caller could put arbitrary provider text there. Its generic
        # EgressBlockedError parent may contain hosts/allowlists and remains
        # class-name-only via the fallback below.
        return _AIR_GAP_DETAIL
    return type(exc).__name__


async def turn_event_stream(
    run_turn: Coroutine[Any, Any, AgentRunResult],
    *,
    keepalive_s: float,
    timeout_s: float,
    transits: TurnTransitChannel | None = None,
    trace_id: str | None = None,
    link_plain_trace: bool = False,
    message_id: str | None = None,
    before_done: Callable[[], Awaitable[None]] | None = None,
) -> AsyncGenerator[bytes, None]:
    """Multiplex a non-streaming turn coroutine with comment keep-alives (spec §2.4).

    The task is created HERE (after the caller set ``TURN_PRINCIPAL``) so the
    ContextVar copies into it. While the turn runs, a keep-alive is emitted every
    ``keepalive_s``; once elapsed time exceeds ``timeout_s`` the turn is cancelled
    and a terminal ``error`` frame is emitted (R9). On completion a ``token``
    then a ``done`` frame carry the full output (R7); a raised turn becomes one
    terminal ``error`` frame with a safe detail.

    NE-137: when ``transits`` is set, a ``queue.get()`` getter rides the SAME
    ``asyncio.wait`` set (FIRST_COMPLETED); a completed getter yields a ``tool``
    frame (NOT a keep-alive tick) and is recreated. Before EVERY terminal frame
    (done/error, incl. the timeout branch) the outstanding getter is SETTLED
    (cancel-and-inspect — a completed getter already popped its transit, which the
    drain cannot see) and the queue is then DRAINED non-blocking, so no completed
    governed tool transit is ever lost at the turn boundary (NE-137-FU-SSE).

    NE-VAF-U4: the terminal ``done`` frame carries a ``verified`` key only when
    ``result.verified`` is populated (the router ROUTE'd deterministically);
    absent otherwise — byte-identical to pre-U4.
    """
    loop = asyncio.get_event_loop()
    started = loop.time()
    task: asyncio.Task[AgentRunResult] = asyncio.create_task(run_turn)
    getter: asyncio.Task[Any] | None = None

    def _drain() -> list[bytes]:
        frames: list[bytes] = []
        if transits is not None:
            while not transits.queue.empty():
                try:
                    transit = transits.queue.get_nowait()
                except asyncio.QueueEmpty:  # pragma: no cover - guarded by empty()
                    break
                frames.append(sse_frame("tool", transit.sse_payload()))
        return frames

    async def _settle_getter() -> ToolTransit | None:
        """Cancel-and-INSPECT the outstanding getter (NE-137-FU-SSE): a getter that
        already completed has POPPED its transit off the queue, so ``_drain`` can no
        longer see it — discarding the task would silently drop the turn's final
        governed decision (§0 law 5). Returns the completed transit so the caller
        emits it BEFORE any terminal frame; a still-pending getter is cancelled, and
        because ``asyncio.Queue.get`` only pops on SUCCESS its item (if any) stays in
        the queue for ``_drain``.
        """
        nonlocal getter
        if getter is None:
            return None
        settled, getter = getter, None
        if not settled.done():
            settled.cancel()
            with contextlib.suppress(BaseException):
                await settled
        if settled.cancelled() or settled.exception() is not None:
            return None
        return settled.result()

    try:
        while True:
            remaining = timeout_s - (loop.time() - started)
            if remaining <= 0.0:
                task.cancel()
                # NE-137-FU-SSE: a getter that completed in this window holds a
                # popped transit ``_drain`` cannot see; it is always the queue HEAD,
                # so emitting it first preserves FIFO order.
                settled = await _settle_getter()
                if settled is not None:
                    yield sse_frame("tool", settled.sse_payload())
                for frame in _drain():  # tool events precede the terminal error
                    yield frame
                yield sse_frame("error", {"type": "error", "detail": _TIMEOUT_DETAIL})
                return
            # Cap the wait by the remaining timeout budget so a short ``timeout_s``
            # is honored even when it is < ``keepalive_s`` — otherwise the wait
            # would block for the full keep-alive interval before the deadline
            # check ever runs, and the advertised per-turn timeout would not fire.
            wait_for = min(keepalive_s, remaining) if keepalive_s > 0.0 else remaining
            waitset: set[asyncio.Task[Any]] = {task}
            if transits is not None:
                if getter is None:
                    getter = asyncio.ensure_future(transits.queue.get())
                waitset.add(getter)
            done, _pending = await asyncio.wait(
                waitset, timeout=wait_for, return_when=asyncio.FIRST_COMPLETED
            )
            # A completed transit getter takes precedence: yield its ``tool`` frame,
            # recreate the getter, and loop (a transit is NOT a keep-alive tick).
            if getter is not None and getter in done:
                transit = getter.result()
                getter = None
                yield sse_frame("tool", transit.sse_payload())
                continue
            if task in done:
                break
            # Woke from the wait: if the timeout budget is now spent, the next
            # iteration's ``remaining <= 0`` check emits the terminal error;
            # otherwise this was a genuine keep-alive tick.
            if loop.time() - started < timeout_s:
                yield KEEPALIVE_FRAME

        # The turn finished: settle the getter, then drain — EVERY completed governed
        # transit precedes the terminal (done AND raised-turn error branches alike).
        settled = await _settle_getter()
        if settled is not None:
            yield sse_frame("tool", settled.sse_payload())
        for frame in _drain():
            yield frame

        try:
            result = task.result()
        except BaseException as exc:
            yield sse_frame("error", {"type": "error", "detail": safe_error_detail(exc)})
            return

        yield sse_frame("token", {"type": "token", "delta": result.output})
        citations = current_citations()
        if citations:
            yield sse_frame("citations", {"type": "citations", "citations": citations})
        if before_done is not None:
            try:
                await before_done()
            except BaseException as exc:
                yield sse_frame("error", {"type": "error", "detail": safe_error_detail(exc)})
                return
        done_payload: dict[str, Any] = {
            "type": "done",
            "session_id": result.session_id,
            "output": result.output,
        }
        if message_id is not None:
            done_payload["message_id"] = message_id
        # NE-VAF-U4: the router populates ``verified`` only when the turn
        # ROUTE'd deterministically; absent (byte-identical to pre-U4) otherwise.
        if result.verified is not None:
            done_payload["verified"] = result.verified.model_dump(mode="json")
        # NE-GA-G5: the grounding post-step populates ``grounded`` only on a
        # grounded-agent turn; absent (byte-identical) otherwise.
        if result.grounded is not None:
            done_payload["grounded"] = result.grounded.model_dump(mode="json")
        # The trace is a server-minted correlation for the STRUCTURAL claim,
        # never a browser inference. Plain unselected generation remains
        # byte-identical; a selected-model turn retains BD-15's trace link.
        if trace_id is not None and (
            link_plain_trace or result.verified is not None or result.grounded is not None
        ):
            done_payload["trace_id"] = trace_id
        yield sse_frame("done", done_payload)
    finally:
        # Covers a client abort (GeneratorExit) and the timeout path: never leave
        # the turn task orphaned. Await the cancellation so it settles before the
        # generator exits (PEP 525 allows awaiting — not yielding — in finally).
        if not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        if getter is not None and not getter.done():
            getter.cancel()
            with contextlib.suppress(BaseException):
                await getter


async def relay_event_stream(
    frames: AsyncIterator[TurnFrame],
    *,
    keepalive_s: float,
    timeout_s: float,
    trace_id: str | None = None,
) -> AsyncIterator[bytes]:
    """Multiplex a cross-process frame stream with comment keep-alives (NE-103c §4.4).

    ne-api is the SOLE SSE encoder: the worker emits typed :class:`TurnFrame`
    records, and this loop strips ``seq`` and encodes each via the SAME
    :func:`sse_frame` used inline — so browser bytes are byte-identical between
    inline and enqueue modes by construction. It:

    * de-duplicates NON-terminal frames by ``seq`` (a DBOS re-execution re-emitting
      frame N is a no-op; ``done.output`` self-heals any residual — spec §2.3);
    * emits a keep-alive every ``keepalive_s`` while awaiting the next frame;
    * stops after a terminal ``done``/``error`` frame — delivered REGARDLESS of
      seq (NE-TURN-PROGRESS: the raised-turn ``error`` carries seq 0 behind the
      step's tool frames; dropping it would freeze the reader at the last hop);
    * on a total wall-clock ``timeout_s`` with no terminal frame, emits an ``error``
      with the design §4.4-5 detail and STOPS READING WITHOUT cancelling the
      workflow (the durable turn outlives the browser — design §4.4-6);
    * on client abort (GeneratorExit) just stops and closes the reader — again
      never cancelling the durable workflow.
    """
    loop = asyncio.get_event_loop()
    started = loop.time()
    aiterator = frames.__aiter__()
    next_task: asyncio.Task[TurnFrame] | None = None
    last_seq = -1
    try:
        while True:
            remaining = timeout_s - (loop.time() - started)
            if remaining <= 0.0:
                yield sse_frame("error", {"type": "error", "detail": _RELAY_TIMEOUT_DETAIL})
                return
            if next_task is None:
                next_task = asyncio.ensure_future(aiterator.__anext__())
            wait_for = min(keepalive_s, remaining) if keepalive_s > 0.0 else remaining
            done, _pending = await asyncio.wait({next_task}, timeout=wait_for)
            if next_task not in done:
                # Woke from the wait without a frame: a keep-alive tick unless the
                # timeout budget is now spent (the next iteration emits the error).
                if loop.time() - started < timeout_s:
                    yield KEEPALIVE_FRAME
                continue
            try:
                frame = next_task.result()
            except StopAsyncIteration:
                # The stream closed with no terminal frame (e.g. a worker crash
                # before recovery). Stop the browser leg; the turn resumes durably.
                return
            except Exception as exc:
                # Response headers may already have left the process. Collapse a
                # protected materialization/read failure (and any other iterator
                # failure) to the same canonical safe terminal frame used by turn
                # execution; never let an exception truncate the SSE body or expose
                # its message/cause through the ASGI server.
                yield sse_frame("error", {"type": "error", "detail": safe_error_detail(exc)})
                return
            finally:
                next_task = None
            # NE-TURN-PROGRESS: a TERMINAL frame is delivered regardless of seq.
            # The worker's raised-turn ``error`` keeps seq 0 (a raised step cannot
            # report how many step-context ``tool`` frames it wrote), so after N
            # tool frames the monotonic dedup would silently swallow the terminal
            # and the reader would freeze at "hop N" forever — the exact failure
            # this repo keeps re-shipping. Duplicate-terminal safety is structural:
            # the loop returns at the FIRST terminal, so a re-delivered done/error
            # can never reach the browser twice.
            if frame.type in ("done", "error"):
                payload = frame.sse_payload()
                if (
                    frame.type == "done"
                    and trace_id is not None
                    and (frame.verified is not None or frame.grounded is not None)
                ):
                    payload["trace_id"] = trace_id
                yield sse_frame(frame.type, payload)
                return
            if frame.seq <= last_seq:
                continue  # duplicate (DBOS re-exec / at-least-once) — drop it
            last_seq = frame.seq
            yield sse_frame(frame.type, frame.sse_payload())
    finally:
        if next_task is not None and not next_task.done():
            next_task.cancel()
            with contextlib.suppress(BaseException):
                await next_task
        aclose = getattr(frames, "aclose", None)
        if aclose is not None:
            with contextlib.suppress(BaseException):
                await aclose()
