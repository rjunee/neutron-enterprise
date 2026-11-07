# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""PrincipalExpander: the fail-closed expansion suite (NE-CKC2 lesson).

empty-means-match-all must be structurally impossible: expand() ALWAYS contains
the principal id, an unknown principal expands to exactly itself, and TENANT_ALL/
PUBLIC_LINK are added only for store-known principals.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ne.contracts.types import Principal
from ne.permissions.expand import RESERVED_PRINCIPAL_SENTINEL, PrincipalExpander
from ne.permissions.models import PUBLIC_LINK, TENANT_ALL, PrincipalKind, SyncedPrincipal
from ne.permissions.store import PrincipalStore

NOW = datetime(2026, 7, 6, tzinfo=UTC)


def _upsert(store: PrincipalStore, *pids: str) -> None:
    store.upsert_principals(
        [SyncedPrincipal(principal_id=p, kind=PrincipalKind.USER) for p in pids],
        source="test",
        now=NOW,
    )


def test_unknown_principal_expands_to_exactly_itself(store: PrincipalStore) -> None:
    result = PrincipalExpander(store).expand("ghost")
    assert result == frozenset({"ghost"})
    assert TENANT_ALL not in result
    assert PUBLIC_LINK not in result


def test_result_never_empty(store: PrincipalStore) -> None:
    exp = PrincipalExpander(store)
    for pid in ["", "x", "unknown-42"]:
        assert pid in exp.expand(pid)
        assert exp.expand(pid)  # truthy → non-empty


def test_transitive_nested_groups(store: PrincipalStore) -> None:
    _upsert(store, "u")
    store.set_member_groups("u", ["g1"], source="t", now=NOW)
    store.set_member_groups("g1", ["g2"], source="t", now=NOW)
    store.set_member_groups("g2", ["g3"], source="t", now=NOW)
    result = PrincipalExpander(store).expand("u")
    assert {"u", "g1", "g2", "g3"} <= result


def test_membership_cycle_terminates(store: PrincipalStore) -> None:
    store.set_member_groups("gA", ["gB"], source="t", now=NOW)
    store.set_member_groups("gB", ["gA"], source="t", now=NOW)
    result = PrincipalExpander(store).expand("gA")
    assert {"gA", "gB"} <= result


def test_max_depth_truncates_chain(store: PrincipalStore) -> None:
    store.set_member_groups("u", ["g1"], source="t", now=NOW)
    store.set_member_groups("g1", ["g2"], source="t", now=NOW)
    store.set_member_groups("g2", ["g3"], source="t", now=NOW)
    store.set_member_groups("g3", ["g4"], source="t", now=NOW)
    result = PrincipalExpander(store, max_depth=2).expand("u")
    assert {"u", "g1", "g2"} <= result
    assert "g3" not in result
    assert "g4" not in result


def test_principal_instance_unions_token_claim_groups(store: PrincipalStore) -> None:
    _upsert(store, "u1")
    store.set_member_groups("u1", ["g-store"], source="t", now=NOW)
    principal = Principal(id="u1", groups=("g-tok",))
    result = PrincipalExpander(store).expand(principal)
    assert {"u1", "g-tok", "g-store"} <= result


def test_tombstoned_group_disappears(store: PrincipalStore) -> None:
    _upsert(store, "u")
    store.set_member_groups("u", ["g1", "g2"], source="t", now=NOW)
    store.mark_removed(["g1"], now=NOW)
    result = PrincipalExpander(store).expand("u")
    assert "g1" not in result
    assert "g2" in result


def test_known_principal_gains_well_known_labels(store: PrincipalStore) -> None:
    _upsert(store, "known")
    result = PrincipalExpander(store).expand("known")
    assert TENANT_ALL in result
    assert PUBLIC_LINK in result
    # Unknown principal never gains them.
    assert TENANT_ALL not in PrincipalExpander(store).expand("stranger")


def test_forged_reserved_seed_groups_never_widen(store: PrincipalStore) -> None:
    # THE audit exploit: unknown principal carrying the reserved ACL labels as
    # verified token-claim groups must expand to exactly {self, engineering}.
    p = Principal(id="mallory", groups=("acl:tenant-all", "acl:public-link", "engineering"))
    result = PrincipalExpander(store).expand(p)
    assert result == frozenset({"mallory", "engineering"})
    assert TENANT_ALL not in result
    assert PUBLIC_LINK not in result


