# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The DECIDED ACL model in miniature (SPEC §5 NE-042).

Chunk ACLs are stamped ONCE and never rewritten; a membership revocation applied
by a later sync removes a group-chunk from a principal's match set purely via
expand() — proving revocation lag = sync interval and NO re-index.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

from ne.audit import PostgresAuditSink
from ne.permissions.expand import PrincipalExpander
from ne.permissions.models import (
    TENANT_ALL,
    MembershipEdge,
    PrincipalKind,
    PrincipalSnapshot,
    SyncedPrincipal,
)
from ne.permissions.store import PrincipalStore
from ne.permissions.sync import PrincipalSyncService

# Chunk ACLs stamped ONCE by the connector and NEVER rewritten below.
CHUNK_USER = ("u-ada",)
CHUNK_ENG = ("g-eng",)
CHUNK_OTHER = ("g-other",)
CHUNK_TENANT = (TENANT_ALL,)
CHUNK_SALES = ("g-sales",)


def _matches(expander: PrincipalExpander, principal: str, chunk_acl: tuple[str, ...]) -> bool:
    """Exactly the rule NE-043 will implement: any label overlap → visible."""
    return bool(expander.expand(principal) & set(chunk_acl))


class _Snap:
    def __init__(self, snapshot: PrincipalSnapshot) -> None:
        self._snapshot = snapshot

    @property
    def name(self) -> str:
        return "acl-model"

    async def fetch(self, state: Mapping[str, str]) -> PrincipalSnapshot:
        return self._snapshot


def _user(pid: str) -> SyncedPrincipal:
    return SyncedPrincipal(principal_id=pid, kind=PrincipalKind.USER)


def _group(pid: str) -> SyncedPrincipal:
    return SyncedPrincipal(principal_id=pid, kind=PrincipalKind.GROUP)


def test_decided_acl_model_acceptance(
    store: PrincipalStore, audit_sink: PostgresAuditSink, frozen_config: str
) -> None:
    service = PrincipalSyncService(store, audit_sink)
    expander = PrincipalExpander(store)

    # --- sync 1: ada is in g-eng -----------------------------------------
    asyncio.run(
        service.run_once(
            _Snap(
                PrincipalSnapshot(
                    principals=(_user("u-ada"), _group("g-eng")),
                    edges=(MembershipEdge(member_id="u-ada", group_id="g-eng"),),
                    replaced_groups=("g-eng",),
                    is_full=True,
                )
            )
        )
    )
    assert _matches(expander, "u-ada", CHUNK_USER)
    assert _matches(expander, "u-ada", CHUNK_ENG)
    assert _matches(expander, "u-ada", CHUNK_TENANT)  # known → tenant-all
    assert not _matches(expander, "u-ada", CHUNK_OTHER)

    # --- sync 2: ada REMOVED from g-eng (chunk stamps untouched) ----------
    asyncio.run(
        service.run_once(
            _Snap(
                PrincipalSnapshot(
                    principals=(_user("u-ada"), _group("g-eng")),
                    edges=(),  # no membership edge → ada dropped from g-eng
                    replaced_groups=("g-eng",),
                    is_full=True,
                )
            )
        )
    )
    # Revocation moved purely via expansion — the g-eng chunk is now invisible.
    assert not _matches(expander, "u-ada", CHUNK_ENG)
    assert _matches(expander, "u-ada", CHUNK_USER)  # her own chunk still matches

    # --- sync 3: ada ADDED to g-sales ------------------------------------
    asyncio.run(
        service.run_once(
            _Snap(
                PrincipalSnapshot(
                    principals=(_user("u-ada"), _group("g-eng"), _group("g-sales")),
                    edges=(MembershipEdge(member_id="u-ada", group_id="g-sales"),),
                    replaced_groups=("g-eng", "g-sales"),
                    is_full=True,
                )
            )
        )
    )
    assert _matches(expander, "u-ada", CHUNK_SALES)  # newly visible, no re-index


def test_unknown_principal_matches_only_named_chunks(store: PrincipalStore) -> None:
    expander = PrincipalExpander(store)
    assert _matches(expander, "stranger", ("stranger",))
    assert not _matches(expander, "stranger", CHUNK_TENANT)
    assert not _matches(expander, "stranger", CHUNK_ENG)
