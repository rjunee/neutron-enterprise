# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Exception hierarchy for ne-govern (the C6 governance chokepoint).

Both failure surfaces fail CLOSED: :class:`PolicyBackendError` (OPA unreachable
or an undefined/malformed decision) and :class:`AuditEmissionError` (the audit
append that MUST precede returning a decision failed) each abort the decision —
no ``PolicyDecision`` is ever returned when either fires.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ne.contracts.governance import GuardrailVerdict


class NeGovernError(Exception):
    """Base class for every error raised by ne-govern."""


class PolicyBackendError(NeGovernError):
    """The OPA policy backend was unreachable or returned an unusable result.

    Raised for a transport failure, a non-200 response, an OPA ``undefined``
    result (missing ``result`` key), or a decision document that violates the
    frozen contract (unknown effect, empty policy version). Fail closed.
    """


class AuditEmissionError(NeGovernError):
    """The audit sink append failed, so the decision is WITHHELD (C6, fail closed).

    A decision that cannot be audited is never returned: the PDP raises this
    instead of yielding a ``PolicyDecision`` the audit chain does not record.
    """


class BundleError(NeGovernError):
    """A policy bundle could not be read or built (missing manifest, bad version)."""


class GuardrailConfigError(NeGovernError):
    """Boot-time rejection of an invalid :class:`~ne.govern.config.GuardrailSettings` tree.

    Mirrors ne-gateway's ``GatewayConfigError`` (fail-loud-at-construction,
    name-the-exact-bad-key). A malformed guardrail config value — e.g. a spend
    cap string that is not a valid finite decimal — must never silently disable
    the control it configures (C12 reject-by-default): it is rejected here, at
    settings construction, before any guardrail is ever built.
    """


class GuardrailViolation(NeGovernError):
    """One or more deterministic guardrails BLOCKED the call (C12, pre-model).

    Raised by :class:`~ne.govern.guardrails.engine.GuardrailEngine` only AFTER the
    block has been recorded to the ``AuditSink`` — a blocked call is never denied
    without an audit event. ``verdicts`` carries the FAILED verdicts (rule name +
    arg-key/violation-class detail only; never raw arg/recipient/context values).
    """

    def __init__(self, verdicts: tuple[GuardrailVerdict, ...]) -> None:
        self.verdicts = verdicts
        rules = ", ".join(v.rule for v in verdicts)
        super().__init__(f"guardrails blocked the call: {rules}")


class GuardrailEvaluationError(NeGovernError):
    """A guardrail raised internally; the call is DENIED fail-closed (C12).

    A guardrail predicate that cannot complete deterministically is treated as a
    block, not a pass: the engine audits a ``guardrail.error`` outcome and raises
    this instead of letting the call through.
    """


class RedactionConfigError(NeGovernError):
    """Boot-time rejection of an invalid redaction configuration (fail loud).

    The sibling of :class:`GuardrailConfigError` for the C12 redaction layer.
    Raised by :class:`~ne.govern.redaction.exempt.AllowlistExemptRedactor` when
    an exemption entry is not a full bare email address: such an entry would
    become a SUBSTRING wildcard un-masking every address that contains it, so
    the platform refuses to construct the redactor at all rather than boot with
    a silently widened exemption.
    """


class RedactionError(NeGovernError):
    """A redaction backend failed, so the text is WITHHELD (fail closed, C12).

    Unredacted input never escapes a failed redaction: the stage raises this
    rather than returning text that may still carry PII.
    """
