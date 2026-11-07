# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""PrincipalSyncService: atomic apply + config_hash-stamped audit emission.

Events land in the REAL ne-audit hash-chained sink (dev-dep over sqlite); no mock
past the audit seam.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any

import pytest
import sqlalchemy as sa

from ne.audit import PostgresAuditSink
from ne.audit.store import ne_audit_log
from ne.contracts.errors import ConfigHashNotFrozenError
from ne.permissions.models import (
    MembershipEdge,
    PrincipalKind,
    PrincipalSnapshot,
    SyncedPrincipal,
)
from ne.permissions.store import PrincipalStore
from ne.permissions.sync import PrincipalSyncService


class _StubSource:
    """A PrincipalSource returning a fixed snapshot (or raising)."""

    def __init__(self, snapshot: PrincipalSnapshot, name: str = "stub") -> None:
        self._snapshot = snapshot
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    async def fetch(self, state: Mapping[str, str]) -> PrincipalSnapshot:
        return self._snapshot


class _FailingSource:
    name = "boom"

    async def fetch(self, state: Mapping[str, str]) -> PrincipalSnapshot:
        raise RuntimeError("fetch exploded")


def _full_snapshot() -> PrincipalSnapshot:
    return PrincipalSnapshot(
        principals=(
            SyncedPrincipal(principal_id="u-ada", kind=PrincipalKind.USER),
            SyncedPrincipal(principal_id="g-eng", kind=PrincipalKind.GROUP),
        ),
        edges=(MembershipEdge(member_id="u-ada", group_id="g-eng"),),
        replaced_groups=("g-eng",),
        is_full=True,
    )


def _events(sink: PostgresAuditSink) -> list[sa.Row[Any]]:
    with sink.engine.connect() as conn:
        return list(
            conn.execute(
                sa.select(
                    ne_audit_log.c.action,
                    ne_audit_log.c.resource,
                    ne_audit_log.c.outcome,
                    ne_audit_log.c.config_hash,
                    ne_audit_log.c.actor,
                    ne_audit_log.c.payload,
                ).order_by(ne_audit_log.c.seq)
            )
        )


def test_full_run_emits_sync_and_membership_events(
    store: PrincipalStore, audit_sink: PostgresAuditSink, frozen_config: str
) -> None:
    service = PrincipalSyncService(store, audit_sink)
    report = asyncio.run(service.run_once(_StubSource(_full_snapshot())))

    assert report.principals_upserted == 2
    assert report.memberships_added == 1
    assert report.changed_members == ("u-ada",)

    rows = _events(audit_sink)
    sync_rows = [r for r in rows if r.action == "permissions.principal_sync"]
    member_rows = [r for r in rows if r.action == "permissions.membership_change"]
    assert len(sync_rows) == 1
    assert sync_rows[0].outcome == "success"
    assert sync_rows[0].resource == "principal-source://stub"
    assert sync_rows[0].actor == "system:ne-permissions"
    assert sync_rows[0].config_hash == frozen_config

    assert len(member_rows) == 1
    assert member_rows[0].resource == "principal://u-ada"
    detail = json.loads(member_rows[0].payload["detail"])
    assert detail == {"added": ["g-eng"], "removed": []}

    # Real hash chain verifies.
    assert asyncio.run(audit_sink.verify()).valid


def test_unchanged_member_emits_no_membership_event(
    store: PrincipalStore, audit_sink: PostgresAuditSink, frozen_config: str
) -> None:
    service = PrincipalSyncService(store, audit_sink)
    asyncio.run(service.run_once(_StubSource(_full_snapshot())))
    # Re-run identical snapshot → no membership change.
    report = asyncio.run(service.run_once(_StubSource(_full_snapshot())))
    assert report.changed_members == ()
    member_rows = [r for r in _events(audit_sink) if r.action == "permissions.membership_change"]
    assert len(member_rows) == 1  # only the first run's event


