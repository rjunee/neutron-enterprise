# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The ONE-ACL visibility filter (design §7.7) — the SAME expander the brain uses.

Zero new authorization code beyond a set-intersection. ne-skills does NOT import
ne-permissions; a package-local structural :class:`GroupExpander` Protocol (the
``ne.brain.trimming`` :class:`GroupExpander` pattern, trimming.py:65-79) is
satisfied VERBATIM by ``ne.permissions.expand.PrincipalExpander``, which the
composition root injects — the SAME instance the chunk retriever's
``PrincipalAclFilter`` holds (F2 one-expander discipline).

:func:`match_labels` INTENTIONALLY MIRRORS (does not import) the brain's
``acl_match_labels`` (trimming.py:107-129): it unions ``{principal.id}`` defensively
so the result is never empty even against a broken expander, and an expander
exception PROPAGATES (deny-by-erroring — never catch-and-fall-back to unfiltered).
The one deliberate delta from chunks: an EMPTY ``acl_labels`` tuple means GLOBAL
(visible to all), because a bundled default pack ships unlabelled, whereas a chunk
always carries stamped ACLs.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ne.contracts.types import Principal


@runtime_checkable
class GroupExpander(Protocol):
    """Package-local structural Protocol (the trimming.py:66-79 pattern).

    ne-skills does NOT import ne-permissions; the composition root injects the
    concrete ``ne.permissions.expand.PrincipalExpander`` which satisfies this
    verbatim. ``expand`` is SYNC (pure indexed reads).
    """

    def expand(self, principal: Principal | str) -> frozenset[str]: ...


def match_labels(principal: Principal, expander: GroupExpander) -> frozenset[str]:
    """The label set ``principal`` may match a pack against.

    Deliberately mirrors ``ne.brain.trimming.acl_match_labels`` (self + transitive
    groups + well-known labels for a store-known principal) with the defensive
    ``{principal.id}`` union so it is never empty. An expander exception PROPAGATES
    (deny-by-erroring) — this NEVER catches-and-falls-back to unfiltered.
    """
    return frozenset(expander.expand(principal)) | {principal.id}


def visible(pack_acl_labels: tuple[str, ...], match: frozenset[str]) -> bool:
    """``()`` = GLOBAL (visible to all — the deliberate delta from chunks).

    Otherwise: label-set intersection, EXACTLY the predicate NE-043 applies to
    chunks.
    """
    return not pack_acl_labels or bool(set(pack_acl_labels) & match)
