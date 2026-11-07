# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Typed errors for the ReBAC module — every one fail-closed by construction.

Credential-leak discipline (mirroring ``ne.permissions.sovereignty``): NO error
message ever interpolates the ``api_url`` or the ``api_token`` — only a parsed
hostname where a location is genuinely useful. A bad share, a pin mismatch, a
backend outage, or a failed audit append each raises rather than silently
degrading toward MORE access.
"""

from __future__ import annotations

__all__ = [
    "BackendError",
    "ModelPinError",
    "RebacAuditError",
    "RebacDisabledError",
    "RebacError",
    "RebacUnavailableError",
    "ShareNotPermittedError",
    "ShareRequiresApprovalError",
    "ShareValidationError",
    "TupleAbsentError",
    "UnsafeEndpointError",
]


class RebacError(Exception):
    """Base class for every ReBAC-module error."""


class BackendError(RebacError):
    """Internal: any OpenFGA backend / transport / timeout failure.

    Raised by :mod:`ne.permissions_rebac._client` at the confined SDK boundary
    with a hostname-only message. The resolver TRANSLATES it per mode (degrade ->
    ∅, deny -> :class:`RebacUnavailableError`); ``ShareService`` treats it as a
    hard failure in both modes. Never carries the ``api_url`` or the token.
    """


class RebacDisabledError(RebacError):
    """A ReBAC component was constructed/invoked while the module is disabled.

    ``RebacSettings.enabled is False`` means the platform filter is identical to
    today and NOTHING in this module should be wired. Constructing the client,
    resolver, or share service against disabled settings is a configuration bug,
    grep-provable and fail-closed.
    """


class RebacUnavailableError(RebacError):
    """The OpenFGA backend was unreachable/timed-out/5xx in ``deny`` mode.

    In ``degrade`` mode the resolver instead returns an empty set (the union
    collapses to the source leg — never wider). This error is the ``deny``-mode
    propagation (deny-by-erroring). ``share``/``revoke`` raise this in BOTH modes.
    """


class TupleAbsentError(BackendError):
    """Raised ONLY by ``delete_tuples`` when the backend rejects deleting a tuple
    that does not exist.

    The real OpenFGA server 400s a transactional delete of an absent tuple —
    ``code=write_failed_due_to_invalid_input``, message ``"cannot delete a
    tuple which does not exist"``. :mod:`ne.permissions_rebac._client` maps
    EXACTLY that signature to this type and nothing else; every other backend
    failure (outage, auth, malformed request, a genuinely different delete
    rejection) stays a plain :class:`BackendError`.

    It subclasses :class:`BackendError` so every existing translate/fail-closed
    path (resolver degrade/deny, gc's loud-abort) is unaffected by construction
    — a caller that only knows about ``BackendError`` still catches this.
    :meth:`~ne.permissions_rebac.share.ShareService.revoke` catches EXACTLY
    this type to deliver idempotent revoke; nothing else is swallowed. Like
    every error in this module, the message never carries the ``api_url`` or
    the token — only a static, hostname-free string.
    """


class ModelPinError(RebacError):
    """The pinned authorization-model id is empty (while enabled) or drifted.

    A server whose model no longer equals the in-repo pinned model is unreachable
    by construction — every call carries the pinned id, and ``verify_model_pin``
    fails closed on any structural drift.
    """


class ShareValidationError(RebacError):
    """A share/revoke request failed structural validation BEFORE any backend call.

    Bad/empty ids, a reserved ``acl:*`` namespace in a subject/object, a
    caller-supplied ``#`` in the subject, an unknown/tombstoned subject, or a
    relation other than ``viewer``.
    """


class ShareNotPermittedError(RebacError):
    """The share was refused: share-requires-read failed, or the PDP returned DENY."""


class ShareRequiresApprovalError(RebacError):
    """The PDP returned REQUIRE_APPROVAL — no tuple was written.

    Carries the :class:`~ne.contracts.governance.PolicyDecision`; routing the
    request through the NE-035 approval gate is O-4 wiring.
    """

    def __init__(self, message: str, *, decision: object | None = None) -> None:
        super().__init__(message)
        self.decision = decision


class RebacAuditError(RebacError):
    """The audit sink failed to append — the share/revoke fails closed, no mutation.

    Audit-then-mutate: an audited-but-absent grant is the safe direction; a
    mutation whose audit event never landed is never permitted.
    """


class UnsafeEndpointError(ValueError):
    """A config-supplied OpenFGA ``api_url`` is SSRF-shaped or not provably in-VPC.

    Raised at CONSTRUCTION time (fail-closed) so a bad endpoint never receives the
    preshared key. Messages name ONLY the parsed hostname (never the full URL /
    any secret).
    """
