# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""``ShareService`` — the ONLY writer of OpenFGA tuples (NE-087 §3.5, I-4).

Every share/revoke passes, in exact order, a fail-closed pipeline with a single
exit: validate -> subject-known -> share-requires-read -> PDP -> audit-then-write.
There is NO sink-less / pdp-less / probe-less constructor (the NE-CKD5 lesson):
all governance dependencies are required positional args. Connectors never write
tuples — enforced repo-wide by the ``c3-openfga-sdk-chokepoint`` import contract
(only ``_client`` may touch the SDK) and by this being the sole caller of
``write_tuples`` / ``delete_tuples`` in the module.
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import suppress

from ne.contracts.governance import (
    AuditSink,
    DecisionEffect,
    PolicyDecisionPoint,
    PolicyRequest,
)
from ne.contracts.types import Principal
from ne.events import new_audit_event
from ne.permissions_rebac.config import RebacSettings
from ne.permissions_rebac.errors import (
    RebacAuditError,
    ShareNotPermittedError,
    ShareRequiresApprovalError,
    ShareValidationError,
    TupleAbsentError,
)
from ne.permissions_rebac.ports import (
    DocumentReadProbe,
    KnownPrincipals,
    OpenFgaTupleClient,
    Tuple3,
)
from ne.schema import AuditOutcome

__all__ = ["ShareService"]

_RESERVED_LABEL_PREFIX = "acl:"
_VALID_SUBJECT_TYPES = ("user", "group")
_ONLY_RELATION = "viewer"


