# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""PrincipalStore: upsert/tombstone-cascade, recompute edges, batching, atomicity."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import sqlalchemy as sa

from ne.permissions.models import PrincipalKind, SyncedPrincipal
from ne.permissions.store import (
    PrincipalStore,
    ensure_permissions_schema,
    ne_group_memberships,
)

NOW = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)
LATER = datetime(2026, 7, 6, 13, 0, tzinfo=UTC)


def _user(pid: str, name: str = "") -> SyncedPrincipal:
    return SyncedPrincipal(principal_id=pid, kind=PrincipalKind.USER, display_name=name)


def _group(pid: str) -> SyncedPrincipal:
    return SyncedPrincipal(principal_id=pid, kind=PrincipalKind.GROUP)


def test_ensure_schema_idempotent(engine: sa.Engine) -> None:
    ensure_permissions_schema(engine)  # second call must not raise
    ensure_permissions_schema(engine)


def test_upsert_updates_and_revives(store: PrincipalStore) -> None:
    store.upsert_principals([_user("u1", "Old")], source="entra", now=NOW)
    assert store.is_known("u1")

    store.mark_removed(["u1"], now=NOW)
    assert not store.is_known("u1")

    # Re-appearance updates display_name and clears removed_at.
    store.upsert_principals([_user("u1", "New")], source="entra", now=LATER)
    assert store.is_known("u1")
    with store.engine.connect() as conn:
        row = conn.execute(
            sa.text("SELECT display_name, removed_at FROM ne_principals WHERE principal_id='u1'")
        ).one()
    assert row.display_name == "New"
    assert row.removed_at is None


def test_mark_removed_cascades_edges_both_sides(store: PrincipalStore) -> None:
    store.upsert_principals([_user("u1"), _group("g1"), _group("g2")], source="entra", now=NOW)
    store.set_member_groups("u1", ["g1", "g2"], source="entra", now=NOW)
    assert store.direct_groups_of(["u1"])["u1"] == frozenset({"g1", "g2"})

    # Remove the member → its edges vanish.
    store.mark_removed(["u1"], now=LATER)
    assert store.direct_groups_of(["u1"]) == {}

    # Remove a group → edges where it is the group vanish (group-side cascade).
    store.upsert_principals([_user("u2")], source="entra", now=NOW)
    store.set_member_groups("u2", ["g1", "g2"], source="entra", now=NOW)
    store.mark_removed(["g1"], now=LATER)
    assert store.direct_groups_of(["u2"])["u2"] == frozenset({"g2"})


def test_set_group_members_recompute_not_accrete(store: PrincipalStore) -> None:
    added, removed = store.set_group_members("g", ["a", "b"], source="s", now=NOW)
    assert added == frozenset({"a", "b"})
    assert removed == frozenset()

    # Recompute to {b, c}: a is removed, c added, b unchanged.
    added, removed = store.set_group_members("g", ["b", "c"], source="s", now=LATER)
    assert added == frozenset({"c"})
    assert removed == frozenset({"a"})
    assert store.direct_groups_of(["b", "c"]) == {"b": frozenset({"g"}), "c": frozenset({"g"})}
    assert store.direct_groups_of(["a"]) == {}


def test_set_member_groups_recompute_not_accrete(store: PrincipalStore) -> None:
    store.set_member_groups("m", ["g1", "g2"], source="s", now=NOW)
    added, removed = store.set_member_groups("m", ["g2", "g3"], source="s", now=LATER)
    assert added == frozenset({"g3"})
    assert removed == frozenset({"g1"})
    assert store.direct_groups_of(["m"])["m"] == frozenset({"g2", "g3"})


def test_direct_groups_of_batches_over_500(store: PrincipalStore) -> None:
    members = [f"m{i}" for i in range(1200)]
    for m in members:
        store.set_member_groups(m, ["g-all"], source="s", now=NOW)
    result = store.direct_groups_of(members)
    assert len(result) == 1200
    assert all(v == frozenset({"g-all"}) for v in result.values())


def test_sync_state_round_trip(store: PrincipalStore) -> None:
    store.set_sync_state({"entra:groups": "link-1", "entra:users": "link-2"}, now=NOW)
    assert store.get_sync_state("entra:") == {
        "entra:groups": "link-1",
        "entra:users": "link-2",
    }
    # Update one key.
    store.set_sync_state({"entra:groups": "link-3"}, now=LATER)
    assert store.get_sync_state("entra:")["entra:groups"] == "link-3"


def test_transaction_rolls_back_whole_snapshot(store: PrincipalStore) -> None:
    with pytest.raises(RuntimeError, match="boom"), store.transaction():
        store.upsert_principals([_user("u1")], source="s", now=NOW)
        store.set_member_groups("u1", ["g1"], source="s", now=NOW)
        raise RuntimeError("boom")
    # Nothing committed.
    assert not store.is_known("u1")
    assert store.direct_groups_of(["u1"]) == {}


