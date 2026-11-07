# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Exception hierarchy for ne-flywheel (capture NE-064, curation NE-065, review NE-066)."""

from __future__ import annotations


class FlywheelError(Exception):
    """Base class for every error raised by ne-flywheel."""


class SignalRejectedError(FlywheelError):
    """A correction-signal emit was rejected before any write (fail closed).

    Raised for an unauthenticated/provenance-missing emit (empty principal,
    missing/unknown surface, empty session_id), an unknown ``signal_kind``, or
    a wrong ``event_type`` — nothing is persisted to the store or the outbox
    when this is raised.
    """


class CurationError(FlywheelError):
    """Base class for errors raised by the NE-065 curation engine."""


class CurationAuditError(CurationError):
    """A curation run's dual-append to the audit chain failed (fail closed, C6).

    The whole curation transaction rolled back — no proposals, no cursor
    advance, no outbox event — so nothing persisted without its tamper-evident
    audit record.
    """


class ReviewError(FlywheelError):
    """Base class for errors raised by the NE-066 propose-then-approve gate."""


class UnknownProposalError(ReviewError):
    """A review transition/status was requested for an unknown proposal.

    Raised (fail closed, nothing persisted) when ``proposal_id`` is absent from
    ``ne_fix_proposals`` — the review gate only ever transitions proposals the
    curation engine actually appended.
    """


class InvalidTransitionError(ReviewError):
    """An illegal review state-machine transition was requested (fail closed).

    Raised for any transition the ``_ALLOWED`` map forbids — a double-approve, a
    ``proposed -> applied`` skip of the human approval step, or any transition
    out of the terminal ``rejected``/``applied`` states. Also raised when the
    unique ``(proposal_id, action)`` index rejects a concurrent duplicate INSERT
    (true multi-worker serialization is LIVE-PARKED to NE-070). Nothing is
    persisted when this is raised.
    """


class ReviewAuditError(ReviewError):
    """A review transition's dual-append to the audit chain failed (fail closed, C6).

    The WHOLE review transaction rolled back — the review row, any rejection
    correction signal, and its outbox mirror — so nothing persisted without its
    tamper-evident audit record. Mirrors :class:`CurationAuditError`.
    """