class ShareService:
    """Governed, audited share/revoke over ``document`` viewer tuples."""

    def __init__(
        self,
        client: OpenFgaTupleClient,
        known: KnownPrincipals,
        read_probe: DocumentReadProbe,
        pdp: PolicyDecisionPoint,
        sink: AuditSink,
        *,
        settings: RebacSettings,
    ) -> None:
        self._client = client
        self._known = known
        self._read_probe = read_probe
        self._pdp = pdp
        self._sink = sink
        self._settings = settings

    # -- validation ----------------------------------------------------------
    @staticmethod
    def _validate(document_id: str, subject: str, relation: str) -> tuple[str, str]:
        """Return ``(subject_type, subject_id)`` or raise ``ShareValidationError``."""
        if not document_id or document_id.startswith(_RESERVED_LABEL_PREFIX):
            raise ShareValidationError(f"invalid document_id {document_id!r}")
        if relation != _ONLY_RELATION:
            raise ShareValidationError(f"relation must be {_ONLY_RELATION!r}, got {relation!r}")
        if "#" in subject:
            raise ShareValidationError(
                "subject must not contain '#'; the service derives '#member' itself"
            )
        subject_type, sep, subject_id = subject.partition(":")
        if not sep:  # no ':' present
            raise ShareValidationError(
                f"subject must be 'user:<id>' or 'group:<id>', got {subject!r}"
            )
        if subject_type not in _VALID_SUBJECT_TYPES:
            raise ShareValidationError(f"invalid subject type {subject_type!r}")
        if not subject_id or subject_id.startswith(_RESERVED_LABEL_PREFIX):
            raise ShareValidationError(f"invalid subject id in {subject!r}")
        return subject_type, subject_id

    def _viewer_tuple(self, subject_type: str, subject_id: str, document_id: str) -> Tuple3:
        obj = f"document:{document_id}"
        if subject_type == "group":
            return (f"group:{subject_id}#member", _ONLY_RELATION, obj)
        return (f"user:{subject_id}", _ONLY_RELATION, obj)

    async def _is_owner(self, principal_id: str, document_id: str) -> bool:
        existing = await self._client.read_tuples(object_filter=f"document:{document_id}")
        owner = (f"user:{principal_id}", "owner", f"document:{document_id}")
        return owner in existing

    # -- share ---------------------------------------------------------------
    async def share(
        self, document_id: str, subject: str, relation: str = _ONLY_RELATION, *, sharer: Principal
    ) -> None:
        subject_type, subject_id = self._validate(document_id, subject, relation)

        # (2) subject known — no dangling grants to unsynced/tombstoned ids.
        if not self._known.is_known(subject_id):
            raise ShareValidationError(f"subject {subject!r} is not a known principal")

        # (3) share-requires-read — BEFORE the PDP call (C12 exfiltration guard).
        if not await self._read_probe.can_read(sharer, document_id):
            raise ShareNotPermittedError(
                "sharer cannot currently read the document (share-requires-read)"
            )

        # (4) PDP — the audited GovernedPdp.
        decision = await self._pdp.decide(
            PolicyRequest(
                principal=sharer,
                action="rebac.share",
                resource=f"document:{document_id}",
                context={"subject": subject, "relation": relation},
            )
        )
        if decision.effect is DecisionEffect.DENY:
            raise ShareNotPermittedError("rebac.share denied by policy")
        if decision.effect is DecisionEffect.REQUIRE_APPROVAL:
            raise ShareRequiresApprovalError("rebac.share requires approval", decision=decision)

        # (5) audit-then-write — audit failure fails the share closed (no tuple).
        try:
            await self._sink.append(
                new_audit_event(
                    action="rebac.share.created",
                    resource=f"document:{document_id}",
                    outcome=AuditOutcome.success,
                    actor=sharer.id,
                    provenance={
                        "subject": subject,
                        "relation": relation,
                        "policy_version": decision.policy_version,
                    },
                )
            )
        except Exception as exc:
            raise RebacAuditError("audit append failed; no tuple written") from exc

        to_write: list[Tuple3] = [self._viewer_tuple(subject_type, subject_id, document_id)]
        if not await self._is_owner(sharer.id, document_id):
            to_write.append((f"user:{sharer.id}", "owner", f"document:{document_id}"))
        await self._client.write_tuples(to_write)

    # -- revoke --------------------------------------------------------------
    async def revoke(
        self, document_id: str, subject: str, relation: str = _ONLY_RELATION, *, actor: Principal
    ) -> None:
        """Revoke ``subject``'s viewer grant on ``document_id``. Idempotent.

        Revoking an already-revoked or never-granted share succeeds — revoke is
        END-STATE semantics ("``subject`` holds no viewer tuple on
        ``document_id``"), so retries and double-revokes must not surface a
        spurious error. Validation, the PDP, and audit still run and still
        raise their own typed errors exactly as for any other call; only the
        backend's OWN authoritative "tuple already absent" signal
        (:class:`~ne.permissions_rebac.errors.TupleAbsentError`) is swallowed
        at the delete step — that response IS the state revoke was trying to
        reach. Every OTHER backend failure (outage, auth, malformed request)
        still propagates as :class:`~ne.permissions_rebac.errors.BackendError`
        (C7): a revoke can never silently report success while access might
        still exist.

        The audit event is appended BEFORE the delete attempt (I-5,
        audit-then-mutate) — deliberately, so even an idempotent no-op revoke
        is audited: it records revocation INTENT, not just a state change.
        """
        subject_type, subject_id = self._validate(document_id, subject, relation)

        actor_is_owner = await self._is_owner(actor.id, document_id)
        decision = await self._pdp.decide(
            PolicyRequest(
                principal=actor,
                action="rebac.revoke",
                resource=f"document:{document_id}",
                context={"subject": subject, "actor_is_owner": actor_is_owner},
            )
        )
        if decision.effect is DecisionEffect.DENY:
            raise ShareNotPermittedError("rebac.revoke denied by policy")
        if decision.effect is DecisionEffect.REQUIRE_APPROVAL:
            raise ShareRequiresApprovalError("rebac.revoke requires approval", decision=decision)

        try:
            await self._sink.append(
                new_audit_event(
                    action="rebac.share.revoked",
                    resource=f"document:{document_id}",
                    outcome=AuditOutcome.success,
                    actor=actor.id,
                    provenance={
                        "subject": subject,
                        "relation": relation,
                        "policy_version": decision.policy_version,
                    },
                )
            )
        except Exception as exc:
            raise RebacAuditError("audit append failed; no tuple deleted") from exc

        # Idempotent: the backend's own "already absent" response IS the goal
        # state revoke seeks, so ONLY that specific error is swallowed here.
        # Any OTHER BackendError (outage, auth, malformed request) must
        # propagate untouched (C7) — never convert a fail-safe bug into a
        # silent authorization-integrity one.
        with suppress(TupleAbsentError):
            await self._client.delete_tuples(
                [self._viewer_tuple(subject_type, subject_id, document_id)]
            )

    # -- listing -------------------------------------------------------------
    async def list_shares(
        self, *, document_id: str | None = None, principal_id: str | None = None
    ) -> Sequence[Tuple3]:
        """Enumerate tuples by EXACTLY ONE selector (console/steward view)."""
        if (document_id is None) == (principal_id is None):
            raise ShareValidationError(
                "list_shares requires exactly one of document_id / principal_id"
            )
        if document_id is not None:
            return await self._client.read_tuples(object_filter=f"document:{document_id}")
        return await self._client.read_tuples(user_filter=f"user:{principal_id}")
