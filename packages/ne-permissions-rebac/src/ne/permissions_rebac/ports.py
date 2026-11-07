# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Package-internal Protocols so the OpenFGA SDK stays confined and fakes trivial.

Every tuple is a plain ``(user, relation, object)`` string triple in OpenFGA form
(e.g. ``("user:jane", "viewer", "document:d1")``) — the SDK's model classes never
cross this seam, so :mod:`ne.permissions_rebac._client` is the ONLY module that
imports ``openfga_sdk`` (the C3 chokepoint) and the in-memory
:class:`~ne.permissions_rebac.testing.fakes.InMemoryOpenFga` implements the same
surface for CI.

The dependency Protocols (:class:`KnownPrincipals`, :class:`GroupExpansion`) are
satisfied VERBATIM by ``ne.permissions`` types; :class:`DocumentReadProbe` and
:class:`DocumentExists` have production implementations that live at the
composition root (O-4 wiring) — this module only declares their shape.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

from ne.contracts.types import Principal

__all__ = [
    "DocumentExists",
    "DocumentReadProbe",
    "GroupExpansion",
    "KnownPrincipals",
    "OpenFgaTupleClient",
]

#: A relationship tuple in OpenFGA form: (user, relation, object).
Tuple3 = tuple[str, str, str]


@runtime_checkable
class OpenFgaTupleClient(Protocol):
    """The confined OpenFGA surface — plain string tuples only, async.

    Satisfied by :class:`ne.permissions_rebac._client.OpenFgaHttpClient` (over the
    real SDK) and by the in-memory fake. Backend/timeout/HTTP failures surface as
    a single internal error the resolver/share-service translate per mode.
    """

    async def list_objects(
        self,
        user: str,
        relation: str,
        object_type: str,
        *,
        contextual_tuples: Sequence[Tuple3],
    ) -> Sequence[str]:
        """Return the ``'<type>:<id>'`` object strings ``user`` has ``relation`` on."""
        ...

    async def write_tuples(self, tuples: Sequence[Tuple3]) -> None: ...

    async def delete_tuples(self, tuples: Sequence[Tuple3]) -> None:
        """Delete ``tuples`` transactionally (no partial deletes on failure).

        Contract (NE-REBAC-REVOKE-IDEMPOTENT): an implementation MUST raise
        :class:`~ne.permissions_rebac.errors.TupleAbsentError` when the
        backend reports that a tuple in the batch does not exist, and
        :class:`~ne.permissions_rebac.errors.BackendError` for any OTHER
        backend failure (outage, auth, malformed request). This is what makes
        the production :class:`~ne.permissions_rebac._client.OpenFgaHttpClient`
        and the in-memory
        :class:`~ne.permissions_rebac.testing.fakes.InMemoryOpenFga` conformant
        BY CONTRACT rather than by accident — ``ShareService.revoke`` relies on
        exactly this shape to be genuinely idempotent.
        """
        ...

    async def close(self) -> None:
        """Release any owned transport/session (O-4 lifecycle at the roots)."""
        ...

    async def read_tuples(
        self, *, object_filter: str | None = None, user_filter: str | None = None
    ) -> Sequence[Tuple3]: ...

    async def read_authorization_model(self) -> Mapping[str, Any]: ...

    # -- bootstrap only ------------------------------------------------------
    async def create_store(self, name: str) -> str: ...

    async def write_authorization_model(self, model: Mapping[str, Any]) -> str: ...


@runtime_checkable
class KnownPrincipals(Protocol):
    """Whether a principal id is store-known (synced, non-tombstoned).

    Satisfied VERBATIM by ``ne.permissions.store.PrincipalStore.is_known``.
    """

    def is_known(self, principal_id: str) -> bool: ...


@runtime_checkable
class GroupExpansion(Protocol):
    """Query-time principal -> full label/group match set.

    Satisfied VERBATIM by ``ne.permissions.expand.PrincipalExpander.expand``.
    """

    def expand(self, principal: Principal | str) -> frozenset[str]: ...


@runtime_checkable
class DocumentReadProbe(Protocol):
    """Whether ``principal`` can currently READ ``document_id`` (share-requires-read).

    The production implementation is a composition-root probe over the trimmed
    retrieval path (a ``get_filtered`` probe on any chunk of the doc, per NE-087
    §3.5) — O-4 wiring. This module only declares the shape so ``ShareService``
    can enforce C12 (a low-scope agent cannot exfiltrate-by-sharing).
    """

    async def can_read(self, principal: Principal, document_id: str) -> bool: ...


@runtime_checkable
class DocumentExists(Protocol):
    """Whether ``document_id`` still exists (GC sweep). Production impl is O-4."""

    async def exists(self, document_id: str) -> bool: ...
