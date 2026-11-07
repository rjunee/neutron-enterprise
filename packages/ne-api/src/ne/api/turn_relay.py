# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ne-api's transport-agnostic turn relay seams — enqueue + frame read (NE-103c §4.3).

Two Protocols decouple the chat router from the durability engine:

* :class:`TurnEnqueuer` — hand a turn to the worker (``workflow_id=turn_id``).
* :class:`TurnFrameReader` — tail the worker's ordered frame stream for a turn.

The primary implementations wrap the SAME ``dbos.DBOSClient`` (one seam, two uses):
``enqueue_async`` onto the ``chat-turns`` queue and ``read_stream_async`` of the
per-workflow ``"frames"`` stream. ``dbos`` is imported LAZILY inside the impl
constructors (the ``_default_gateway`` precedent) so the ne-api import graph stays
decoration-free (import-linter forbids ne.api → ne.worker / ne.runtime.approvals_durable).

The Protocol seam IS the fallback insurance (design §4.3): a build-time stream-API
gap swaps the impl to a durable ``(turn_id, seq, frame_json)`` poll-tail table
WITHOUT touching the router, the relay loop, or a single SSE-byte assertion.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from ne.api.turn_wire import TurnFrame


def normalize_dbos_url(url: str) -> str:
    """Strip a SQLAlchemy ``+driver`` from a Postgres DSN for DBOS (risk #4).

    Compose wires the DBOS URLs in the ``postgresql+psycopg://`` house form (config
    parity with the platform engines). DBOS's client/launch expect a plain
    ``postgresql://`` DSN, so normalize here (the client constructor / WorkerSettings)
    — NEVER in compose (design §10).
    """
    prefix = "postgresql+psycopg://"
    if url.startswith(prefix):
        return "postgresql://" + url[len(prefix) :]
    return url


class TurnEnqueuer(Protocol):
    """Enqueue a chat turn as a durable workflow keyed by ``turn_id`` (idempotent)."""

    async def enqueue(self, turn_id: str, request_json: str) -> None: ...


class TurnFrameReader(Protocol):
    """Tail a turn's ordered frame stream from ``seq`` 0, de-duped by ``seq``."""

    def frames(self, turn_id: str) -> AsyncIterator[TurnFrame]: ...


class DbosTurnEnqueuer:
    """Enqueue ``chat.turn`` onto the ``chat-turns`` queue via ``DBOSClient``.

    ``workflow_id=turn_id`` is the DBOS idempotency key — an ne-api retry of the
    same enqueue can never double-run the turn (design §2.1).
    """

    def __init__(self, *, system_database_url: str, queue_name: str = "chat-turns") -> None:
        from dbos import DBOSClient

        self._client = DBOSClient(system_database_url=normalize_dbos_url(system_database_url))
        self._queue_name = queue_name

    async def enqueue(self, turn_id: str, request_json: str) -> None:
        from dbos import EnqueueOptions

        options: EnqueueOptions = {
            "queue_name": self._queue_name,
            "workflow_name": "chat.turn",
            "workflow_id": turn_id,
        }
        await self._client.enqueue_async(options, request_json)


class DbosAgentRunEnqueuer:
    """Enqueue canonical ``agent.run`` using its durable enqueue id."""

    def __init__(self, *, system_database_url: str, queue_name: str = "agent-runs") -> None:
        from dbos import DBOSClient

        self._client = DBOSClient(system_database_url=normalize_dbos_url(system_database_url))
        self._queue_name = queue_name

    async def enqueue(self, request: object) -> None:
        from dbos import EnqueueOptions

        from ne.runtime.run_workflow import RunWorkflowRequest

        value = RunWorkflowRequest.model_validate(request)
        options: EnqueueOptions = {
            "queue_name": self._queue_name,
            "workflow_name": "agent.run",
            "workflow_id": value.dispatch_id,
        }
        await self._client.enqueue_async(options, value.model_dump_json())

    async def enqueue_approval_resume(self, request: object) -> None:
        """Enqueue one persisted approval decision by its server-issued resume id."""

        from dbos import EnqueueOptions

        from ne.runtime.run_checkpoint import RunApprovalResumeRequest

        value = RunApprovalResumeRequest.model_validate(request)
        options: EnqueueOptions = {
            "queue_name": self._queue_name,
            "workflow_name": "agent.run.approval.resume",
            "workflow_id": value.resume_id,
        }
        await self._client.enqueue_async(options, value.model_dump_json())


