# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Framework-owned incremental cursor state + dedup ledger (NE-040).

Stdlib :mod:`sqlite3` ONLY — NO sqlalchemy dependency anywhere under
``ne.connect`` src. The durable tables are:

* ``cursors`` — the resumable incremental cursor per ``(source, source_id)``.
* ``dedup`` — a content-hash ledger per ``(source, source_id, record_id)`` so an
  unchanged record is skipped on re-sync.
* ``mapper_revisions`` — the last committed mapping-decision revision.
* ``connector_target_bindings`` — the credential-free target digest claimed by
  an otherwise empty source ledger.

``legacy_unbound_sources`` is a migration safety marker: it preserves evidence
that a pre-fold dedup cache was nonempty before that cache is rebuilt, preventing
the rebuilt empty table from being auto-adopted for a different target.

NE-040 P2 fold (delivered by NE-044): the dedup key gains ``source_id``. A
connector like M365 legitimately reuses the same ``record_id`` (a driveItem id)
across drives; keying dedup on ``(source, record_id)`` alone would let an item in
drive-A suppress a DIFFERENT item that happens to share its id in drive-B. The
key is therefore ``(source, source_id, record_id)`` — matching the record's own
identity triple. A pre-existing on-disk ledger written under the old two-column
schema is transparently DROPPED and recreated at open time: the dedup table is a
resync-safe cache, so the worst case is one idempotent re-write of every item on
the next run (never data loss, never over-grant).

