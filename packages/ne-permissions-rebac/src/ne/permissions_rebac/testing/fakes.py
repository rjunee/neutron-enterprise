# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""In-memory test doubles — the reusable CI surface (also for the O-4 rider).

``InMemoryOpenFga`` implements the §1.1 model semantics EXACTLY in memory (owner
folds into viewer; ``group#member`` resolves ONLY via CONTEXTUAL tuples — group
membership is never stored, mirroring the production resolver). No deny semantics
are representable. The remaining fakes satisfy the package's dependency Protocols
so the resolver, share service, bootstrap, and gc can be tested with NO live
server.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ne.contracts.governance import (
    AuditVerification,
    DecisionEffect,
    PolicyDecision,
    PolicyRequest,
)
from ne.contracts.types import Principal
from ne.permissions_rebac.errors import TupleAbsentError
from ne.permissions_rebac.ports import Tuple3

__all__ = [
    "AlwaysExists",
    "FailingAuditSink",
    "FakeGroupExpansion",
    "FakeKnownPrincipals",
    "FakeReadProbe",
    "InMemoryOpenFga",
    "NeverExists",
    "RecordingAuditSink",
    "StaticPdp",
]

_MEMBER_SUFFIX = "#member"


class InMemoryOpenFga:
    """In-memory :class:`~ne.permissions_rebac.ports.OpenFgaTupleClient`.

    ``fail_with`` (when set) makes EVERY method raise it (backend-outage
    injection, T-8). Duplicate writes raise a plain ``ValueError`` (documented
    type-shape divergence from the server, unchanged). Deletes of an absent
    tuple raise :class:`~ne.permissions_rebac.errors.TupleAbsentError`
    TRANSACTIONALLY — mirroring the real server (NE-REBAC-REVOKE-IDEMPOTENT):
    a batch containing even one already-absent tuple deletes NOTHING.
    ``model`` backs ``read_authorization_model`` for pin tests.
    """

    def __init__(
        self,
        *,
        tuples: set[Tuple3] | None = None,
        model: Mapping[str, Any] | None = None,
        fail_with: Exception | None = None,
    ) -> None:
        self.tuples: set[Tuple3] = set(tuples or set())
        self.model: Mapping[str, Any] | None = model
        self.fail_with: Exception | None = fail_with
        self.created_stores: list[str] = []
        #: Flipped True by :meth:`close` — the O-4 lifecycle proof (the roots MUST
        #: close the retained client on shutdown; a fake owns no real session).
        self.closed: bool = False

    async def close(self) -> None:
        self.closed = True

    def _guard(self) -> None:
        if self.fail_with is not None:
            raise self.fail_with

    def _has_viewer(self, user: str, obj: str, ctx: set[Tuple3]) -> bool:
        if (user, "viewer", obj) in self.tuples:
            return True
        if (user, "owner", obj) in self.tuples:  # owner folds into viewer
            return True
        for tup_user, relation, tup_obj in self.tuples:
            if relation == "viewer" and tup_obj == obj and tup_user.endswith(_MEMBER_SUFFIX):
                group = tup_user[: -len(_MEMBER_SUFFIX)]  # "group:<id>"
                if (user, "member", group) in ctx:
                    return True
        return False

    async def list_objects(
        self,
        user: str,
        relation: str,
        object_type: str,
        *,
        contextual_tuples: Sequence[Tuple3],
    ) -> Sequence[str]:
        self._guard()
        if relation != "viewer":
            raise ValueError(f"InMemoryOpenFga only models the 'viewer' relation, got {relation!r}")
        prefix = f"{object_type}:"
        ctx = set(contextual_tuples)
        candidates = {o for (_, _, o) in self.tuples if o.startswith(prefix)}
        return sorted(o for o in candidates if self._has_viewer(user, o, ctx))

    async def write_tuples(self, tuples: Sequence[Tuple3]) -> None:
        self._guard()
        for t in tuples:
            if t in self.tuples:
                raise ValueError(f"duplicate tuple write: {t!r}")
        self.tuples.update(tuples)

    async def delete_tuples(self, tuples: Sequence[Tuple3]) -> None:
        self._guard()
        # Transactional, mirroring the real Write API: if ANY tuple in the
        # batch is already absent, NOTHING is deleted (NE-REBAC-REVOKE-
        # IDEMPOTENT — the fake used to silently no-op here, which is exactly
        # why this class of fake-more-forgiving-than-production divergence
        # hid the real defect until a real server was put under the suite).
        missing = [t for t in tuples if t not in self.tuples]
        if missing:
            raise TupleAbsentError(f"cannot delete a tuple which does not exist: {missing[0]!r}")
        for t in tuples:
            self.tuples.discard(t)

    async def read_tuples(
        self, *, object_filter: str | None = None, user_filter: str | None = None
    ) -> Sequence[Tuple3]:
        self._guard()
        out = [
            t
            for t in self.tuples
            if (object_filter is None or t[2] == object_filter)
            and (user_filter is None or t[0] == user_filter)
        ]
        return sorted(out)

    async def read_authorization_model(self) -> Mapping[str, Any]:
        self._guard()
        return dict(self.model or {})

    async def create_store(self, name: str) -> str:
        self._guard()
        store_id = f"store-{name}"
        self.created_stores.append(store_id)
        return store_id

    async def write_authorization_model(self, model: Mapping[str, Any]) -> str:
        self._guard()
        self.model = model
        return "model-fake-1"


