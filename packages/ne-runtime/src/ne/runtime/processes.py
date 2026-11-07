# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ProcessRegistry — the live-agent/job registry with lineage and atomic caps (NE-034).

A single in-process, in-memory registry tracking every live session, sub-agent,
and cron job, with parent/child lineage (``parent_id`` + ``depth``) and a bounded
finished-history ring. It is the SERVER-SIDE authority for the global
concurrency cap: :meth:`ProcessRegistry.register` checks the live count and
inserts atomically (no ``await`` between the check and the insert), so a recursive
agent cannot exceed the cap by lying — the count is the registry's, never the
spec's.

Concurrency model (ADR-001 ne-worker topology): the registry is SYNC and assumes
a single-process asyncio worker. Register/finish run to completion between
``await`` points, so the check-then-insert is atomic under cooperative
scheduling. A MULTI-WORKER registry (shared across processes) is deferred to
NE-070 alongside the multi-worker due-job claim — a single in-memory registry is
authoritative for this unit and for single-writer CI.
"""

from __future__ import annotations

import uuid
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from ne.runtime.errors import DispatchCapExceededError

#: The kinds of process tracked by the registry.
ProcessKind = Literal["session", "subagent", "cron-job"]

#: How many finished records to retain for introspection (bounded ring).
_FINISHED_HISTORY_MAXLEN = 256


@dataclass(frozen=True)
class ProcessRecord:
    """An immutable snapshot of one tracked process.

    ``finished_at`` / ``succeeded`` are ``None`` while the process is live and set
    once (as a NEW frozen record) when it finishes — the registry swaps the record
    rather than mutating it, so a handed-out record is a stable snapshot.
    """

    process_id: str
    kind: ProcessKind
    name: str
    principal_id: str
    parent_id: str | None
    depth: int
    started_at: datetime
    finished_at: datetime | None = None
    succeeded: bool | None = None


@dataclass
class ProcessRegistry:
    """In-memory live-process registry with lineage + atomic concurrency caps.

    SYNC by construction (single-process asyncio; see the module docstring). Live
    records are held in an insertion-ordered dict keyed by ``process_id``; finished
    records move to a bounded :class:`collections.deque`.
    """

    _live: dict[str, ProcessRecord] = field(default_factory=dict)
    _finished: deque[ProcessRecord] = field(
        default_factory=lambda: deque(maxlen=_FINISHED_HISTORY_MAXLEN)
    )

    def register(
        self,
        *,
        kind: ProcessKind,
        name: str,
        principal_id: str,
        parent_id: str | None = None,
        depth: int = 0,
        concurrency_cap: int | None = None,
    ) -> ProcessRecord:
        """Register a new live process, enforcing the concurrency cap ATOMICALLY.

        When ``concurrency_cap`` is not ``None`` and the current live count of
        ``kind`` is already at/over the cap, raises
        :class:`DispatchCapExceededError` (``reason='concurrency'``,
        ``scope='server'``) WITHOUT registering — the check and the insert run with
        no ``await`` between them, so the count can never be raced past the cap
        within the single-process asyncio worker.
        """
        if concurrency_cap is not None and self.active_count(kind) >= concurrency_cap:
            raise DispatchCapExceededError(
                f"global {kind} concurrency cap {concurrency_cap} reached",
                reason="concurrency",
                scope="server",
            )
        record = ProcessRecord(
            process_id=str(uuid.uuid4()),
            kind=kind,
            name=name,
            principal_id=principal_id,
            parent_id=parent_id,
            depth=depth,
            started_at=datetime.now(UTC),
        )
        self._live[record.process_id] = record
        return record

    def finish(self, process_id: str, *, succeeded: bool) -> None:
        """Move a live record to the bounded finished history (idempotent no-op if gone)."""
        record = self._live.pop(process_id, None)
        if record is None:
            return
        self._finished.append(
            ProcessRecord(
                process_id=record.process_id,
                kind=record.kind,
                name=record.name,
                principal_id=record.principal_id,
                parent_id=record.parent_id,
                depth=record.depth,
                started_at=record.started_at,
                finished_at=datetime.now(UTC),
                succeeded=succeeded,
            )
        )

    def live(self) -> tuple[ProcessRecord, ...]:
        """The currently-running records, in registration order."""
        return tuple(self._live.values())

    def active_count(self, kind: ProcessKind) -> int:
        """How many live records of ``kind`` exist right now."""
        return sum(1 for r in self._live.values() if r.kind == kind)

    def get(self, process_id: str) -> ProcessRecord | None:
        """The live record under ``process_id``, or the finished one, else ``None``."""
        live = self._live.get(process_id)
        if live is not None:
            return live
        for record in self._finished:
            if record.process_id == process_id:
                return record
        return None

    def finished(self) -> Iterator[ProcessRecord]:
        """Iterate the bounded finished-history ring (oldest first)."""
        return iter(self._finished)
