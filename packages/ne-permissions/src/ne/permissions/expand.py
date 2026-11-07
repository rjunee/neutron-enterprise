# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""PrincipalExpander — the NE-043 query-time group-expansion API.

Implements the DECIDED ACL model (SPEC §5 NE-042): chunks keep their source ACLs
as-is; at QUERY time the querying principal's group memberships expand so NE-043's
filter matches a chunk when the principal id OR any of its (transitive) group ids
appears in the chunk ACL. Membership changes do NOT trigger re-index — revocation
moves purely through this expansion on the next sync (lag = sync interval).

FAIL-CLOSED GUARANTEES (NE-CKC2 lesson — empty-means-match-all must be
structurally impossible):

* the result ALWAYS contains the principal id, so it can NEVER be empty —
  UNLESS the pid ITSELF is reserved-prefixed (``acl:*`` / ``role:*``), in which
  case the result is EXACTLY the non-matching :data:`RESERVED_PRINCIPAL_SENTINEL`
  (still non-empty, but it echoes neither the pid nor any well-known label). This
  is NE-FIX-IDENTITY P2-1 defense-in-depth: ne-identity already rejects a
  reserved subject at the verifier/provisioner boundary, so a reserved pid should
  never reach here — but if one did, it must never match a TENANT_ALL-stamped
  chunk;
* an unknown / unsynced principal expands to EXACTLY itself (plus any VERIFIED
  token-claim groups it carries) — never ``TENANT_ALL``, never widened; this now
  holds even for HOSTILE token-claim groups (Checkpoint-D finding #2, C7);
* the ``acl:*`` namespace is RESERVED — principal-supplied or store-edge-reachable
  ``acl:*`` ids are stripped, and the BFS never traverses THROUGH a reserved node
  (a poisoned edge chain dead-ends at it), so :data:`TENANT_ALL` /
  :data:`PUBLIC_LINK` are granted EXCLUSIVELY by the ``is_known`` branch below
  (NE-CKD2);
* :data:`TENANT_ALL` / :data:`PUBLIC_LINK` are added ONLY for a store-known
  (synced, non-tombstoned) principal;
* tombstoned principals/groups drop out structurally (their edges were deleted at
  tombstone time), so no runtime filtering is needed.

``expand`` is deliberately SYNC — it is pure indexed SQLite/Postgres reads, and
NE-043 calls it from its retrieval path. If NE-043 needs an async surface it can
wrap this in ``asyncio.to_thread`` (documented in the README).
"""

from __future__ import annotations

import secrets

from ne.contracts.types import Principal
from ne.permissions.models import PUBLIC_LINK, TENANT_ALL
from ne.permissions.store import PrincipalStore

__all__ = ["RESERVED_PRINCIPAL_SENTINEL", "PrincipalExpander"]

# The reserved namespace of TENANT_ALL / PUBLIC_LINK (ne.permissions.models).
# Principal-supplied groups and store-reachable ids in this namespace are stripped
# so the ONLY code path that grants them is the is_known gate in expand() below.
_RESERVED_LABEL_PREFIX = "acl:"

# Namespaces a principal id itself may never occupy (NE-FIX-IDENTITY P2-1).
# Mirrors ne.identity.roles.RESERVED_PREFIXES — ne-permissions must NOT import
# ne-identity (dependency direction is contracts-only), so keep the two in sync.
_RESERVED_PID_PREFIXES: tuple[str, str] = ("acl:", "role:")

# The single, non-matching label a reserved-prefixed pid expands to. It contains
# neither the pid nor TENANT_ALL/PUBLIC_LINK, and preserves the never-empty
# invariant.
#
# Codex P1 (NE-FIX-IDENTITY): a FIXED sentinel literal is itself a matchable ACL
# label — chunk ACL entries are opaque strings that could legitimately or
# maliciously carry that exact literal, which would then authorize EVERY rejected
# reserved principal (fail-open). So the sentinel is drawn from an UNFORGEABLE,
# per-process-random namespace: no chunk ACL — written before or after this
# process started, by any actor — can predict and therefore contain it, so the
# rejected state can never intersect a real ACL. The value is never persisted nor
# compared across processes (it lives only inside a single expand() result used
# immediately for set-intersection matching), so per-process randomness is sound;
# the stable prefix keeps it debuggable in logs while the entropy makes it
# match-proof.
RESERVED_PRINCIPAL_SENTINEL = f"invalid:reserved-principal:{secrets.token_urlsafe(24)}"


class PrincipalExpander:
    """Expand a principal to its full match-set (self + transitive groups + labels)."""

    def __init__(self, store: PrincipalStore, *, max_depth: int = 32) -> None:
        self._store = store
        self._max_depth = max_depth

    def expand(self, principal: Principal | str) -> frozenset[str]:
        """Return the frozenset of ACL labels a chunk may name to match ``principal``.

        Accepts a bare subject id or a full :class:`~ne.contracts.types.Principal`.
        A Principal's ``groups`` are VERIFIED token-claim groups from ne-identity —
        trusted and fresher than the store — so they seed the BFS directly, AFTER
        stripping the reserved ``acl:*`` label namespace. :data:`TENANT_ALL` /
        :data:`PUBLIC_LINK` can therefore enter the result ONLY via the
        ``is_known`` gate below, never through a hostile token-claim group.

        If the pid ITSELF is reserved-prefixed (``acl:*`` / ``role:*``), the store
        is never touched and the result is exactly
        :data:`RESERVED_PRINCIPAL_SENTINEL` — a non-matching sentinel (P2-1
        defense-in-depth; such a pid should never arrive, ne-identity rejects it).
        """
        pid = principal.id if isinstance(principal, Principal) else principal

        if pid.startswith(_RESERVED_PID_PREFIXES):
            # Non-empty (never-empty invariant preserved) but matches no chunk ACL:
            # it echoes neither the pid nor TENANT_ALL/PUBLIC_LINK, and no store
            # read happens.
            return frozenset({RESERVED_PRINCIPAL_SENTINEL})

        if isinstance(principal, Principal):
            seeds = {
                pid,
                *(g for g in principal.groups if not g.startswith(_RESERVED_LABEL_PREFIX)),
            }
        else:
            seeds = {pid}

        visited: set[str] = set(seeds)
        frontier: set[str] = set(seeds)
        for _ in range(self._max_depth):
            groups = self._store.direct_groups_of(frontier)
            new_ids: set[str] = set()
            for group_ids in groups.values():
                new_ids |= group_ids
            # Reserved acl:* ids are excluded BEFORE they can join the frontier,
            # so a poisoned edge chain (e.g. engineering -> acl:tenant-all ->
            # secret-group) dead-ends AT the reserved node instead of using it
            # as a traversal conduit.
            new_ids = {n for n in new_ids if not n.startswith(_RESERVED_LABEL_PREFIX)}
            new_ids -= visited
            if not new_ids:
                break
            visited |= new_ids
            frontier = new_ids

        # Belt-and-braces final sweep (seeds and BFS additions are already
        # stripped above — this guards future refactors); pid itself is always
        # kept to preserve the result-never-empty invariant.
        visited = {v for v in visited if v == pid or not v.startswith(_RESERVED_LABEL_PREFIX)}

        if self._store.is_known(pid):
            # Org-wide + public-link grants resolve for a synced tenant member
            # (NE-041 bullet 4 anti-over-restriction); NEVER for an unknown one.
            visited |= {TENANT_ALL, PUBLIC_LINK}

        return frozenset(visited)
