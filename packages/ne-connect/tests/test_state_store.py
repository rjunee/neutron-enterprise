# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ConnectorStateStore: cursor UPSERT + (source, source_id, record_id) dedup ledger."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from ne.connect import ConnectorStateStore


def test_cursor_upsert_roundtrip_and_load_state() -> None:
    store = ConnectorStateStore()
    store.set_cursor("src", "src/a", "2026-01-01T00:00:00Z")
    store.set_cursor("src", "src/b", "2026-01-02T00:00:00Z")
    store.set_cursor("src", "src/a", "2026-01-03T00:00:00Z")  # UPSERT overwrites
    assert store.load_state("src") == {
        "src/a": "2026-01-03T00:00:00Z",
        "src/b": "2026-01-02T00:00:00Z",
    }
    assert store.load_state("other") == {}
    store.close()


def test_persistence_across_reopen(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    store = ConnectorStateStore(db)
    store.set_cursor("src", "src/a", "cursor-1")
    store.mark("src", "src/a", "r1", "hash-1")
    store.close()

    reopened = ConnectorStateStore(db)
    assert reopened.load_state("src") == {"src/a": "cursor-1"}
    assert reopened.is_unchanged("src", "src/a", "r1", "hash-1") is True
    reopened.close()


def test_target_binding_claim_is_atomic_idempotent_and_persistent(tmp_path: Path) -> None:
    db = tmp_path / "target-binding.db"
    first = ConnectorStateStore(db)
    assert first.claim_target_binding("m365", "target-a") is True

    second = ConnectorStateStore(db)
    assert second.claim_target_binding("m365", "target-a") is False
    with pytest.raises(RuntimeError, match="does not match its binding"):
        second.claim_target_binding("m365", "target-b")
    assert first.claim_target_binding("m365", "target-a") is False

    first.close()
    second.close()


@pytest.mark.parametrize("legacy_kind", ["cursor", "dedup", "mapper"])
def test_target_binding_rejects_every_nonempty_unbound_legacy_state(
    legacy_kind: str,
) -> None:
    store = ConnectorStateStore()
    if legacy_kind == "cursor":
        store.set_cursor("m365", "delta:drive-1", "cursor-1")
    elif legacy_kind == "dedup":
        store.mark("m365", "drive-1:item-1", "item-1", "hash-1")
    else:
        assert store.begin_mapper_refresh("m365", "revision-1") is True
        store.commit_mapper_refresh("m365", "revision-1")

    with pytest.raises(RuntimeError, match="non-empty and has no target binding"):
        store.claim_target_binding("m365", "target-a")
    store.close()


def test_target_binding_is_scoped_to_exact_source() -> None:
    store = ConnectorStateStore()
    store.set_cursor("other", "cursor", "value")
    assert store.claim_target_binding("m365", "target-a") is True
    store.close()


def test_mapper_refresh_commits_revision_cursor_and_dedup_atomically(tmp_path: Path) -> None:
    db = tmp_path / "mapper-refresh.db"
    store = ConnectorStateStore(db)

    assert store.begin_mapper_refresh("m365", "revision-1") is True
    store.set_cursor("m365", "delta:drive-1", "cursor-1")
    store.mark("m365", "drive-1:item-1", "item-1", "hash-1")

    # A second connection cannot observe the uncommitted refresh state.
    observer = sqlite3.connect(str(db))
    assert observer.execute("SELECT * FROM mapper_revisions").fetchall() == []
    assert observer.execute("SELECT * FROM cursors").fetchall() == []
    assert observer.execute("SELECT * FROM dedup").fetchall() == []
    observer.close()

    store.commit_mapper_refresh("m365", "revision-1")
    assert store.load_mapper_revision("m365") == "revision-1"
    assert store.load_state("m365") == {"delta:drive-1": "cursor-1"}
    assert store.is_unchanged("m365", "drive-1:item-1", "item-1", "hash-1") is True
    assert store.begin_mapper_refresh("m365", "revision-1") is False
    store.close()


def test_failed_mapper_refresh_preserves_prior_revision_cursor_and_dedup(tmp_path: Path) -> None:
    db = tmp_path / "mapper-refresh-rollback.db"
    store = ConnectorStateStore(db)
    assert store.begin_mapper_refresh("salesforce", "revision-1") is True
    store.set_cursor("salesforce", "modstamp:Account", "cursor-1")
    store.mark("salesforce", "Account", "record-1", "hash-1")
    store.mark("salesforce", "Account", "record-2", "hash-1b")
    store.commit_mapper_refresh("salesforce", "revision-1")

    assert store.begin_mapper_refresh("salesforce", "revision-2") is True
    store.set_cursor("salesforce", "modstamp:Account", "cursor-2")
    store.mark("salesforce", "Account", "record-1", "hash-2")
    assert store.stale_mapper_refresh_identities("salesforce", "revision-2") == (
        ("Account", "record-2"),
    )
    store.rollback_mapper_refresh("salesforce", "revision-2")

    assert store.load_mapper_revision("salesforce") == "revision-1"
    assert store.load_state("salesforce") == {"modstamp:Account": "cursor-1"}
    assert store.is_unchanged("salesforce", "Account", "record-1", "hash-1") is True
    assert store.is_unchanged("salesforce", "Account", "record-1", "hash-2") is False
    # The changed revision is still pending and must be retried.
    assert store.begin_mapper_refresh("salesforce", "revision-2") is True
    store.rollback_mapper_refresh("salesforce", "revision-2")
    store.close()


def test_invalidated_committed_revision_forces_another_refresh() -> None:
    store = ConnectorStateStore()
    assert store.begin_mapper_refresh("m365", "revision-1") is True
    store.mark("m365", "drive:item", "item", "hash-1")
    store.commit_mapper_refresh("m365", "revision-1")
    store.invalidate_mapper_revision("m365", "revision-1")
    assert store.load_mapper_revision("m365") is None
    assert store.begin_mapper_refresh("m365", "revision-1") is True
    assert store.is_unchanged("m365", "drive:item", "item", "hash-1") is True
    assert store.stale_mapper_refresh_identities("m365", "revision-1") == ()
    store.rollback_mapper_refresh("m365", "revision-1")
    store.close()


def test_mapper_refresh_rejects_cross_source_state_write() -> None:
    store = ConnectorStateStore()
    assert store.begin_mapper_refresh("m365", "revision-1") is True
    with pytest.raises(RuntimeError, match="different source"):
        store.set_cursor("salesforce", "modstamp:Account", "cursor")
    store.rollback_mapper_refresh("m365", "revision-1")
    assert store.load_state("salesforce") == {}
    store.close()


def test_dedup_ledger() -> None:
    store = ConnectorStateStore()
    store.mark("src", "src/a", "r1", "hash-1")
    assert store.is_unchanged("src", "src/a", "r1", "hash-1") is True
    assert store.is_unchanged("src", "src/a", "r1", "hash-2") is False
    assert store.is_unchanged("src", "src/a", "unknown", "hash-1") is False
    # re-mark updates the hash
    store.mark("src", "src/a", "r1", "hash-2")
    assert store.is_unchanged("src", "src/a", "r1", "hash-2") is True
    store.close()


def test_same_record_id_under_two_source_ids_are_distinct_rows() -> None:
    # NE-040 P2 fold: a driveItem id reused across drives must NOT collide.
    store = ConnectorStateStore()
    store.mark("m365", "drive-A:item-1", "item-1", "hash-A")
    store.mark("m365", "drive-B:item-1", "item-1", "hash-B")
    assert store.is_unchanged("m365", "drive-A:item-1", "item-1", "hash-A") is True
    assert store.is_unchanged("m365", "drive-B:item-1", "item-1", "hash-B") is True
    # Cross-source_id: drive-A's hash does NOT satisfy drive-B's row.
    assert store.is_unchanged("m365", "drive-B:item-1", "item-1", "hash-A") is False
    store.close()


def test_memory_store_works_within_one_connection() -> None:
    store = ConnectorStateStore(":memory:")
    store.set_cursor("s", "s/x", "c")
    assert store.load_state("s") == {"s/x": "c"}
    store.close()


def test_legacy_two_column_dedup_table_is_dropped_and_rebuilt(tmp_path: Path) -> None:
    # A pre-fold ledger (source, record_id) is transparently rebuilt on open.
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        "CREATE TABLE dedup (source TEXT NOT NULL, record_id TEXT NOT NULL, "
        "content_hash TEXT NOT NULL, updated_at TEXT NOT NULL, "
        "PRIMARY KEY (source, record_id));"
        "CREATE TABLE cursors (source TEXT NOT NULL, source_id TEXT NOT NULL, "
        "cursor TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY (source, source_id));"
    )
    conn.execute("INSERT INTO dedup VALUES ('m365', 'r1', 'old-hash', '2026-01-01T00:00:00Z')")
    conn.execute("INSERT INTO cursors VALUES ('m365', 'delta:d', 'link-1', '2026-01-01T00:00:00Z')")
    conn.commit()
    conn.close()

    store = ConnectorStateStore(db)
    # dedup was rebuilt -> the legacy row is gone (cache, not system of record).
    assert store.is_unchanged("m365", "r1", "r1", "old-hash") is False
    # the new schema is in force (source_id column present -> triple works).
    store.mark("m365", "s1", "r1", "new-hash")
    assert store.is_unchanged("m365", "s1", "r1", "new-hash") is True
    # cursors are UNTOUCHED by the migration.
    assert store.load_state("m365") == {"delta:d": "link-1"}
    # Dropping the cache must not make a previously used source look empty.
    with pytest.raises(RuntimeError, match="non-empty and has no target binding"):
        store.claim_target_binding("m365", "target-a")
    store.close()


