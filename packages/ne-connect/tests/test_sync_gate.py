# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-CONNECT-SEEDLOCK: two connector syncs CANNOT write one state database at once.

The defect these cover: ``ne demo up`` seeded ``fixture`` and ``csv`` from the HOST
while the ne-worker container ran its own ``connect-sync-*`` crons against the SAME
``/connect/state.db`` inode. m365/salesforce hold ``BEGIN IMMEDIATE`` for the whole
run, SQLite admits one writer, and the stdlib busy timeout is five seconds — so the
host seed failed with ``database is locked`` on EVERY bring-up. These tests assert
the race is now impossible (the second sync waits), not merely retried.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ne.connect.state import BUSY_TIMEOUT_S, ConnectorStateStore
from ne.connect.sync_gate import (
    DEFAULT_SYNC_LOCK_TIMEOUT_S,
    ConnectorSyncLockTimeout,
    connector_sync_gate,
    sync_lock_path,
)


def test_lock_path_sits_next_to_the_state_database(tmp_path: Path) -> None:
    state = tmp_path / "connect" / "state.db"
    assert sync_lock_path(state) == tmp_path / "connect" / "state.db.synclock"


@pytest.mark.parametrize("ungated", ["", "  ", ":memory:", "file::memory:?cache=shared"])
def test_non_file_state_paths_are_ungated(ungated: str) -> None:
    """An in-memory store is private to its process — there is nothing to serialise."""
    assert sync_lock_path(ungated) is None
    assert sync_lock_path(None) is None


def test_gate_serialises_two_holders_in_one_process(tmp_path: Path) -> None:
    """The second holder cannot enter until the first leaves — no interleaving."""
    state = tmp_path / "state.db"
    order: list[str] = []

    async def hold(name: str, delay: float) -> None:
        async with connector_sync_gate(state, source=name, timeout_s=30.0):
            order.append(f"{name}:enter")
            await asyncio.sleep(delay)
            order.append(f"{name}:exit")

    async def _t() -> None:
        await asyncio.gather(hold("fixture", 0.2), hold("csv", 0.0))

    asyncio.run(_t())

    # Whatever the scheduling order, an enter is never sandwiched inside another
    # holder's enter/exit pair.
    assert order[0].endswith(":enter")
    assert order[1] == order[0].replace(":enter", ":exit")
    assert order[2].endswith(":enter")
    assert order[3] == order[2].replace(":enter", ":exit")
    assert {order[0].split(":")[0], order[2].split(":")[0]} == {"fixture", "csv"}


def test_gate_releases_on_exception(tmp_path: Path) -> None:
    """A failing sync must not strand the gate — the next sync still runs."""
    state = tmp_path / "state.db"

    async def _t() -> None:
        with pytest.raises(RuntimeError, match="sync blew up"):
            async with connector_sync_gate(state, source="fixture", timeout_s=5.0):
                raise RuntimeError("sync blew up")

        async with connector_sync_gate(state, source="csv", timeout_s=5.0):
            pass  # acquired, so the failed holder released

    asyncio.run(_t())


#: Run in a SEPARATE interpreter — the cross-process case is the real one (the
#: host seed and the in-container cron are different processes on one inode), and
#: ``flock`` is per open-file-description, so an in-process test cannot stand in
#: for it. A subprocess (not ``multiprocessing``) because this test module is not
#: importable by module path under ``packages/``.
_CHILD_PROGRAM = """
import asyncio, sys
from pathlib import Path
from ne.connect.sync_gate import connector_sync_gate

state, acquired, hold_s = sys.argv[1], sys.argv[2], float(sys.argv[3])

async def main():
    async with connector_sync_gate(state, source="peer", timeout_s=30.0):
        Path(acquired).write_text("held", encoding="utf-8")
        await asyncio.sleep(hold_s)

asyncio.run(main())
"""


def test_a_peer_process_holding_the_gate_makes_this_sync_wait(tmp_path: Path) -> None:
    """The host seed vs the in-container cron: one inode, two processes, no race."""
    state = tmp_path / "state.db"
    acquired = tmp_path / "acquired"
    child = subprocess.Popen(
        [sys.executable, "-c", _CHILD_PROGRAM, str(state), str(acquired), "1.0"]
    )
    try:
        deadline = time.monotonic() + 30.0
        while not acquired.exists():
            if time.monotonic() > deadline:  # pragma: no cover - child never started
                pytest.fail("the peer process never acquired the gate")
            time.sleep(0.05)

        waits: list[str] = []

        async def _t() -> float:
            started = time.monotonic()
            async with connector_sync_gate(
                state, source="fixture", timeout_s=30.0, on_wait=waits.append
            ):
                return time.monotonic() - started

        waited_for = asyncio.run(_t())

        # We waited for the peer rather than failing with `database is locked`...
        assert waited_for >= 0.5
        # ...and the wait was ANNOUNCED exactly once, so it cannot read as a hang.
        assert len(waits) == 1
        assert "waiting for another connector sync" in waits[0]
        assert "fixture" in waits[0]
    finally:
        try:
            child.wait(timeout=30)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            child.kill()
            child.wait(timeout=5)
    assert child.returncode == 0


