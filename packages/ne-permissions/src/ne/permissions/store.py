# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The principal store — SQLAlchemy 2.0 Core, sync, dialect-neutral.

Three tables (SQLite-CI / Postgres-parity, mirroring ``ne.audit.store``
conventions: module-level :class:`sa.MetaData`, ``String`` / ``DateTime(timezone
=True)``, an idempotent ``ensure_permissions_schema`` create-all seam):

* ``ne_principals`` — users + groups, with ``removed_at`` tombstone (NULL = live).
* ``ne_group_memberships`` — direct ``member_id -> group_id`` edges (transitive
  closure is computed at query time by :class:`ne.permissions.expand.Principal
  Expander`, never materialized). Indexed both directions.
* ``ne_permissions_sync_state`` — opaque per-source sync cursors (Entra delta
  links etc.), keyed by an app-chosen string.

Single-deployment (follows from C1 self-host): NO tenant/org column anywhere — one deployment = one
customer. Group membership edges are always RECOMPUTED, never accreted
(delete-then-insert diff), so a member dropped from a group at source disappears
from the store on the next sync (the NE-060 recompute-not-accrete lesson) — that
is exactly what makes revocation-lag = sync-interval hold.
"""

from __future__ import annotations

import threading
from collections.abc import Collection, Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime

import sqlalchemy as sa

from ne.permissions.models import SyncedPrincipal

__all__ = ["PrincipalStore", "ensure_permissions_schema", "metadata"]

_IN_CHUNK = 500
"""Max IN-list size per statement (keeps SQLite's 999-variable limit safe)."""

metadata = sa.MetaData()

ne_principals = sa.Table(
    "ne_principals",
    metadata,
    sa.Column("principal_id", sa.String, primary_key=True),
    sa.Column("kind", sa.String, nullable=False),
    sa.Column("display_name", sa.String, nullable=False, default=""),
    sa.Column("source", sa.String, nullable=False),
    sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
)

ne_group_memberships = sa.Table(
    "ne_group_memberships",
    metadata,
    sa.Column("member_id", sa.String, nullable=False),
    sa.Column("group_id", sa.String, nullable=False),
    sa.Column("source", sa.String, nullable=False),
    sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint("member_id", "group_id"),
    sa.Index("ix_ne_group_memberships_member", "member_id"),
    sa.Index("ix_ne_group_memberships_group", "group_id"),
)

ne_permissions_sync_state = sa.Table(
    "ne_permissions_sync_state",
    metadata,
    sa.Column("key", sa.String, primary_key=True),
    sa.Column("value", sa.Text, nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)


def ensure_permissions_schema(engine: sa.Engine) -> None:
    """Create the principal-store tables. Idempotent (``create_all`` skips existing).

    The real migration runner is NE-069's; this is the create-all bootstrap seam
    tests and early deployments use (mirrors ``ne.audit.ensure_audit_schema``).
    """
    metadata.create_all(engine)


def _chunked(items: list[str], size: int = _IN_CHUNK) -> Iterator[list[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


class PrincipalStore:
    """Sync, dialect-neutral CRUD over principals + membership edges + sync state."""

    def __init__(self, engine: sa.Engine) -> None:
        self._engine = engine
        #: Transaction binding is PER-THREAD, never a plain instance attribute.
        #: One PrincipalStore instance is shared by the principal-sync cron job
        #: AND the query-time group expander (build_permissions_stack hands the
        #: same store to both), and the cron tick runs on its own worker thread.
        #: A plain ``self._bound`` made the sync's uncommitted snapshot-apply
        #: connection visible to every other thread for the whole sweep, so a
        #: concurrent ``read.knowledge.search`` could expand a principal's
        #: groups on that connection mid-delete-before-reinsert — a torn read
        #: feeding an authorization decision. Thread-local binding keeps
        #: ``transaction()`` atomicity for its owning thread and gives every
        #: other thread its own short-lived committed connection.
        self._local = threading.local()

    @property
    def _bound(self) -> sa.Connection | None:
        return getattr(self._local, "bound", None)

    @_bound.setter
    def _bound(self, conn: sa.Connection | None) -> None:
        self._local.bound = conn

    @property
    def engine(self) -> sa.Engine:
        return self._engine

    # -- transaction seam ----------------------------------------------------
    @contextmanager
    def transaction(self) -> Iterator[sa.Connection]:
        """Run a block on ONE connection so a whole snapshot applies atomically.

        Every store method invoked inside the block runs on this connection; on a
        clean exit the transaction commits, on any exception it rolls back the
        entire snapshot (``PrincipalSyncService`` relies on this atomicity).
        Re-entrant: a nested ``transaction()`` reuses the already-bound connection.
        """
        if self._bound is not None:
            yield self._bound
            return
        with self._engine.begin() as conn:
            self._bound = conn
            try:
                yield conn
            finally:
                self._bound = None

    @contextmanager
    def _connect(self) -> Iterator[sa.Connection]:
        """Yield the transaction-bound connection, or a fresh committing one."""
        if self._bound is not None:
            yield self._bound
        else:
            with self._engine.begin() as conn:
                yield conn

    # -- principals ----------------------------------------------------------
    def upsert_principals(
        self, principals: Iterable[SyncedPrincipal], *, source: str, now: datetime
    ) -> int:
        """Insert-or-update principals; clears ``removed_at`` on re-appearance.

        Returns the number of principals written (inserted + updated). A principal
        that reappears after a tombstone is revived (``removed_at`` set back to
        NULL) so it re-enters expansion.
        """
        rows = list(principals)
        if not rows:
            return 0
        by_id = {p.principal_id: p for p in rows}
        ids = list(by_id)
        with self._connect() as conn:
            existing: set[str] = set()
            for chunk in _chunked(ids):
                existing.update(
                    conn.execute(
                        sa.select(ne_principals.c.principal_id).where(
                            ne_principals.c.principal_id.in_(chunk)
                        )
                    ).scalars()
                )
            to_insert = [
                {
                    "principal_id": p.principal_id,
                    "kind": p.kind.value,
                    "display_name": p.display_name,
                    "source": source,
                    "first_seen_at": now,
                    "last_synced_at": now,
                    "removed_at": None,
                }
                for pid, p in by_id.items()
                if pid not in existing
            ]
            if to_insert:
                conn.execute(ne_principals.insert(), to_insert)
            for pid in existing:
                p = by_id[pid]
                conn.execute(
                    ne_principals.update()
                    .where(ne_principals.c.principal_id == pid)
                    .values(
                        kind=p.kind.value,
                        display_name=p.display_name,
                        source=source,
                        last_synced_at=now,
                        removed_at=None,
                    )
                )
        return len(ids)

    def mark_removed(self, principal_ids: Collection[str], *, now: datetime) -> int:
        """Tombstone principals AND cascade-delete their membership edges.

        Sets ``removed_at`` on each id and deletes EVERY ``ne_group_memberships``
        row where the id is the member OR the group — so a tombstoned principal
        (or group) drops out of expansion STRUCTURALLY, not by a runtime filter.
        Returns the number of principals tombstoned.
        """
        ids = list(dict.fromkeys(principal_ids))
        if not ids:
            return 0
        with self._connect() as conn:
            for chunk in _chunked(ids):
                conn.execute(
                    ne_principals.update()
                    .where(ne_principals.c.principal_id.in_(chunk))
                    .values(removed_at=now)
                )
                conn.execute(
                    ne_group_memberships.delete().where(
                        sa.or_(
                            ne_group_memberships.c.member_id.in_(chunk),
                            ne_group_memberships.c.group_id.in_(chunk),
                        )
                    )
                )
        return len(ids)

    # -- membership edges (recompute-not-accrete) ----------------------------
    def set_group_members(
        self, group_id: str, member_ids: Collection[str], *, source: str, now: datetime
    ) -> tuple[frozenset[str], frozenset[str]]:
        """REPLACE the full member set of ``group_id``; return ``(added, removed)`` ids.

        Delete-then-insert diff (recompute-not-accrete): a member no longer in
        ``member_ids`` has its edge removed. Returns the exact sets of member ids
        added and removed (empty/empty on unchanged input — idempotent).
        """
        desired = set(member_ids)
        with self._connect() as conn:
            existing = set(
                conn.execute(
                    sa.select(ne_group_memberships.c.member_id).where(
                        ne_group_memberships.c.group_id == group_id
                    )
                ).scalars()
            )
            to_add = desired - existing
            to_remove = existing - desired
            for chunk in _chunked(list(to_remove)):
                conn.execute(
                    ne_group_memberships.delete().where(
                        sa.and_(
                            ne_group_memberships.c.group_id == group_id,
                            ne_group_memberships.c.member_id.in_(chunk),
                        )
                    )
                )
            if to_add:
                conn.execute(
                    ne_group_memberships.insert(),
                    [
                        {
                            "member_id": m,
                            "group_id": group_id,
                            "source": source,
                            "synced_at": now,
                        }
                        for m in to_add
                    ],
                )
        return frozenset(to_add), frozenset(to_remove)

    def set_member_groups(
        self, member_id: str, group_ids: Collection[str], *, source: str, now: datetime
    ) -> tuple[frozenset[str], frozenset[str]]:
        """REPLACE the full group set of ``member_id``; return ``(added, removed)`` ids.

        Symmetric to :meth:`set_group_members` (recompute-not-accrete): returns the
        exact sets of group ids added and removed for ``member_id``.
        """
        desired = set(group_ids)
        with self._connect() as conn:
            existing = set(
                conn.execute(
                    sa.select(ne_group_memberships.c.group_id).where(
                        ne_group_memberships.c.member_id == member_id
                    )
                ).scalars()
            )
            to_add = desired - existing
            to_remove = existing - desired
            for chunk in _chunked(list(to_remove)):
                conn.execute(
                    ne_group_memberships.delete().where(
                        sa.and_(
                            ne_group_memberships.c.member_id == member_id,
                            ne_group_memberships.c.group_id.in_(chunk),
                        )
                    )
                )
            if to_add:
                conn.execute(
                    ne_group_memberships.insert(),
                    [
                        {
                            "member_id": member_id,
                            "group_id": g,
                            "source": source,
                            "synced_at": now,
                        }
                        for g in to_add
                    ],
                )
        return frozenset(to_add), frozenset(to_remove)

    # -- reads ---------------------------------------------------------------
    def direct_groups_of(self, member_ids: Collection[str]) -> dict[str, frozenset[str]]:
        """Return ``member_id -> frozenset(direct group ids)`` for the given members.

        A single batched SELECT (IN-lists chunked at 500). Members with no direct
        groups are simply absent from the returned mapping.
        """
        ids = list(dict.fromkeys(member_ids))
        if not ids:
            return {}
        acc: dict[str, set[str]] = {}
        with self._connect() as conn:
            for chunk in _chunked(ids):
                rows = conn.execute(
                    sa.select(
                        ne_group_memberships.c.member_id, ne_group_memberships.c.group_id
                    ).where(ne_group_memberships.c.member_id.in_(chunk))
                )
                for member_id, group_id in rows:
                    acc.setdefault(member_id, set()).add(group_id)
        return {m: frozenset(gs) for m, gs in acc.items()}

    def all_principal_ids(
        self, *, source: str | None = None, include_removed: bool = False
    ) -> set[str]:
        """Return every stored principal id (live only, unless ``include_removed``).

        Optionally restricted to principals last synced by ``source`` (the
        provenance column stamped by :meth:`upsert_principals`). The two filters
        compose (e.g. ``source=`` + ``include_removed=True``).
        """
        stmt = sa.select(ne_principals.c.principal_id)
        if not include_removed:
            stmt = stmt.where(ne_principals.c.removed_at.is_(None))
        if source is not None:
            stmt = stmt.where(ne_principals.c.source == source)
        with self._connect() as conn:
            return set(conn.execute(stmt).scalars())

    def display_names(self, principal_ids: Collection[str]) -> dict[str, tuple[str, bool]]:
        """Return ``principal_id -> (display_name, removed)`` for known ids.

        This is the read half of the governed principal-directory seam.  It is
        deliberately not a listing API: callers must already hold concrete ids
        derived from records they are authorized to read.  Tombstoned rows are
        included for historical attribution and carry ``removed=True``.  Blank
        names and unknown ids are absent so callers fall back to the identifier
        rather than fabricating or rendering an empty identity.
        """
        ids = list(dict.fromkeys(principal_ids))
        if not ids:
            return {}
        out: dict[str, tuple[str, bool]] = {}
        with self._connect() as conn:
            for chunk in _chunked(ids):
                rows = conn.execute(
                    sa.select(
                        ne_principals.c.principal_id,
                        ne_principals.c.display_name,
                        ne_principals.c.removed_at,
                    ).where(ne_principals.c.principal_id.in_(chunk))
                )
                for principal_id, display_name, removed_at in rows:
                    name = display_name.strip()
                    if name:
                        out[principal_id] = (name, removed_at is not None)
        return out

    def is_known(self, principal_id: str) -> bool:
        """True if ``principal_id`` exists and is NOT tombstoned (``removed_at`` NULL)."""
        with self._connect() as conn:
            row = conn.execute(
                sa.select(ne_principals.c.principal_id)
                .where(
                    sa.and_(
                        ne_principals.c.principal_id == principal_id,
                        ne_principals.c.removed_at.is_(None),
                    )
                )
                .limit(1)
            ).first()
        return row is not None

    # -- sync state ----------------------------------------------------------
    def get_sync_state(self, prefix: str) -> dict[str, str]:
        """Return the ``key -> value`` sync-state map for keys starting with ``prefix``."""
        with self._connect() as conn:
            rows = conn.execute(
                sa.select(ne_permissions_sync_state.c.key, ne_permissions_sync_state.c.value).where(
                    ne_permissions_sync_state.c.key.like(f"{prefix}%")
                )
            )
            return {key: value for key, value in rows}

    def set_sync_state(self, mapping: dict[str, str], *, now: datetime) -> None:
        """Upsert each ``key -> value`` sync-state entry (update-then-insert)."""
        with self._connect() as conn:
            for key, value in mapping.items():
                result = conn.execute(
                    ne_permissions_sync_state.update()
                    .where(ne_permissions_sync_state.c.key == key)
                    .values(value=value, updated_at=now)
                )
                if result.rowcount == 0:
                    conn.execute(
                        ne_permissions_sync_state.insert().values(
                            key=key, value=value, updated_at=now
                        )
                    )