def test_store_is_usable_from_a_thread_other_than_its_constructor() -> None:
    # Regression for the a7a4343d cron offload: the tick body now runs on a
    # worker thread, so the store built on the loop thread is touched from a
    # DIFFERENT thread. Before check_same_thread=False this raised
    # sqlite3.ProgrammingError ("SQLite objects created in a thread can only
    # be used in that same thread") on first touch.
    store = ConnectorStateStore()  # constructed on THIS thread
    errors: list[BaseException] = []

    def offloaded_tick() -> None:
        try:
            assert store.claim_target_binding("m365", "target-a") is True
            store.set_cursor("m365", "delta:d", "link-1")
            store.mark("m365", "s1", "r1", "hash-1")
            assert store.is_unchanged("m365", "s1", "r1", "hash-1") is True
            assert store.load_state("m365") == {"delta:d": "link-1"}
        except BaseException as exc:  # surfaced to the test thread
            errors.append(exc)

    worker = threading.Thread(target=offloaded_tick, name="cron-tick-offload")
    worker.start()
    worker.join(timeout=30.0)
    assert not worker.is_alive()
    assert errors == []
    # The constructing thread still sees the worker thread's committed state.
    assert store.load_state("m365") == {"delta:d": "link-1"}
    assert store.claim_target_binding("m365", "target-a") is False
    store.close()


