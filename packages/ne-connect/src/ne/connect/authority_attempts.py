# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Canonical metadata-only routing for durable authority attempts.

API and CLI callers identify a durable attempt; they do not choose which
connector implementation may retry it. This reader binds exact configured child
scopes to the server-owned source-kind routing table, then delegates currency to
the admission store's current connector-parent epoch check.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from ne.connect.authority_admission import (
    AdmissionAttempt,
    AdmissionStage,
    SqlAlchemyAdmissionAttemptStore,
)
from ne.contracts.source_authority import SourceReconciliationScope

__all__ = [
    "AuthorityAttemptNotPendingError",
    "AuthorityAttemptRead",
    "AuthorityAttemptReader",
    "UnknownAuthorityAttemptError",
    "UnknownAuthoritySourceKindError",
]


class UnknownAuthorityAttemptError(LookupError):
    """The durable attempt id does not exist or is outside configured scopes."""


class AuthorityAttemptNotPendingError(LookupError):
    """The attempt exists but is stale, superseded, or already complete."""


class UnknownAuthoritySourceKindError(ValueError):
    """A configured scope has no server-owned retry binding."""


@dataclass(frozen=True)
class AuthorityAttemptRead:
    """The metadata-only attempt projection consumed by API and CLI surfaces."""

    attempt_id: str
    source_kind: str
    source_binding: str
    scope_hash: str
    identity_hash: str
    stage: AdmissionStage
    source_revision: str
    contract_epoch: int
    prior_artifact_id: str | None
    receipt_id: str | None
    receipt_hash: str | None


_SOURCE_BINDING_BY_KIND = {
    "m365-sharepoint": "m365",
    "salesforce": "salesforce",
}

_CHILD_TAG_BY_KIND = {
    "m365-sharepoint": "drive",
    "salesforce": "object",
}


class AuthorityAttemptReader:
    """Read current attempts through exact configured child-scope routing."""

    def __init__(
        self,
        attempts: SqlAlchemyAdmissionAttemptStore,
        scopes: Iterable[SourceReconciliationScope],
    ) -> None:
        self._attempts = attempts
        self._scopes = {scope.scope_hash: scope for scope in scopes}
        for scope in self._scopes.values():
            _source_route(scope)

    def pending(self) -> tuple[AuthorityAttemptRead, ...]:
        """Return stable, deduplicated current attempts for configured scopes."""
        attempts = self._attempts.pending_for_current_scopes(self._scopes.values())
        return tuple(self._read(attempt, self._scopes[attempt.scope_hash]) for attempt in attempts)

    def read(self, attempt_id: str) -> AuthorityAttemptRead:
        """Read one durable id, refusing unknown and non-current attempts."""
        durable = self._attempts.inspect(attempt_id)
        if durable is None or durable.scope_hash not in self._scopes:
            raise UnknownAuthorityAttemptError(f"no configured authority attempt {attempt_id!r}")
        scope = self._scopes[durable.scope_hash]
        current = next(
            (
                attempt
                for attempt in self._attempts.pending_for_current_scopes((scope,))
                if attempt.attempt_id == attempt_id
            ),
            None,
        )
        if current is None:
            raise AuthorityAttemptNotPendingError(
                f"authority attempt {attempt_id!r} is stale, superseded, or complete"
            )
        return self._read(current, scope)

    @staticmethod
    def _read(attempt: AdmissionAttempt, scope: SourceReconciliationScope) -> AuthorityAttemptRead:
        source_kind, source_binding = _source_route(scope)
        return AuthorityAttemptRead(
            attempt_id=attempt.attempt_id,
            source_kind=source_kind,
            source_binding=source_binding,
            scope_hash=attempt.scope_hash,
            identity_hash=attempt.identity_hash,
            stage=attempt.stage,
            source_revision=attempt.source_revision,
            contract_epoch=attempt.contract_epoch,
            prior_artifact_id=attempt.prior_artifact_id,
            receipt_id=attempt.receipt_id,
            receipt_hash=attempt.receipt_hash,
        )


def _source_route(scope: SourceReconciliationScope) -> tuple[str, str]:
    path = scope.path
    source_kind = path[0]
    expected_tag = _CHILD_TAG_BY_KIND.get(source_kind)
    if len(path) != 6 or expected_tag is None or path[4] != expected_tag:
        raise UnknownAuthoritySourceKindError(
            "authority attempt scope has no canonical server-owned source binding"
        )
    return source_kind, _SOURCE_BINDING_BY_KIND[source_kind]