class FakeKnownPrincipals:
    """:class:`~ne.permissions_rebac.ports.KnownPrincipals` over a set of ids."""

    def __init__(self, known: set[str]) -> None:
        self.known = set(known)

    def is_known(self, principal_id: str) -> bool:
        return principal_id in self.known


class FakeGroupExpansion:
    """:class:`~ne.permissions_rebac.ports.GroupExpansion` over a dict.

    ``expand(p)`` = ``{pid} | mapping.get(pid, frozenset())`` (always contains the
    principal id, mirroring the real expander's never-empty invariant).
    """

    def __init__(self, mapping: dict[str, frozenset[str]] | None = None) -> None:
        self.mapping = mapping or {}

    def expand(self, principal: Principal | str) -> frozenset[str]:
        pid = principal.id if isinstance(principal, Principal) else principal
        return frozenset({pid, *self.mapping.get(pid, frozenset())})


class FakeReadProbe:
    """:class:`~ne.permissions_rebac.ports.DocumentReadProbe` over a readable set."""

    def __init__(self, readable: set[tuple[str, str]]) -> None:
        self.readable = set(readable)

    async def can_read(self, principal: Principal, document_id: str) -> bool:
        return (principal.id, document_id) in self.readable


class AlwaysExists:
    """:class:`~ne.permissions_rebac.ports.DocumentExists` — everything exists."""

    async def exists(self, document_id: str) -> bool:
        return True


class NeverExists:
    """:class:`~ne.permissions_rebac.ports.DocumentExists` — nothing exists."""

    async def exists(self, document_id: str) -> bool:
        return False


class StaticPdp:
    """A recording :class:`~ne.contracts.governance.PolicyDecisionPoint`."""

    def __init__(self, effect: DecisionEffect, tier: int | None = None) -> None:
        self.effect = effect
        self.tier = tier
        self.requests: list[PolicyRequest] = []

    async def decide(self, request: PolicyRequest) -> PolicyDecision:
        self.requests.append(request)
        return PolicyDecision(effect=self.effect, policy_version="fake-1", tier=self.tier)


class RecordingAuditSink:
    """An :class:`~ne.contracts.governance.AuditSink` that records appended events."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def append(self, event: Any) -> None:
        self.events.append(event)

    async def verify(self, *, start: Any = None, end: Any = None) -> AuditVerification:
        return AuditVerification(valid=True, checked=len(self.events))


class FailingAuditSink:
    """An :class:`~ne.contracts.governance.AuditSink` whose append always fails."""

    async def append(self, event: Any) -> None:
        raise RuntimeError("audit sink append failed")

    async def verify(self, *, start: Any = None, end: Any = None) -> AuditVerification:
        return AuditVerification(valid=False, checked=0)