def test_two_concurrent_connector_ticks_in_two_threads_do_not_interleave() -> None:
    # m365 and salesforce ticks genuinely run concurrently on different worker
    # threads against ONE shared store. Each tick performs multi-statement work
    # (claim_target_binding runs a whole BEGIN IMMEDIATE transaction); the lock
    # must hold for the whole logical operation, so neither thread ever observes
    # "cannot start a transaction within a transaction", a ProgrammingError, or
    # the other tick's half-finished state.
    store = ConnectorStateStore()
    barrier = threading.Barrier(2, timeout=30.0)
    errors: list[BaseException] = []
    rounds = 200

    def tick(source: str) -> None:
        try:
            barrier.wait()
            assert store.claim_target_binding(source, f"target-{source}") is True
            for i in range(rounds):
                store.set_cursor(source, f"{source}/sub", f"cursor-{i:04d}")
                store.mark(source, f"{source}/sub", f"r{i}", f"hash-{i}")
                assert store.is_unchanged(source, f"{source}/sub", f"r{i}", f"hash-{i}") is True
                # Idempotent re-claim exercises the transaction path repeatedly
                # while the other thread is writing.
                assert store.claim_target_binding(source, f"target-{source}") is False
        except BaseException as exc:  # surfaced to the test thread
            errors.append(exc)

    threads = [
        threading.Thread(target=tick, args=(source,), name=f"tick-{source}")
        for source in ("m365", "salesforce")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60.0)
    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert store.load_state("m365") == {"m365/sub": f"cursor-{rounds - 1:04d}"}
    assert store.load_state("salesforce") == {"salesforce/sub": f"cursor-{rounds - 1:04d}"}
    store.close()


