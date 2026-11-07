# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Typed errors for the verified-answer layer (NE-085c).

Kept in their own module so both the load-time (``templates``) and request-time
(``scope``/``answers``) surfaces can raise them without an import cycle.
"""

from __future__ import annotations

__all__ = [
    "TemplateRegistryError",
    "VerifiedAnswerArgsError",
    "VerifiedAnswerCountError",
    "VerifiedAnswerScopeError",
    "VerifiedTemplateConflictError",
    "VerifiedTemplateGateError",
]


class TemplateRegistryError(ValueError):
    """A template failed LOAD-time validation (boot fails, never a per-request error)."""


class VerifiedAnswerArgsError(ValueError):
    """Caller args failed the template's strict typed-args model (before any store read)."""


class VerifiedAnswerScopeError(RuntimeError):
    """A fail-closed C7 scope denial: empty scope, or an out-of-dataset citation."""


class VerifiedAnswerCountError(RuntimeError):
    """The trusted COUNT rewrite returned a malformed result — fail closed, never a
    fabricated total (NE-CTXB-7, SPEC §0-law-5).

    Raised when the ``total_rows`` COUNT(*) probe returns anything other than exactly
    one row carrying a non-negative integer ``ne_total``. A probe TIMEOUT is NOT this
    error (it yields an honest ``total_rows=None``); this is reserved for a structurally
    impossible result the store should never produce.
    """


class VerifiedTemplateConflictError(ValueError):
    """A governed-store append hit an existing ``(template_id, version)`` with a
    DIFFERENT ``toml_sha256`` (NE-ONTOLOGY-FLYWHEEL).

    Fail-loud divergence: two distinct template bodies can never share one
    ``(id, version)`` slot — the steward re-proposes at the next version.
    """


class VerifiedTemplateGateError(RuntimeError):
    """A governed template apply was refused because the NE-066 gate did not report
    ``APPROVED`` for this proposal (or the proposal is not the verified-answer
    kind). Fail-closed — nothing is persisted (the ``apply_merge`` F5 kind-guard).
    """