def test_known_principal_reserved_entitlement_unaffected(store: PrincipalStore) -> None:
    _upsert(store, "alice")
    p = Principal(id="alice", groups=("acl:tenant-all", "engineering"))
    result = PrincipalExpander(store).expand(p)
    assert {"alice", "engineering"} <= result
    # Present ONLY because the is_known gate added them (store-known entitlement).
    assert TENANT_ALL in result
    assert PUBLIC_LINK in result


def test_poisoned_store_edge_cannot_widen_unknown_principal(store: PrincipalStore) -> None:
    # A legacy pre-fix poisoned store edge engineering -> acl:tenant-all.
    store.upsert_principals(
        [
            SyncedPrincipal(principal_id="engineering", kind=PrincipalKind.GROUP),
            SyncedPrincipal(principal_id="acl:tenant-all", kind=PrincipalKind.GROUP),
        ],
        source="test",
        now=NOW,
    )
    store.set_member_groups("engineering", ["acl:tenant-all"], source="t", now=NOW)
    p = Principal(id="stranger", groups=("engineering",))
    result = PrincipalExpander(store).expand(p)
    assert {"stranger", "engineering"} <= result
    # The BFS-strip layer holds even when the store itself is polluted.
    assert TENANT_ALL not in result


def test_reserved_node_is_not_a_traversal_conduit(store: PrincipalStore) -> None:
    # Codex review of NE-CKD2: a poisoned chain engineering -> acl:tenant-all ->
    # secret-group must dead-end AT the reserved node. Stripping acl:* only
    # after the BFS would still leak secret-group into the result.
    store.set_member_groups("engineering", ["acl:tenant-all"], source="t", now=NOW)
    store.set_member_groups("acl:tenant-all", ["secret-group"], source="t", now=NOW)
    p = Principal(id="stranger", groups=("engineering",))
    result = PrincipalExpander(store).expand(p)
    assert result == frozenset({"stranger", "engineering"})
    assert "secret-group" not in result
    assert TENANT_ALL not in result


def test_return_type_is_frozenset(store: PrincipalStore) -> None:
    assert isinstance(PrincipalExpander(store).expand("x"), frozenset)


def test_reserved_pid_yields_no_tenant_all_match(store: PrincipalStore) -> None:
    # NE-FIX-IDENTITY P2-1 defense-in-depth: even a STORE-KNOWN reserved pid must
    # expand to exactly the non-matching sentinel — never TENANT_ALL/PUBLIC_LINK.
    _upsert(store, "acl:tenant-all")
    result = PrincipalExpander(store).expand(
        Principal(id="acl:tenant-all", groups=("engineering",))
    )
    assert result == frozenset({RESERVED_PRINCIPAL_SENTINEL})
    assert TENANT_ALL not in result
    assert PUBLIC_LINK not in result
    assert "acl:tenant-all" not in result
    assert "engineering" not in result


def test_reserved_role_pid_yields_no_match(store: PrincipalStore) -> None:
    # Bare-str form, role:* prefix.
    result = PrincipalExpander(store).expand("role:admin")
    assert result == frozenset({RESERVED_PRINCIPAL_SENTINEL})
    assert "role:admin" not in result


def test_reserved_pid_result_is_non_empty_frozenset(store: PrincipalStore) -> None:
    result = PrincipalExpander(store).expand("acl:public-link")
    assert isinstance(result, frozenset)
    assert result  # never-empty invariant holds via the sentinel


def test_reserved_sentinel_is_unforgeable_not_a_matchable_literal(store: PrincipalStore) -> None:
    # Codex P1: the sentinel must NOT be a fixed, guessable, matchable ACL label.
    # A chunk ACL an attacker could author (e.g. the human-readable prefix, or the
    # old fixed literal) must not intersect the rejected reserved principal's
    # match-set — otherwise a rejected principal would be authorized for every such
    # chunk. This fails if the sentinel is reverted to a fixed literal.
    result = PrincipalExpander(store).expand("acl:tenant-all")
    attacker_authored_acl = {
        "invalid:reserved-principal",  # the pre-fix fixed literal
        "invalid:reserved-principal:",  # the debuggable prefix a reader could guess
        "reserved-principal",
    }
    assert not (result & attacker_authored_acl)
    # The real sentinel carries unguessable entropy beyond the readable prefix.
    (only,) = result
    assert only.startswith("invalid:reserved-principal:")
    assert len(only) > len("invalid:reserved-principal:") + 20
