# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Typed errors for the Skill Forge read side.

:class:`SkillNotFoundError` carries a message that is IDENTICAL for an absent, an
ACL-invisible, and a hash-tampered skill on the ``read.skills.view`` path — no
existence/tamper oracle to the caller (the NE-043 retrieve-by-id posture).
"""

from __future__ import annotations


class SkillError(Exception):
    """Base class for the read side's typed errors."""


class SkillNotFoundError(SkillError):
    """A skill is absent, ACL-invisible, or hash-tampered (indistinguishable).

    The message is EXACTLY ``skill not found: {name}`` for all three cases so the
    caller learns nothing about existence or tampering (no oracle).
    """

    def __init__(self, name: str) -> None:
        super().__init__(f"skill not found: {name}")
        self.name = name


class SkillAuthoringUnavailableError(SkillError):
    """``author`` is not implemented on the read-side binding (lands in 1b)."""

    def __init__(self) -> None:
        super().__init__("skill authoring lands in NE-SKILL-1b; this binding is read-side only")


class PrincipalUnboundError(SkillError):
    """No tool principal is bound — fail closed (the tools.py:62-64 pattern).

    The executor audits this as a ``tool.execute`` error.
    """

    def __init__(self) -> None:
        super().__init__("no tool principal bound; refusing skill access")


class SkillAuthoringParseError(SkillError):
    """The author agent's output could not be parsed into a valid draft pack.

    Raised on any parse/validation failure of the model output (missing
    frontmatter, malformed YAML, a house-standard violation before construction).
    The forge counts this as an ``authoring_failed`` and advances the cursor
    (skip-on-failure) — a stuck cursor is worse than one skipped snapshot.
    """


class SkillForgeAuditError(SkillError):
    """A forge run's dual-append to the audit chain failed (fail closed, C6).

    The whole forge run rolled back — no proposal, no cursor advance, no outbox
    event — so nothing persisted without its tamper-evident audit record. Mirrors
    ``CurationAuditError``.
    """


class SkillApplyError(SkillError):
    """Base class for the ``apply-skill`` steward-seam errors (NE-SKILL-1b)."""


class SkillApplyConfigError(SkillApplyError):
    """The review-service engine and the skills engine are not the same database.

    The one-transaction apply seam (R-20) requires the registry writes to ride
    the review service's connection, so both MUST target one database URL. Raised
    fail-closed BEFORE any write.
    """


class SkillApplyGateError(SkillApplyError):
    """The proposal is the wrong kind or not in an approvable state.

    Raised when the proposal is not a ``skill_candidate`` /
    ``skill_update_candidate`` or its folded status is not ``approved``.
    """


class SkillApplyIntegrityError(SkillApplyError):
    """The stored payload pack failed its recomputed content-hash check.

    A poisoned/tampered stored payload (body edited, stale hash) is refused — the
    recomputed ``content_hash`` must equal the payload's declared hash.
    """


class SkillApplyLabelError(SkillApplyError):
    """The apply's ACL-label decision is unsafe or names an unknown label.

    Raised when neither an explicit global-visibility decision nor a non-empty
    label set is supplied (R-22: blank labels never publish fleet-wide by
    accident), or when a supplied label fails the validator (D-6: reject
    unknown).
    """


class SkillApplyVersionError(SkillApplyError):
    """The apply violates version discipline (new-vs-update / stale version).

    A ``skill_candidate`` must be version 1 with a name absent from the registry;
    a ``skill_update_candidate`` must name an existing skill at ``current + 1``. A
    stale update (the registry moved on) is refused (409-mapped at the API).
    """


class SkillEvalGateError(SkillApplyGateError):
    """The fail-closed BIRTH eval gate (NE-SKILL-2) refused the candidate.

    Raised at the apply seam (and the kind-guarded steward approve route) when a
    ``skill(_update)_candidate``'s payload lacks a PASSING, store-anchored
    ``SkillTriggerEvalSuite`` run whose ``routing_hash`` matches the recomputed
    pack ``routing_hash`` — the moat: a pack that cannot route to itself (or a
    forged/tampered eval ref) is neither approvable nor applicable. Subclasses
    :class:`SkillApplyGateError`, so the existing steward-route error table maps
    it to 409 with ZERO changes.
    """
