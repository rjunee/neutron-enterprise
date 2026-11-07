# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The query-time shared-document resolver — the rebac leg of the C7 union.

:class:`OpenFgaSharedDocumentResolver` satisfies
``ne.brain.trimming.SharedDocumentResolver`` STRUCTURALLY (it is NOT imported
from ne-brain — this package is a leaf; the composition root injects the resolver
into ``PrincipalAclFilter(shared_docs=...)``). Given a principal it returns the
set of ``document_id`` values that principal may READ via a ReBAC ``viewer``
grant, which the C7 hook unions (OR) with the source-ACL label leg.

Fail-closed by construction (NE-087 §3.6 / I-2 / I-3 / I-8):

* an unknown/tombstoned principal gets NO rebac leg even with a live ``user:``
  tuple (the ``is_known`` gate — the TENANT_ALL-gate mirror);
* group membership is NEVER stored in OpenFGA — it is derived per-query as
  contextual tuples from the live NE-042 store expansion, with the reserved
  ``acl:*`` namespace stripped at construction (a hostile ``acl:tenant-all``
  claim can never become a ReBAC membership);
* a backend outage in ``degrade`` mode yields ∅ (the union collapses to the
  source leg — never wider, revocation-safe); in ``deny`` mode it raises;
* results are truncated to ``max_shared_documents`` (over-restriction direction).
"""

from __future__ import annotations

import logging

from ne.contracts.types import Principal
from ne.permissions_rebac.config import RebacSettings
from ne.permissions_rebac.errors import (
    BackendError,
    RebacDisabledError,
    RebacError,
    RebacUnavailableError,
)
from ne.permissions_rebac.ports import (
    GroupExpansion,
    KnownPrincipals,
    OpenFgaTupleClient,
    Tuple3,
)

__all__ = ["OpenFgaSharedDocumentResolver"]

_LOG = logging.getLogger(__name__)
_RESERVED_LABEL_PREFIX = "acl:"
_DOCUMENT_PREFIX = "document:"


class OpenFgaSharedDocumentResolver:
    """Resolve a principal to the ids of documents shared to it (ReBAC viewer)."""

    def __init__(
        self,
        client: OpenFgaTupleClient,
        expander: GroupExpansion,
        known: KnownPrincipals,
        settings: RebacSettings,
    ) -> None:
        if not settings.enabled:
            raise RebacDisabledError(
                "OpenFgaSharedDocumentResolver constructed while ReBAC is disabled"
            )
        self._client = client
        self._expander = expander
        self._known = known
        self._settings = settings
        #: Module-only stand-ins for the O-4 ne-obs counters (documented in README).
        self.degraded_count = 0
        self.truncated_count = 0

    async def shared_document_ids(self, principal: Principal | str) -> frozenset[str]:
        pid = principal.id if isinstance(principal, Principal) else principal

        # I-2 / T-1b: unknown/tombstoned principals get NO rebac leg, even with a
        # live user: tuple (the TENANT_ALL-gate mirror).
        if not self._known.is_known(pid):
            return frozenset()

        # Contextual group tuples derived per-query from the live store expansion,
        # excluding the principal itself and the reserved acl:* namespace (I-3).
        contextual: list[Tuple3] = [
            (f"user:{pid}", "member", f"group:{gid}")
            for gid in sorted(self._expander.expand(principal))
            if gid != pid and not gid.startswith(_RESERVED_LABEL_PREFIX)
        ]

        try:
            objects = await self._client.list_objects(
                f"user:{pid}", "viewer", "document", contextual_tuples=contextual
            )
        except BackendError as exc:
            # The confined SDK boundary (_client.py) raises BackendError — a
            # RebacError SUBCLASS — for EVERY outage/timeout/5xx. It MUST be
            # translated per mode here (I-8 / §3.6): degrade -> ∅ + counter/log
            # (the union collapses to the source leg, never wider); deny ->
            # RebacUnavailableError. This clause MUST precede the RebacError
            # re-raise below (BackendError is-a RebacError) so a sidecar outage
            # never becomes a platform-wide retrieval outage.
            return self._on_backend_error(pid, exc)
        except RebacError:
            raise
        except Exception as exc:
            return self._on_backend_error(pid, exc)

        ids = sorted(
            obj[len(_DOCUMENT_PREFIX) :] if obj.startswith(_DOCUMENT_PREFIX) else obj
            for obj in objects
        )
        cap = self._settings.max_shared_documents
        if len(ids) > cap:
            self.truncated_count += 1
            _LOG.warning(
                "rebac shared-document set for a principal exceeded max_shared_documents "
                "(%d > %d); truncating (over-restriction)",
                len(ids),
                cap,
            )
            ids = ids[:cap]
        return frozenset(ids)

    def _on_backend_error(self, pid: str, exc: Exception) -> frozenset[str]:
        if self._settings.unavailable_mode == "deny":
            raise RebacUnavailableError("openfga backend unavailable (deny mode)") from exc
        self.degraded_count += 1
        _LOG.warning("rebac resolver degraded (backend unavailable); rebac leg = empty set")
        return frozenset()