def test_full_snapshot_tombstones_absent_delta_does_not(
    store: PrincipalStore, audit_sink: PostgresAuditSink, frozen_config: str
) -> None:
    service = PrincipalSyncService(store, audit_sink)
    asyncio.run(service.run_once(_StubSource(_full_snapshot())))
    assert store.is_known("u-ada")

    # A FULL snapshot omitting u-ada tombstones it.
    full_empty = PrincipalSnapshot(
        principals=(SyncedPrincipal(principal_id="g-eng", kind=PrincipalKind.GROUP),),
        is_full=True,
    )
    asyncio.run(service.run_once(_StubSource(full_empty)))
    assert not store.is_known("u-ada")

    # Re-add, then a DELTA omitting u-ada leaves it alone.
    asyncio.run(service.run_once(_StubSource(_full_snapshot())))
    delta_empty = PrincipalSnapshot(principals=(), is_full=False)
    asyncio.run(service.run_once(_StubSource(delta_empty)))
    assert store.is_known("u-ada")


def test_full_sync_tombstones_only_its_own_sources_stale(
    store: PrincipalStore, audit_sink: PostgresAuditSink, frozen_config: str
) -> None:
    """An Entra FULL sync must not tombstone SCIM-provisioned principals (P2-9).

    Source-blind stale detection (``all_principal_ids() - present``) would tombstone
    every SCIM principal on an Entra full sync and cascade-delete their edges. With
    the stale set scoped to the syncing source, the Entra full sync tombstones only
    its OWN stale principals; the SCIM principals and their edges survive.
    """
    service = PrincipalSyncService(store, audit_sink)

    scim_full = PrincipalSnapshot(
        principals=(
            SyncedPrincipal(principal_id="u-scim", kind=PrincipalKind.USER),
            SyncedPrincipal(principal_id="g-scim", kind=PrincipalKind.GROUP),
        ),
        edges=(MembershipEdge(member_id="u-scim", group_id="g-scim"),),
        replaced_groups=("g-scim",),
        is_full=True,
    )
    entra_old = PrincipalSnapshot(
        principals=(SyncedPrincipal(principal_id="u-old", kind=PrincipalKind.USER),),
        is_full=True,
    )
    entra_new = PrincipalSnapshot(
        principals=(SyncedPrincipal(principal_id="u-ada", kind=PrincipalKind.USER),),
        is_full=True,
    )

    # (1) Seed SCIM principals + an edge, (2) seed an Entra principal, then (3) run an
    # Entra full sync that omits everything but its own snapshot.
    asyncio.run(service.run_once(_StubSource(scim_full, name="scim")))
    asyncio.run(service.run_once(_StubSource(entra_old, name="entra")))
    asyncio.run(service.run_once(_StubSource(entra_new, name="entra")))

    # SCIM principals AND their edge survive the Entra full sync.
    assert store.is_known("u-scim")
    assert store.is_known("g-scim")
    assert store.direct_groups_of(["u-scim"]) == {"u-scim": frozenset({"g-scim"})}

    # Entra's own stale principal IS tombstoned; the current Entra principal is live.
    assert not store.is_known("u-old")
    assert store.is_known("u-ada")


def test_failing_source_emits_error_event_then_raises(
    store: PrincipalStore, audit_sink: PostgresAuditSink, frozen_config: str
) -> None:
    service = PrincipalSyncService(store, audit_sink)
    with pytest.raises(RuntimeError, match="exploded"):
        asyncio.run(service.run_once(_FailingSource()))
    rows = _events(audit_sink)
    assert len(rows) == 1
    assert rows[0].action == "permissions.principal_sync"
    assert rows[0].outcome == "error"


def test_unfrozen_config_hash_raises_and_emits_nothing(
    store: PrincipalStore, audit_sink: PostgresAuditSink
) -> None:
    # No frozen_config fixture → config_hash not frozen.
    service = PrincipalSyncService(store, audit_sink)
    with pytest.raises(ConfigHashNotFrozenError):
        asyncio.run(service.run_once(_StubSource(_full_snapshot())))
    assert _events(audit_sink) == []
