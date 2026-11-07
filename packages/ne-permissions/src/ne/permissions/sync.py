# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""PrincipalSyncService — apply a source snapshot to the store, atomically + audited.

``run_once`` fetches one :class:`~ne.permissions.models.PrincipalSnapshot`, applies
it inside a single :meth:`PrincipalStore.transaction` (so the whole snapshot
commits or rolls back as a unit), and emits config_hash-stamped audit events:

* exactly ONE ``permissions.principal_sync`` event per run (success outcome, or
  error outcome then re-raise on failure);
* one ``permissions.membership_change`` event per member whose DIRECT group set
  changed, carrying the sorted added/removed group ids.

There is no scheduler here — ``run_once`` is the whole surface; the hosting
process (NE-034 cron) schedules it every ``sync_interval_seconds`` (that interval
IS the revocation lag). config_hash stamping is non-optional: if the process hash
is not frozen, ``ConfigHashNotFrozenError`` propagates and nothing is emitted.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime

from ne.contracts.governance import AuditSink
from ne.events import new_audit_event
from ne.permissions.models import SyncReport
from ne.permissions.sources import PrincipalSource
from ne.permissions.store import PrincipalStore
from ne.schema import AuditOutcome

__all__ = ["PrincipalSyncService"]


class PrincipalSyncService:
    """Drive a :class:`PrincipalSource` into the store with atomic apply + audit."""

    def __init__(
        self, store: PrincipalStore, sink: AuditSink, *, actor: str = "system:ne-permissions"
    ) -> None:
        self._store = store
        self._sink = sink
        self._actor = actor

    async def run_once(self, source: PrincipalSource) -> SyncReport:
        """Fetch + apply one snapshot; emit the sync + membership_change events."""
        name = source.name
        resource = f"principal-source://{name}"
        try:
            report = await self._apply(source)
        except Exception as exc:  # emit the error-outcome sync event, then re-raise
            await self._sink.append(
                new_audit_event(
                    action="permissions.principal_sync",
                    resource=resource,
                    outcome=AuditOutcome.error,
                    actor=self._actor,
                    detail=f"{type(exc).__name__}: {exc}",
                )
            )
            raise

        # (a) exactly one sync event per run.
        await self._sink.append(
            new_audit_event(
                action="permissions.principal_sync",
                resource=resource,
                outcome=AuditOutcome.success,
                actor=self._actor,
                detail=json.dumps(
                    {
                        "source": report.source,
                        "is_full": report.is_full,
                        "principals_upserted": report.principals_upserted,
                        "principals_removed": report.principals_removed,
                        "memberships_added": report.memberships_added,
                        "memberships_removed": report.memberships_removed,
                    },
                    sort_keys=True,
                ),
            )
        )
        return report

    async def _apply(self, source: PrincipalSource) -> SyncReport:
        store = self._store
        name = source.name
        state = store.get_sync_state(f"{name}:")
        snapshot = await source.fetch(state)
        now = datetime.now(UTC)

        # Group edges by group and by member for the two REPLACE paths.
        members_of: dict[str, list[str]] = defaultdict(list)
        groups_of: dict[str, list[str]] = defaultdict(list)
        for edge in snapshot.edges:
            members_of[edge.group_id].append(edge.member_id)
            groups_of[edge.member_id].append(edge.group_id)

        # member_id -> (added group ids, removed group ids).
        added_groups: dict[str, set[str]] = defaultdict(set)
        removed_groups: dict[str, set[str]] = defaultdict(set)
        memberships_added = 0
        memberships_removed = 0

        with store.transaction():
            upserted = store.upsert_principals(snapshot.principals, source=name, now=now)

            for group_id in snapshot.replaced_groups:
                added, removed = store.set_group_members(
                    group_id, members_of.get(group_id, ()), source=name, now=now
                )
                memberships_added += len(added)
                memberships_removed += len(removed)
                for member_id in added:
                    added_groups[member_id].add(group_id)
                for member_id in removed:
                    removed_groups[member_id].add(group_id)

            for member_id in snapshot.replaced_members:
                added, removed = store.set_member_groups(
                    member_id, groups_of.get(member_id, ()), source=name, now=now
                )
                memberships_added += len(added)
                memberships_removed += len(removed)
                added_groups[member_id] |= added
                removed_groups[member_id] |= removed

            removed_count = store.mark_removed(snapshot.removed_principal_ids, now=now)

            if snapshot.is_full:
                present = {p.principal_id for p in snapshot.principals}
                # Stale set is scoped to THIS source: a full Entra sync must never
                # tombstone SCIM-provisioned principals (source-blind stale detection
                # cascade-deleted other sources' edges — bulletproofing P2-9).
                stale = store.all_principal_ids(source=name) - present
                removed_count += store.mark_removed(stale, now=now)

            store.set_sync_state(dict(snapshot.state), now=now)

        changed_members = sorted(set(added_groups) | set(removed_groups))

        # (b) one membership_change event per changed member.
        for member_id in changed_members:
            await self._sink.append(
                new_audit_event(
                    action="permissions.membership_change",
                    resource=f"principal://{member_id}",
                    outcome=AuditOutcome.success,
                    actor=self._actor,
                    detail=json.dumps(
                        {
                            "added": sorted(added_groups.get(member_id, set())),
                            "removed": sorted(removed_groups.get(member_id, set())),
                        }
                    ),
                )
            )

        return SyncReport(
            source=name,
            is_full=snapshot.is_full,
            principals_upserted=upserted,
            principals_removed=removed_count,
            memberships_added=memberships_added,
            memberships_removed=memberships_removed,
            changed_members=tuple(changed_members),
        )
