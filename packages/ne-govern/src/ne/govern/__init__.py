# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ne-govern — the C6 governance chokepoint + the C12 deterministic guardrails.

:class:`GovernedPdp` is the only :class:`~ne.contracts.governance.PolicyDecisionPoint`
in the package. It queries an OPA sidecar over HTTP (the versioned Rego bundle
under ``policies/`` encodes SPEC §4.2 approval tiers) and, by construction,
emits an ``AuditEvent`` to the frozen ``AuditSink`` BEFORE returning any
decision — there is no bypass path.

Alongside it, the C12 layer adds the pre-model, code-enforced guardrails a fully
prompt-injected agent cannot talk past: :class:`GuardrailEngine` runs spend caps,
rate limits, recipient/egress allowlists, and injection-resistant tool-arg
validation, auditing every BLOCK (mirroring the PDP's audit coupling); and the
backend-agnostic :class:`RedactionStage` strips PII fail-closed, defaulting to
Presidio (adopted via an isolated hash-pinned venv) with a stdlib
:class:`RegexRedactor` air-gap supplement. ``create_pdp`` /
``create_guardrail_engine`` / ``create_redactor`` / ``build_tool_call_payload``
are the NE-033 middleware seams (``sink`` is required wherever a block is audited).
"""

from __future__ import annotations

__version__ = "0.1.0"

from ne.govern.bundle import build_bundle, read_manifest_revision, read_policy_version
from ne.govern.config import GovernSettings
from ne.govern.emails import is_valid_email
from ne.govern.errors import (
    AuditEmissionError,
    BundleError,
    GuardrailConfigError,
    GuardrailEvaluationError,
    GuardrailViolation,
    NeGovernError,
    PolicyBackendError,
    RedactionConfigError,
    RedactionError,
)
from ne.govern.factory import create_guardrail_engine, create_pdp, create_redactor
from ne.govern.guardrails import (
    GroundingGuardrail,
    GuardrailEngine,
    InMemorySpendLedger,
    RateLimitGuardrail,
    RecipientAllowlistGuardrail,
    SlidingWindowCounter,
    SpendCapGuardrail,
    SqlAlchemySlidingWindowCounter,
    SqlAlchemySpendLedger,
    ToolArgGuardrail,
    VerifiedDomainGuardrail,
    build_tool_call_payload,
    ensure_guardrail_state_schema,
)
from ne.govern.pdp import GovernedPdp
from ne.govern.redaction import PresidioRedactor, RedactionStage, RegexRedactor

__all__ = [
    "AuditEmissionError",
    "BundleError",
    "GovernSettings",
    "GovernedPdp",
    "GroundingGuardrail",
    "GuardrailConfigError",
    "GuardrailEngine",
    "GuardrailEvaluationError",
    "GuardrailViolation",
    "InMemorySpendLedger",
    "NeGovernError",
    "PolicyBackendError",
    "PresidioRedactor",
    "RateLimitGuardrail",
    "RecipientAllowlistGuardrail",
    "RedactionConfigError",
    "RedactionError",
    "RedactionStage",
    "RegexRedactor",
    "SlidingWindowCounter",
    "SpendCapGuardrail",
    "SqlAlchemySlidingWindowCounter",
    "SqlAlchemySpendLedger",
    "ToolArgGuardrail",
    "VerifiedDomainGuardrail",
    "__version__",
    "build_bundle",
    "build_tool_call_payload",
    "create_guardrail_engine",
    "create_pdp",
    "create_redactor",
    "ensure_guardrail_state_schema",
    "is_valid_email",
    "read_manifest_revision",
    "read_policy_version",
]