Cursor comparison contract: cursors are OPAQUE strings compared
LEXICOGRAPHICALLY (ISO-8601 UTC timestamps satisfy this — NE-044/045 connectors
MUST emit lexicographically monotonic cursors or supply a custom mapper). The
M365 connector uses ``cursor_field=None`` and drives resumability off persisted
Graph ``deltaLink`` tokens + this dedup ledger instead.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cursors (
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    cursor TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (source, source_id)
);
CREATE TABLE IF NOT EXISTS dedup (
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    record_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (source, source_id, record_id)
);
CREATE TABLE IF NOT EXISTS mapper_revisions (
    source TEXT PRIMARY KEY,
    revision TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS connector_target_bindings (
    source TEXT PRIMARY KEY,
    target_digest TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS legacy_unbound_sources (
    source TEXT PRIMARY KEY
);
"""


#: SQLite busy-wait for this store. Stdlib :func:`sqlite3.connect` defaults to
#: FIVE seconds, which is far below the write-lock hold time of a real sync
#: (``begin_mapper_refresh`` keeps ``BEGIN IMMEDIATE`` open for the whole m365 /
#: salesforce run). The cross-process gate in :mod:`ne.connect.sync_gate` is what
#: actually prevents the collision; this is the second line of defence, so a
#: straggler waits rather than raising ``database is locked`` after five seconds.
BUSY_TIMEOUT_S = 30.0


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class ConnectorStateStore:
    """Durable cursor state + dedup ledger over a single sqlite3 connection.

    A single persistent connection is kept for the store's lifetime — required
    for the in-memory (``':memory:'``) database, whose contents vanish when the
    last connection closes.

    Thread safety: the cron tick body runs on worker threads (a7a4343d offloads
    it off DBOS's shared event loop), so the store may be touched from a thread
    other than the one that constructed it — and by two connector ticks in two
    threads at once. The connection is therefore opened with
    ``check_same_thread=False`` and EVERY access to it (and to the mutable
    ``_active_refresh*`` bookkeeping) is serialised under one reentrant lock,
    held for the whole logical operation — never per-statement — so a
    multi-statement transaction such as :meth:`claim_target_binding` can never
    interleave with another thread's statements on the same connection. A
    mapper refresh intentionally spans calls with its transaction open; the
    ``_active_refresh`` guard (unchanged) is what fails other writers closed
    for the duration, exactly as it does single-threaded.
    """

    def __init__(self, path: str | Path = ":memory:") -> None:
        #: The database path AS GIVEN — :func:`ne.connect.sync_gate.sync_lock_path`
        #: derives the cross-process sync gate from it, so a caller does not have to
        #: carry the path alongside the store it already holds.
        self.path = str(path)
        #: Reentrant because store methods call each other (e.g. ``set_cursor``
        #: → ``_commit_unless_refresh``, ``begin_mapper_refresh`` →
        #: ``load_mapper_revision``) while already holding the lock.
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_S, check_same_thread=False)
        self._active_refresh: tuple[str, str] | None = None
        self._active_refresh_prior: frozenset[tuple[str, str]] = frozenset()
        self._active_refresh_seen: set[tuple[str, str]] = set()
        with self._lock:
            self._migrate_legacy_dedup()
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def _migrate_legacy_dedup(self) -> None:
        """DROP a pre-fold ``dedup`` table lacking the ``source_id`` column.

        The dedup table is a resync-safe content-hash CACHE, not a system of
        record. A legacy two-column ledger (NE-040 ``(source, record_id)`` key)
        cannot be widened in place without an ambiguous ``source_id`` backfill, so
        it is dropped and recreated under the new schema — the only cost is one
        idempotent re-write of already-written items on the next run (never data
        loss, never an over-grant). Cursors are untouched.
        """
        with self._lock:
            cols = self._conn.execute("PRAGMA table_info(dedup)").fetchall()
            if cols and not any(col[1] == "source_id" for col in cols):
                # Preserve the fact that this source already had state before the
                # resync-safe cache is dropped.  Otherwise a later target-binding
                # claim could mistake the rebuilt, empty table for a new source and
                # silently adopt unrelated tenant/site/drive settings.
                self._conn.execute(
                    "CREATE TABLE IF NOT EXISTS legacy_unbound_sources (source TEXT PRIMARY KEY)"
                )
                self._conn.execute(
                    "INSERT OR IGNORE INTO legacy_unbound_sources (source) "
                    "SELECT DISTINCT source FROM dedup"
                )
                self._conn.execute("DROP TABLE dedup")
                self._conn.commit()

    def claim_target_binding(self, source: str, target_digest: str) -> bool:
        """Atomically bind an empty source ledger to one credential-free target.

        The first claim is admitted only when ``source`` has no cursor, dedup,
        mapper-revision, or migrated-legacy state.  A repeated exact claim is
        idempotent.  Every mismatch and every non-empty unbound legacy source
        fails closed; target replacement/reset is a separate governed action.

        Returns ``True`` for the first durable claim and ``False`` for an
        already-bound exact match.
        """
        if not source.strip() or not target_digest.strip():
            raise ValueError("connector target binding requires non-empty source and digest")
        with self._lock:
            if self._active_refresh is not None:
                raise RuntimeError("connector target binding cannot change during mapper refresh")

            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT target_digest FROM connector_target_bindings WHERE source = ?",
                    (source,),
                ).fetchone()
                if row is not None:
                    self._conn.rollback()
                    if str(row[0]) == target_digest:
                        return False
                    raise RuntimeError(
                        f"connector state target for source {source!r} does not match its binding"
                    )

                has_legacy_state = any(
                    self._conn.execute(query, (source,)).fetchone() is not None
                    for query in (
                        "SELECT 1 FROM cursors WHERE source = ? LIMIT 1",
                        "SELECT 1 FROM dedup WHERE source = ? LIMIT 1",
                        "SELECT 1 FROM mapper_revisions WHERE source = ? LIMIT 1",
                        "SELECT 1 FROM legacy_unbound_sources WHERE source = ? LIMIT 1",
                    )
                )
                if has_legacy_state:
                    self._conn.rollback()
                    raise RuntimeError(
                        f"connector state for source {source!r} is non-empty "
                        "and has no target binding"
                    )

                self._conn.execute(
                    "INSERT INTO connector_target_bindings "
                    "(source, target_digest, updated_at) VALUES (?, ?, ?)",
                    (source, target_digest, _now_iso()),
                )
                self._conn.commit()
                return True
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.rollback()
                raise

    def load_state(self, source: str) -> dict[str, str]:
        """Return ``{source_id: cursor}`` for every stored cursor of ``source``."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT source_id, cursor FROM cursors WHERE source = ?", (source,)
            ).fetchall()
        return {source_id: cursor for source_id, cursor in rows}

    def set_cursor(self, source: str, source_id: str, cursor: str) -> None:
        """UPSERT the cursor for ``(source, source_id)`` and commit."""
        with self._lock:
            self._reject_foreign_refresh_write(source)
            self._conn.execute(
                "INSERT INTO cursors (source, source_id, cursor, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(source, source_id) DO UPDATE SET cursor = excluded.cursor, "
                "updated_at = excluded.updated_at",
                (source, source_id, cursor, _now_iso()),
            )
            self._commit_unless_refresh(source)

    def load_mapper_revision(self, source: str) -> str | None:
        """Return the last successfully committed mapper revision for ``source``."""
        with self._lock:
            row = self._conn.execute(
                "SELECT revision FROM mapper_revisions WHERE source = ?", (source,)
            ).fetchone()
        return None if row is None else str(row[0])

    def begin_mapper_refresh(self, source: str, revision: str) -> bool:
        """Begin an atomic full-refresh state transaction when decisions changed.

        The revision comparison happens under ``BEGIN IMMEDIATE`` so two process
        roots sharing one state database cannot both admit the same refresh. A
        changed revision returns ``True`` and defers every cursor/dedup commit
        until :meth:`commit_mapper_refresh`; callers must roll back on failure.
        """
        if not revision:
            return False
        with self._lock:
            if self._active_refresh is not None:
                raise RuntimeError("connector state store already has an active mapper refresh")

            self._conn.execute("BEGIN IMMEDIATE")
            current = self.load_mapper_revision(source)
            if current == revision:
                self._conn.rollback()
                return False

            self._active_refresh = (source, revision)
            self._active_refresh_prior = frozenset(
                (str(row[0]), str(row[1]))
                for row in self._conn.execute(
                    "SELECT source_id, record_id FROM dedup WHERE source = ?", (source,)
                )
            )
            self._active_refresh_seen = set()
            return True

    def stale_mapper_refresh_identities(
        self, source: str, revision: str
    ) -> tuple[tuple[str, str], ...]:
        """Return prior identities not accepted during the exact active refresh."""
        with self._lock:
            if self._active_refresh != (source, revision):
                raise RuntimeError("connector mapper refresh lookup does not match active scope")
            return tuple(sorted(self._active_refresh_prior - self._active_refresh_seen))

    def commit_mapper_refresh(self, source: str, revision: str) -> None:
        """Commit the exact active refresh revision with its deferred state writes."""
        with self._lock:
            if self._active_refresh != (source, revision):
                raise RuntimeError("connector mapper refresh commit does not match active scope")
            self._conn.execute(
                "INSERT INTO mapper_revisions (source, revision, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(source) DO UPDATE SET revision = excluded.revision, "
                "updated_at = excluded.updated_at",
                (source, revision, _now_iso()),
            )
            self._conn.commit()
            self._clear_active_refresh()

    def rollback_mapper_refresh(self, source: str, revision: str) -> None:
        """Roll back the exact active refresh, preserving prior revision/state."""
        with self._lock:
            if self._active_refresh != (source, revision):
                raise RuntimeError("connector mapper refresh rollback does not match active scope")
            self._conn.rollback()
            self._clear_active_refresh()

    def invalidate_mapper_revision(self, source: str, revision: str) -> None:
        """Remove an exact committed revision so the next run must full-refresh.

        Guarded like every other mutating method: with ANY mapper refresh active
        this raises BEFORE touching the connection. The unconditional
        ``commit()`` below would otherwise make a sibling source's in-flight
        ``BEGIN IMMEDIATE`` refresh durable mid-flight — its partial dedup/cursor
        writes would commit, its later ``rollback_mapper_refresh`` would be a
        no-op, and ``is_unchanged`` would then skip records whose projection was
        rolled back, permanently. Every production caller invalidates only AFTER
        its own refresh committed (``refresh_committed=True`` paths), so its own
        refresh is never active here; an active refresh always belongs to a
        sibling and the write must fail closed.
        """
        with self._lock:
            if self._active_refresh is not None:
                raise RuntimeError(
                    "connector mapper revision invalidation during an active mapper refresh"
                )
            self._conn.execute(
                "DELETE FROM mapper_revisions WHERE source = ? AND revision = ?",
                (source, revision),
            )
            self._conn.commit()

    def is_unchanged(self, source: str, source_id: str, record_id: str, content_hash: str) -> bool:
        """True iff a dedup row exists for the id triple with an equal hash."""
        with self._lock:
            row = self._conn.execute(
                "SELECT content_hash FROM dedup "
                "WHERE source = ? AND source_id = ? AND record_id = ?",
                (source, source_id, record_id),
            ).fetchone()
            unchanged = row is not None and row[0] == content_hash
            if unchanged and self._active_refresh is not None and self._active_refresh[0] == source:
                # A retry after terminal-audit failure can legitimately find the
                # newly written revision in dedup. The source still emitted this
                # identity, so it is not stale even though no second mark is needed.
                self._active_refresh_seen.add((source_id, record_id))
            return unchanged

    def mark(self, source: str, source_id: str, record_id: str, content_hash: str) -> None:
        """UPSERT the dedup ledger row for ``(source, source_id, record_id)`` and commit."""
        with self._lock:
            self._reject_foreign_refresh_write(source)
            if self._active_refresh is not None and self._active_refresh[0] == source:
                self._active_refresh_seen.add((source_id, record_id))
            self._conn.execute(
                "INSERT INTO dedup (source, source_id, record_id, content_hash, updated_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(source, source_id, record_id) DO UPDATE SET "
                "content_hash = excluded.content_hash, updated_at = excluded.updated_at",
                (source, source_id, record_id, content_hash, _now_iso()),
            )
            self._commit_unless_refresh(source)

    def _reject_foreign_refresh_write(self, source: str) -> None:
        """Fail a cross-source write closed BEFORE its SQL touches the connection.

        Order is load-bearing: a mapper refresh holds ONE ``BEGIN IMMEDIATE`` open
        on the store's single connection, so any statement another source executes
        while it is active lands INSIDE that foreign transaction. The old shape
        (execute, then raise in ``_commit_unless_refresh``) left the failed
        write's row in the refresh's write set — ``commit_mapper_refresh`` then
        made a cursor advance durable for a sync that was recorded FAILED, and the
        next incremental run silently resumed past records the failed batch never
        wrote. Checking first means a rejected write leaves nothing behind.
        """
        active = self._active_refresh
        if active is not None and active[0] != source:
            raise RuntimeError("connector state write for a different source during mapper refresh")

    def _commit_unless_refresh(self, source: str) -> None:
        with self._lock:
            active = self._active_refresh
            if active is None:
                self._conn.commit()
                return
            if active[0] != source:
                raise RuntimeError(
                    "connector state write for a different source during mapper refresh"
                )

    def close(self) -> None:
        """Close the underlying connection (drops an in-memory database).

        Fails loud with a mapper refresh still active: no production path closes
        mid-refresh (every sync commits or rolls back before teardown, including
        on error), so an active refresh here always means some OTHER thread's
        refresh is in flight and closing would silently destroy its transaction
        while leaving its later ``rollback_mapper_refresh`` a misleading no-op.
        """
        with self._lock:
            if self._active_refresh is not None:
                raise RuntimeError("connector state store closed during an active mapper refresh")
            self._conn.close()

    def _clear_active_refresh(self) -> None:
        with self._lock:
            self._active_refresh = None
            self._active_refresh_prior = frozenset()
            self._active_refresh_seen = set()
