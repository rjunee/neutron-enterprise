# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""C7 query-time security trimming — the fail-closed principal->ACL hook (NE-043).

This module ships the production side of C7 security trimming: the label algebra
that turns a querying principal into the set of ACL labels a chunk may name to be
visible to it, plus the :class:`PrincipalAclFilter` production hook wired into
:class:`ne.brain.retriever.HybridRetriever`'s ``acl_filters`` seam and
:meth:`ne.brain.qdrant.QdrantChunkStore.permission_filter`.

Fail-closed (the NE-CKC2 lesson, enshrined): empty-means-match-all is made
STRUCTURALLY impossible. :func:`acl_match_labels` ALWAYS contains ``principal.id``
(even against a broken expander that returns an empty set), so the emitted label
set is never empty; :class:`PrincipalAclFilter` therefore NEVER returns ``None``
and NEVER returns ``{}`` — the retriever's ``None``-means-disabled and
``{}``-means-match-nothing escape branches are unreachable from production and
remain only as defense in depth. An expander exception PROPAGATES (deny-by-
erroring) rather than degrading to an unscoped fetch.

WHY a package-local Protocol and not an import of ne-permissions:

* ne-contracts is FROZEN; its :class:`~ne.contracts.identity.PrincipalResolver`
  is a DIFFERENT seam (``resolve(subject) -> Principal``), not a group expander,
  so it cannot be reused here.
* A ne-brain -> ne-permissions *runtime* import edge is forbidden architecture
  (C3/C5 seam discipline): ne-connect already depends on ne-brain, and
  ne-permissions/sovereignty.py documents the anticipated cycle, so importing
  ne-permissions from ne-brain src would close it.

The fix is the established package-local structural-Protocol pattern (cf.
``QuerySparseEncoder`` / ``FilteredChunkFetch`` in ``retriever.py``): ne-brain
declares :class:`GroupExpander` here, and the concrete
``ne.permissions.expand.PrincipalExpander`` — which satisfies it verbatim — is
injected at the COMPOSITION ROOT. ``expand`` is intentionally SYNC (NE-042
designed it as pure indexed reads; the ``acl_filters`` seam is a sync callable);
wrap it in ``asyncio.to_thread`` at the composition root if the host loop needs
it, per the ne-permissions README.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from ne.contracts.types import Principal

#: The reserved OR-group key the ACL trimming hook may emit to union the source
#: ACL leg with the NE-087 ReBAC shared-document leg. Reserved to this hook: a
#: caller filter carrying ``$any`` (or ANY ``$``-prefixed key) is rejected by
#: :func:`ne.brain.retriever.and_combine_filters` (the Checkpoint-D anti-
#: smuggling lesson) so a caller can never widen the AND-composition. Defined
#: HERE as the single canonical source; ``retriever.py`` and ``qdrant.py`` import
#: it from this module so the grammar can never drift across the two seams.
ACL_ANY_KEY = "$any"

__all__ = [
    "ACL_ANY_KEY",
    "GroupExpander",
    "PrincipalAclFilter",
    "SharedDocumentResolver",
    "acl_match_labels",
]


@runtime_checkable
class GroupExpander(Protocol):
    """Structural protocol for a query-time principal-to-labels expander.

    Satisfied VERBATIM by ``ne.permissions.expand.PrincipalExpander`` (NE-042),
    which is injected at the composition root. Deliberately NOT imported from
    ne-permissions (that would close a ne-brain -> ne-permissions -> ... ->
    ne-brain cycle) and NOT part of the FROZEN ne-contracts surface — same
    package-local-Protocol pattern as ``QuerySparseEncoder``/``FilteredChunkFetch``.

    ``expand`` is SYNC on purpose: NE-042's expander is pure indexed SQLite/
    Postgres reads, and the ``acl_filters`` seam is a sync callable.
    """

    def expand(self, principal: Principal | str) -> frozenset[str]: ...