#: The reader's DEAD-AIR FLOOR, and the only per-hop latency that is ours to set.
#:
#: A ``DBOSClient`` deliberately runs NO notification-listener thread (dbos
#: ``_client.py`` says so at the construction site: ``use_listen_notify=False``), so
#: a stream read that finds no value at the next offset cannot be woken by
#: LISTEN/NOTIFY — it waits out the system database's fallback poll interval and
#: looks again. That interval defaults to 1.0 s and is spent on EVERY idle gap in a
#: turn's frame stream, first hop included: a governed transit the worker wrote at
#: t+6 s is not readable until t+7 s. Measured end-to-end through the real writer
#: and this real reader at ``+1.006 s`` before, ``+0.244 s`` after
#: (``packages/ne-worker/tests/test_turn_frame_stream_visibility.py``).
#:
#: 0.25 s is a quarter of the dead air for four times the reads, and one read is a
#: single indexed primary-key SELECT against the DBOS ``streams`` table per WAITING
#: turn — bounded by the turn timeout and by the client's own two-connection pool.
#: It is NOT lowered further: the client's inner wait already ticks at 0.1 s, so
#: below that the extra reads buy nothing.
_STREAM_POLL_INTERVAL_S = 0.25


def _tighten_stream_poll_interval(client: object) -> None:
    """Set :data:`_STREAM_POLL_INTERVAL_S` on the client's system database.

    The interval is a ``SystemDatabase`` constructor argument that ``DBOSClient``
    does not forward, so there is no public seam for it. GUARDED on both attributes
    existing: a dbos upgrade that renames either leaves the vendored 1.0 s default
    in place — slower, never broken. A frame-stream tuning knob must never be able
    to fail a composition root.
    """
    system_database = getattr(client, "_sys_db", None)
    attribute = "_notification_listener_polling_interval_sec"
    if system_database is not None and hasattr(system_database, attribute):
        setattr(system_database, attribute, _STREAM_POLL_INTERVAL_S)


class DbosStreamTurnFrameReader:
    """Read a turn's ``"frames"`` DBOS workflow stream (native async generator).

    ``DBOSClient.read_stream_async`` yields values as they arrive and ends at
    stream close — so the reader needs NO ``to_thread`` wrapper. Each value is
    validated into a :class:`TurnFrame`; NON-terminal frames whose ``seq`` was
    already seen are dropped (at-least-once step re-execution tolerance, design
    §4.3). Terminal ``done``/``error`` frames pass REGARDLESS of seq
    (NE-TURN-PROGRESS): the worker's raised-turn ``error`` carries seq 0 behind
    the step's tool frames, and dropping it here would freeze the reader at the
    last hop — the same rule ``relay_event_stream`` applies, enforced at BOTH
    dedup sites so neither can silently swallow a turn's ending.
    """

    def __init__(self, *, system_database_url: str, stream_key: str = "frames") -> None:
        from dbos import DBOSClient

        self._client = DBOSClient(system_database_url=normalize_dbos_url(system_database_url))
        _tighten_stream_poll_interval(self._client)
        self._stream_key = stream_key

    async def frames(self, turn_id: str) -> AsyncIterator[TurnFrame]:
        last_seq = -1
        async for value in self._client.read_stream_async(turn_id, self._stream_key):
            frame = TurnFrame.model_validate(value)
            if frame.type in ("done", "error"):
                # A terminal always reaches the consumer; the SSE relay stops at
                # the first one, so a duplicate can never reach the browser twice.
                yield frame
                continue
            if frame.seq <= last_seq:
                continue
            last_seq = frame.seq
            yield frame