def test_a_stuck_holder_raises_a_NAMED_timeout_not_database_is_locked(
    tmp_path: Path,
) -> None:
    """The whole point of INBOX 26: the failure must state its cause.

    ``OperationalError: database is locked`` is not a diagnosis. A holder that
    never lets go now surfaces as a timeout that names the file and the waiter.
    """
    state = tmp_path / "state.db"

    async def _t() -> ConnectorSyncLockTimeout:
        async with connector_sync_gate(state, source="m365", timeout_s=5.0):
            with pytest.raises(ConnectorSyncLockTimeout) as excinfo:
                async with connector_sync_gate(state, source="fixture", timeout_s=0.2):
                    pytest.fail("the gate was granted twice")
            return excinfo.value

    message = str(asyncio.run(_t()))
    assert "fixture" in message
    assert str(sync_lock_path(state)) in message
    assert "locked" not in message.lower()


def test_state_store_busy_timeout_is_far_above_the_stdlib_default(tmp_path: Path) -> None:
    """Defence in depth behind the gate: five seconds was never enough."""
    assert BUSY_TIMEOUT_S >= 30.0
    store = ConnectorStateStore(tmp_path / "state.db")
    try:
        assert store.path == str(tmp_path / "state.db")
    finally:
        store.close()


def test_state_store_waits_out_a_short_foreign_write_lock(tmp_path: Path) -> None:
    """A brief foreign writer is survived, where the 5s default would have failed.

    Uses a real second sqlite3 connection holding ``BEGIN IMMEDIATE`` — the exact
    shape ``begin_mapper_refresh`` produces — and proves the store's own busy-wait
    outlasts it instead of raising ``database is locked``.
    """
    state = tmp_path / "state.db"
    store = ConnectorStateStore(state)
    try:
        blocker = sqlite3.connect(str(state), timeout=30.0)
        blocker.execute("BEGIN IMMEDIATE")
        try:
            with pytest.raises(sqlite3.OperationalError):
                # Proof the lock is genuinely held: a 0.1s-timeout connection fails.
                impatient = sqlite3.connect(str(state), timeout=0.1)
                try:
                    impatient.execute("BEGIN IMMEDIATE")
                finally:
                    impatient.close()
        finally:
            blocker.rollback()
            blocker.close()
        # With the blocker gone the store writes normally.
        store.mark("fixture", "acme", "doc-1", "hash-1")
        assert store.is_unchanged("fixture", "acme", "doc-1", "hash-1")
    finally:
        store.close()


def test_default_gate_timeout_covers_a_full_connector_cron_period() -> None:
    """A wait longer than the ``*/15`` cadence that caused it means a wedged holder."""
    assert DEFAULT_SYNC_LOCK_TIMEOUT_S >= 15 * 60


def test_run_configured_sync_post_sync_runs_inside_the_gate_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-08-29 U13 review (F3): follow-up state-store work (the worker's m365
    Tier-2 graph projection) must run while the cross-process sync gate is still
    HELD — released-gate ordering let the graph's own long BEGIN IMMEDIATE
    mapper refresh overlap sibling connector syncs admitted by later ticks.
    Proven directly against the flock: inside ``post_sync`` a fresh fd cannot
    take the gate; after run_configured_sync returns it can.
    """
    import fcntl
    from types import SimpleNamespace

    from ne.connect.runner import run_configured_sync

    state_path = tmp_path / "state.db"
    lock_path = sync_lock_path(state_path)
    assert lock_path is not None
    events: list[str] = []

    def gate_is_free() -> bool:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDONLY, 0o666)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
            return True
        finally:
            os.close(fd)

    class _Outcome:
        report = "report-sentinel"

    async def _fake_run(_settings: object, **_kwargs: object) -> _Outcome:
        events.append("connector")
        return _Outcome()

    async def _post_sync() -> None:
        events.append("post_sync")
        assert not gate_is_free(), "post_sync ran with the sync gate already released"

    monkeypatch.setattr("ne.connect.runner._load_ref", lambda dotted: _fake_run)
    report = asyncio.run(
        run_configured_sync(
            "csv",
            writer=None,  # type: ignore[arg-type]
            state_store=SimpleNamespace(path=str(state_path)),  # type: ignore[arg-type]
            audit_sink=None,
            actor="test",
            settings="csv",
            post_sync=_post_sync,
        )
    )
    assert report == "report-sentinel"
    assert events == ["connector", "post_sync"], "post_sync must follow the connector body"
    assert gate_is_free(), "the gate must be released after the run"


def test_run_configured_sync_post_sync_failure_propagates_and_releases_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed follow-up fails the run (recorded failed upstream) and never
    leaks the gate hold."""
    from types import SimpleNamespace

    from ne.connect.runner import run_configured_sync

    state_path = tmp_path / "state.db"

    class _Outcome:
        report = "report-sentinel"

    async def _fake_run(_settings: object, **_kwargs: object) -> _Outcome:
        return _Outcome()

    async def _post_sync() -> None:
        raise RuntimeError("graph projection failed")

    monkeypatch.setattr("ne.connect.runner._load_ref", lambda dotted: _fake_run)
    with pytest.raises(RuntimeError, match="graph projection failed"):
        asyncio.run(
            run_configured_sync(
                "csv",
                writer=None,  # type: ignore[arg-type]
                state_store=SimpleNamespace(path=str(state_path)),  # type: ignore[arg-type]
                audit_sink=None,
                actor="test",
                settings="csv",
                post_sync=_post_sync,
            )
        )
    # The gate must be free again for the next sync.
    lock_path = sync_lock_path(state_path)
    assert lock_path is not None
    import fcntl

    fd = os.open(lock_path, os.O_CREAT | os.O_RDONLY, 0o666)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