@runtime_checkable
class SharedDocumentResolver(Protocol):
    """Query-time principal -> ids of documents shared to it via ReBAC (NE-087).

    Satisfied STRUCTURALLY by
    ``ne.permissions_rebac.resolver.OpenFgaSharedDocumentResolver``, injected at
    the composition root (the same package-local-Protocol discipline as
    :class:`GroupExpander` — ne-brain must NOT import ne-permissions-rebac; that
    would close the ne-brain -> ne-permissions -> ... -> ne-brain cycle
    documented at :mod:`ne.permissions.sovereignty`).

    ASYNC on purpose: unlike :meth:`GroupExpander.expand` (pure indexed SQL), this
    is a network call to the in-VPC OpenFGA sidecar. Fail-closed contract
    (NE-087 §2.3 / §3.6): the concrete resolver MUST return an EMPTY frozenset
    (never "all") when it degrades on a backend outage, and MUST raise when it is
    configured to deny on outage — a raise PROPAGATES through
    :class:`PrincipalAclFilter` (deny-by-erroring, the trimming discipline). The
    returned ids are bare ``document_id`` values (no ``document:`` prefix) that
    the caller places into the ``$any`` OR-group's ``document_id`` leg — nothing
    from the resolver can ever enter the ``acl`` field (I-3 field isolation).
    """

    async def shared_document_ids(self, principal: Principal | str) -> frozenset[str]: ...


def acl_match_labels(principal: Principal, expander: GroupExpander | None) -> tuple[str, ...]:
    """The ACL label set a chunk may name to be visible to ``principal``.

    With an ``expander`` (production): its expansion (self + transitive groups +
    well-known labels for a store-known principal) is used. Without one
    (fixtures/eval fallback): the verified token identity only — the principal id
    plus its VERIFIED token-claim ``groups`` from ne-identity — never widened.

    Fail-closed invariants enforced here:

    * the result ALWAYS contains ``principal.id`` (a defensive union even against
      a broken expander that returns an empty set), so it is NEVER empty;
    * order is deterministic (sorted), so the store filter and the hook mapping
      built from it can never drift;
    * an expander exception PROPAGATES — this never catches-and-falls-back to an
      unscoped/None result.
    """
    labels = (
        expander.expand(principal)
        if expander is not None
        else frozenset({principal.id, *principal.groups})
    )
    return tuple(sorted(set(labels) | {principal.id}))


class PrincipalAclFilter:
    """The production ``acl_filters`` hook — fail-closed, never None, never ``{}``.

    Composed at the composition root over the concrete NE-042 ``PrincipalExpander``
    and passed to ``HybridRetriever(acl_filters=...)``. Because
    :func:`acl_match_labels` is never empty, ``__call__`` always returns a
    non-empty mapping — so the retriever's ``None`` (disabled) and ``{}``
    (match-nothing) branches are never reached from production and survive only as
    defense in depth.

    NE-087 union seam (ADDITIVE — the §1.2 union rule; see the ne087 producer
    spec §2.2). With NO ``shared_docs`` resolver wired (every existing
    composition, including ne-api app.py) the behavior is BYTE-IDENTICAL to
    before: ``__call__`` returns the plain SYNC ``{"acl": [...]}`` mapping. With a
    resolver wired, ``__call__`` returns a COROUTINE (awaited by the retriever's
    awaitable-aware hook path) that unions the source ACL leg with the ReBAC
    shared-document leg:

    * zero shares  -> ``{"acl": [...]}`` (the ``$any`` wrapper is NOT emitted — no
      empty OR-branch; the enabled-no-shares path stays byte-identical to today,
      proved by the byte-identity test);
    * >=1 share    -> ``{"$any": [{"acl": [...]}, {"document_id": [...sorted]}]}``.

    A resolver exception PROPAGATES out of the awaited coroutine (deny-by-erroring
    at this layer; the concrete resolver handles degrade-vs-deny internally).
    """

    def __init__(
        self, expander: GroupExpander, *, shared_docs: SharedDocumentResolver | None = None
    ) -> None:
        self._expander = expander
        self._shared_docs = shared_docs

    def __call__(self, principal: Principal) -> Any:
        # Return type is the dynamic sync-or-async trimming seam
        # (``Mapping[str, Any] | Awaitable[Mapping[str, Any]]``); it is declared
        # ``Any`` so existing SYNC callers keep treating the no-resolver result as
        # a plain ``Mapping`` (the retriever awaits it only when awaitable).
        if self._shared_docs is None:
            # BYTE-IDENTICAL to the pre-NE-087 hook: a plain sync mapping, never a
            # coroutine — so every existing sync composition is untouched.
            return {"acl": list(acl_match_labels(principal, self._expander))}
        return self._resolve(principal)

    async def _resolve(self, principal: Principal) -> Mapping[str, Any]:
        assert self._shared_docs is not None
        labels = list(acl_match_labels(principal, self._expander))
        shared = await self._shared_docs.shared_document_ids(principal)
        if not shared:
            # Collapse: no shares -> no OR-group, identical to the disabled path.
            return {"acl": labels}
        return {ACL_ANY_KEY: [{"acl": labels}, {"document_id": sorted(shared)}]}