# ---------------------------------------------------------------------------
# 2026-08-29 U13/U14 review (F1/F2): the mutating methods must fail a foreign
# write CLOSED *before* any SQL lands inside another source's open mapper
# refresh. The pre-fix shape executed the statement first and only then raised
# in _commit_unless_refresh — the row was already in the foreign BEGIN
# IMMEDIATE's write set, so commit_mapper_refresh made a FAILED sync's cursor
# advance durable (silent corpus gap on the next incremental run). These tests
# drive begin_mapper_refresh against sibling writes — sequentially AND
# genuinely concurrently — which the pre-existing suite never did.
# ---------------------------------------------------------------------------


def test_rejected_foreign_write_leaves_nothing_in_the_committed_refresh(tmp_path: Path) -> None:
    """F1: the failed sibling write must NOT ride the refresh's commit."""
    path = tmp_path / "state.db"
    store = ConnectorStateStore(path)
    store.claim_target_binding("graph:m365", "digest-a")
    store.claim_target_binding("salesforce", "digest-b")
    assert store.begin_mapper_refresh("graph:m365", "rev-1") is True
    with pytest.raises(RuntimeError, match="different source"):
        store.set_cursor("salesforce", "modstamp:Account", "advanced-past-unwritten-records")
    with pytest.raises(RuntimeError, match="different source"):
        store.mark("salesforce", "modstamp:Account", "rec-1", "hash-1")
    # The refresh owner commits successfully — pre-fix this is the moment the
    # failed salesforce cursor advance became durable.
    store.commit_mapper_refresh("graph:m365", "rev-1")
    store.close()

    check = sqlite3.connect(path)
    try:
        assert (
            check.execute(
                "SELECT source, source_id, cursor FROM cursors WHERE source = 'salesforce'"
            ).fetchall()
            == []
        )
        assert (
            check.execute(
                "SELECT source, record_id FROM dedup WHERE source = 'salesforce'"
            ).fetchall()
            == []
        )
    finally:
        check.close()


