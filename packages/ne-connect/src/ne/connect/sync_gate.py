# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Cross-process gate that makes two concurrent connector syncs IMPOSSIBLE.

WHY THIS EXISTS (NE-CONNECT-SEEDLOCK, 2026-08-27). Every ``ne demo up`` ended with::

    fixture: ERROR OperationalError: database is locked [code=SQLITE_BUSY]
    csv:     ERROR OperationalError: database is locked [code=SQLITE_BUSY]

and it was NOT flaky — it was deterministic, for a structural reason:

* ``ne-worker`` runs the seeded ``connect-sync-<source>`` crons INSIDE the
  container against ``/connect/state.db``.
* ``ne demo up`` runs the host-side seed (``ne connect sync`` over ``fixture``
  then ``csv``) against the SAME file through the ``workdir/connect`` bind mount
  — same inode, two processes.
* The m365 and salesforce syncs open ``BEGIN IMMEDIATE`` for the WHOLE run
  (:meth:`ne.connect.state.ConnectorStateStore.begin_mapper_refresh` defers every
  cursor/dedup commit to ``commit_mapper_refresh``). On the CPU-only demo TEI
  server that write lock is held for MINUTES.
* SQLite's rollback journal gives one writer at a time and stdlib
  :func:`sqlite3.connect` defaults to a **5 second** busy timeout, so the host
  seed lost the race every time — and m365 never failed because m365 was the
  lock HOLDER.

WAL would NOT have fixed this. WAL buys reader/writer concurrency; this is
writer-vs-writer, which WAL serialises exactly as the rollback journal does. The
fix has to remove the concurrency, not relabel it — so a connector sync now takes
an advisory :func:`fcntl.flock` on a lock file that lives NEXT TO the state
database. Host and container reach the same inode through the ``/connect`` bind
mount, so the second sync WAITS instead of racing, and a genuinely stuck holder
surfaces as :class:`ConnectorSyncLockTimeout` — a named, diagnosable failure —
rather than a bare ``OperationalError``.

``flock`` is advisory and per open-file-description, so unrelated readers (the
``mode=ro`` opens in :mod:`ne.api.connect_status`) are untouched: they never take
the gate and are never blocked by it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import AsyncIterator

try:  # pragma: no cover - POSIX everywhere we deploy; a soft import keeps imports safe
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None  # type: ignore[assignment]

_LOG = logging.getLogger(__name__)

#: Suffix appended to the connector state database path to name its gate file.
LOCK_SUFFIX = ".synclock"

#: How long a sync waits for a peer sync to finish before giving up. One full
#: connector-cron period (``*/15``): waiting longer than the cadence that
#: produced the contention means the holder is wedged, not merely slow.
DEFAULT_SYNC_LOCK_TIMEOUT_S = 900.0

#: Poll cadence while waiting. ``flock`` has no async form, so the gate polls
#: non-blockingly and sleeps on the event loop rather than blocking it.
_POLL_INTERVAL_S = 0.5

#: State paths that name no file on disk — nothing to serialise between processes.
_NON_FILE_PATHS = frozenset({"", ":memory:"})


class ConnectorSyncLockTimeout(TimeoutError):
    """Another connector sync held the state-database gate past the timeout.

    Deliberately NOT an ``OperationalError``: "database is locked" is not a
    diagnosis, and getting behind one cost a whole deploy cycle. This names the
    file, the waiting source, and how long it waited.
    """


def sync_lock_path(state_path: str | Path | None) -> Path | None:
    """The gate file for ``state_path``, or ``None`` when there is nothing to gate.

    ``None``/empty/``:memory:`` state stores are per-process by construction (an
    in-memory database is not shared with anybody), so they are ungated.
    """
    text = "" if state_path is None else str(state_path).strip()
    if text in _NON_FILE_PATHS or text.startswith("file::memory:") or ":memory:" in text:
        return None
    path = Path(text)
    return path.with_name(path.name + LOCK_SUFFIX)


def _open_gate_fd(path: Path) -> int:
    """Open (creating if needed) the gate file and return its fd.

    ``O_RDONLY`` is deliberate: ``flock`` locks the open file description and
    does not care about the access mode, so the gate works even for a role that
    only has read access to the lock file. The best-effort ``chmod`` matters on
    the demo box, where the HOST user creates ``workdir/connect`` and the
    containers run as uid 10001 — whoever creates the file first must leave it
    openable by the other.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDONLY, 0o666)
    # A foreign owner (the other uid got there first) leaves the existing mode
    # in place; it is already 0666 in that case, so there is nothing to repair.
    with contextlib.suppress(OSError):
        os.chmod(path, 0o666)
    return fd


@asynccontextmanager
async def connector_sync_gate(
    state_path: str | Path | None,
    *,
    source: str,
    timeout_s: float = DEFAULT_SYNC_LOCK_TIMEOUT_S,
    on_wait: Callable[[str], None] | None = None,
) -> AsyncIterator[None]:
    """Hold the cross-process connector-sync gate for ``state_path``.

    A no-op for an ungated state path (see :func:`sync_lock_path`) and on any
    platform without :mod:`fcntl`. When another process holds the gate, ``on_wait``
    is called ONCE with a human-readable line (the CLI prints it, so contention is
    visible rather than looking like a hang) and the wait is bounded by
    ``timeout_s``, after which :class:`ConnectorSyncLockTimeout` is raised.
    """
    path = sync_lock_path(state_path)
    if path is None or fcntl is None:
        yield
        return

    fd = _open_gate_fd(path)
    try:
        deadline = time.monotonic() + timeout_s
        announced = False
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                # EACCES/EAGAIN — held by a peer sync. Anything else from flock on
                # a valid fd is not a thing we can distinguish usefully here.
                pass
            if not announced:
                announced = True
                message = (
                    f"{source}: waiting for another connector sync to release {path} "
                    f"(up to {timeout_s:.0f}s) — the connector state database admits "
                    "exactly one writer"
                )
                _LOG.warning("%s", message)
                if on_wait is not None:
                    on_wait(message)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ConnectorSyncLockTimeout(
                    f"{source}: another connector sync still held {path} after "
                    f"{timeout_s:.0f}s — the holder is stuck, not merely slow"
                )
            await asyncio.sleep(min(_POLL_INTERVAL_S, remaining))
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