def test_all_principal_ids_excludes_removed(store: PrincipalStore) -> None:
    store.upsert_principals([_user("u1"), _user("u2")], source="s", now=NOW)
    store.mark_removed(["u2"], now=LATER)
    assert store.all_principal_ids() == {"u1"}
    assert store.all_principal_ids(include_removed=True) == {"u1", "u2"}


def test_all_principal_ids_source_filter(store: PrincipalStore) -> None:
    store.upsert_principals([_user("u-1"), _group("g-1")], source="scim", now=NOW)
    store.upsert_principals([_user("u-2")], source="entra", now=NOW)

    assert store.all_principal_ids(source="entra") == {"u-2"}
    assert store.all_principal_ids(source="scim") == {"u-1", "g-1"}
    assert store.all_principal_ids() == {"u-1", "g-1", "u-2"}

    # The source= and include_removed= filters compose.
    store.mark_removed(["u-2"], now=LATER)
    assert store.all_principal_ids(source="entra") == set()
    assert store.all_principal_ids(source="entra", include_removed=True) == {"u-2"}


def test_display_names_is_bounded_lookup_with_honest_tombstones(store: PrincipalStore) -> None:
    store.upsert_principals(
        [_user("live", " Ada "), _user("gone", "Grace"), _user("blank", "   ")],
        source="entra",
        now=NOW,
    )
    store.mark_removed(["gone"], now=LATER)
    assert store.display_names(["unknown", "gone", "live", "blank", "live"]) == {
        "live": ("Ada", False),
        "gone": ("Grace", True),
    }


def test_edges_gone_after_group_membership_table_empty(store: PrincipalStore) -> None:
    store.set_group_members("g", ["a"], source="s", now=NOW)
    store.set_group_members("g", [], source="s", now=LATER)
    with store.engine.connect() as conn:
        count = conn.execute(sa.select(sa.func.count()).select_from(ne_group_memberships)).scalar()
    assert count == 0


def test_transaction_binding_is_thread_local_not_instance_wide(store: PrincipalStore) -> None:
    """2026-08-29 U13 review (F4): no torn authorization read across threads.

    One PrincipalStore instance is shared by the principal-sync cron job and the
    query-time group expander, and the cron tick body runs on its own thread. A
    plain instance-attribute ``_bound`` made the sync's uncommitted snapshot
    connection visible to EVERY thread for the whole apply, so a concurrent
    query-time read ran on it mid-delete-before-reinsert and saw the torn state
    — an uncommitted (later rolled back) membership delete feeding an
    authorization decision. The binding must be per-thread: the reader gets its
    own committed connection and sees only committed membership.
    """
    import threading

    store.upsert_principals([_user("user:j"), _group("group:sales")], source="s", now=NOW)
    store.set_group_members("group:sales", ["user:j"], source="s", now=NOW)
    assert store.direct_groups_of(["user:j"]) == {"user:j": frozenset({"group:sales"})}

    mid_apply = threading.Event()
    resume = threading.Event()
    seen: dict[str, object] = {}

    def sync_thread() -> None:
        try:
            with store.transaction():
                # recompute-not-accrete mid-apply: the edge is deleted,
                # the reinsert has not happened yet.
                store.set_group_members("group:sales", [], source="s", now=LATER)
                mid_apply.set()
                assert resume.wait(timeout=30.0)
                raise RuntimeError("sync fails; the whole snapshot must roll back")
        except RuntimeError:
            pass

    def query_thread() -> None:
        assert mid_apply.wait(timeout=30.0)
        try:
            seen["groups"] = store.direct_groups_of(["user:j"])
        finally:
            resume.set()

    threads = [threading.Thread(target=sync_thread), threading.Thread(target=query_thread)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60.0)
    assert not any(thread.is_alive() for thread in threads)

    # The concurrent reader saw only COMMITTED membership (pre-fix: {} — the
    # sync's uncommitted delete), and the rollback restored nothing because
    # nothing committed ever changed.
    assert seen["groups"] == {"user:j": frozenset({"group:sales"})}
    assert store.direct_groups_of(["user:j"]) == {"user:j": frozenset({"group:sales"})}


def test_transaction_still_binds_reads_on_its_own_thread(store: PrincipalStore) -> None:
    """The owning thread's reads inside transaction() still see its own
    uncommitted writes (the atomic-snapshot contract PrincipalSyncService
    relies on) — thread-locality must not have broken same-thread binding."""
    store.upsert_principals([_user("user:j"), _group("group:g")], source="s", now=NOW)
    with store.transaction():
        store.set_group_members("group:g", ["user:j"], source="s", now=NOW)
        assert store.direct_groups_of(["user:j"]) == {"user:j": frozenset({"group:g"})}
    assert store.direct_groups_of(["user:j"]) == {"user:j": frozenset({"group:g"})}