def test_concurrent_sibling_write_during_active_refresh_fails_closed_without_trace(
    tmp_path: Path,
) -> None:
    """F1, concurrent form: a sibling THREAD's write during the refresh window.

    This is the deployed shape: the graph:m365 refresh holds its BEGIN IMMEDIATE
    on the tick thread while the next tick's salesforce sync writes from another
    thread. The write must raise (fail closed) and leave nothing for the
    refresh's commit to make durable.
    """
    path = tmp_path / "state.db"
    store = ConnectorStateStore(path)
    store.claim_target_binding("graph:m365", "digest-a")
    store.claim_target_binding("salesforce", "digest-b")

    refresh_open = threading.Event()
    sibling_done = threading.Event()
    outcomes: list[BaseException | str] = []

    def refresh_owner() -> None:
        assert store.begin_mapper_refresh("graph:m365", "rev-1") is True
        store.mark("graph:m365", "drive", "own-rec", "own-hash")
        refresh_open.set()
        assert sibling_done.wait(timeout=30.0)
        store.commit_mapper_refresh("graph:m365", "rev-1")

    def sibling_sync() -> None:
        assert refresh_open.wait(timeout=30.0)
        try:
            store.set_cursor("salesforce", "modstamp:Account", "advanced")
            outcomes.append("set_cursor unexpectedly succeeded")
        except RuntimeError as exc:
            outcomes.append(exc)
        try:
            store.mark("salesforce", "modstamp:Account", "rec-1", "hash-1")
            outcomes.append("mark unexpectedly succeeded")
        except RuntimeError as exc:
            outcomes.append(exc)
        finally:
            sibling_done.set()

    threads = [threading.Thread(target=refresh_owner), threading.Thread(target=sibling_sync)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60.0)
    assert not any(thread.is_alive() for thread in threads)
    assert len(outcomes) == 2
    assert all(isinstance(outcome, RuntimeError) for outcome in outcomes), outcomes
    store.close()

    check = sqlite3.connect(path)
    try:
        # The refresh's own write committed; the sibling's rejected writes did not.
        assert check.execute("SELECT source, record_id FROM dedup ORDER BY source").fetchall() == [
            ("graph:m365", "own-rec")
        ]
        assert check.execute("SELECT source FROM cursors").fetchall() == []
    finally:
        check.close()


def test_invalidate_mapper_revision_fails_closed_during_active_refresh(tmp_path: Path) -> None:
    """F2: invalidate must never commit a sibling's in-flight refresh.

    Pre-fix, invalidate_mapper_revision was the ONE mutating method with no
    _active_refresh guard: its unconditional commit() made the open refresh's
    partial dedup writes durable, the owner's later rollback_mapper_refresh was
    a no-op, and is_unchanged then skipped those records permanently.
    """
    path = tmp_path / "state.db"
    store = ConnectorStateStore(path)
    store.claim_target_binding("graph:m365", "digest-a")
    assert store.begin_mapper_refresh("graph:m365", "rev-1") is True
    store.mark("graph:m365", "drive", "rec-1", "hash-1")
    with pytest.raises(RuntimeError, match="invalidation during an active mapper refresh"):
        store.invalidate_mapper_revision("m365", "rev-x")
    # The refresh is untouched by the refused invalidate: its rollback still works.
    store.rollback_mapper_refresh("graph:m365", "rev-1")
    assert store.is_unchanged("graph:m365", "drive", "rec-1", "hash-1") is False
    store.close()

    check = sqlite3.connect(path)
    try:
        assert check.execute("SELECT source, record_id FROM dedup").fetchall() == []
    finally:
        check.close()


def test_invalidate_mapper_revision_still_works_with_no_refresh_active() -> None:
    """The guard rejects only the foreign-transaction window, not the error path
    every sync's refresh_committed=True branch actually uses (rollback/commit
    first, invalidate after)."""
    store = ConnectorStateStore()
    store.claim_target_binding("m365", "digest-a")
    assert store.begin_mapper_refresh("m365", "rev-1") is True
    store.commit_mapper_refresh("m365", "rev-1")
    store.invalidate_mapper_revision("m365", "rev-1")
    assert store.begin_mapper_refresh("m365", "rev-1") is True
    store.rollback_mapper_refresh("m365", "rev-1")
    store.close()


def test_close_fails_loud_during_active_refresh() -> None:
    """F2 (close leg): close() must not silently destroy an in-flight refresh.

    No production path closes mid-refresh (every sync commits or rolls back
    before teardown, including on error), so an active refresh at close time is
    always a lifecycle bug — loud beats a silent foreign-transaction rollback
    whose owner's own rollback_mapper_refresh then lies about having worked.
    """
    store = ConnectorStateStore()
    store.claim_target_binding("m365", "digest-a")
    assert store.begin_mapper_refresh("m365", "rev-1") is True
    with pytest.raises(RuntimeError, match="closed during an active mapper refresh"):
        store.close()
    store.rollback_mapper_refresh("m365", "rev-1")
    store.close()
