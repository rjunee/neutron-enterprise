# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""C12 deterministic guardrails — code-enforced checks that run BEFORE the model.

Every guardrail here is **sync, deterministic, and pre-model**: no LLM call, no
network, no model judgment. They are the code-enforced backstops a fully
prompt-injected / hijacked agent cannot talk past. Injection is ASSUMED; these
checks reject-by-default on any unknown tool, argument, or shape.

The canonical *tool-call payload* every guardrail's ``check`` consumes is a plain
``Mapping[str, Any]`` with these keys (build it with :func:`build_tool_call_payload`):

    principal          ne.contracts.types.Principal   — the authenticated subject
    tool               str                            — the tool/action name
    args               Mapping[str, Any]              — the tool-call arguments
    recipients         Sequence[str]                  — outbound recipients (emails)
    egress_urls        Sequence[str]                  — outbound egress targets (URLs)
    estimated_cost_usd str | Decimal | None           — pre-call cost estimate

The optional verified-routing keys ``verified_domain``/``routed`` are set only
by the verified-answer router (NE-VAF-U3), and the optional grounding-gate keys
``grounded_gate``/``grounded`` only by the grounded-agent runtime post-step
(NE-GA-G3) — never by ``build_tool_call_payload``.
:class:`~ne.govern.guardrails.verified.VerifiedDomainGuardrail` and
:class:`~ne.govern.guardrails.grounding.GroundingGuardrail` are the only rails
that read them.

A guardrail returns a :class:`~ne.contracts.governance.GuardrailVerdict`: a
normal deny is a *returned* failed verdict (``passed=False``), never a raise. A
payload missing ``principal`` or ``tool`` yields a failed verdict
(reject-by-default). Rule names are namespaced (``'spend.cap-exceeded'``,
``'recipients.not-allowlisted'``, …). Per the audit-redaction discipline that
mirrors :mod:`ne.govern.pdp`, ``GuardrailVerdict.detail`` NEVER echoes raw
argument, recipient, or context VALUES — only the offending arg key and the
violation class.

The guardrails are side-effect-free predicates (the frozen ``Guardrail``
Protocol is sync and stateless); the mutating hooks (rate-counter increment,
spend pre-charge) live in :class:`~ne.govern.guardrails.engine.GuardrailEngine`
on the all-pass path. ``GuardrailEngine.enforce`` /
``ne.govern.create_guardrail_engine`` / ``build_tool_call_payload`` are the
documented seams NE-033 wires non-optionally around every tool call.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from ne.govern.guardrails.durable import (
    SqlAlchemySlidingWindowCounter,
    SqlAlchemySpendLedger,
    ensure_guardrail_state_schema,
)
from ne.govern.guardrails.engine import GuardrailEngine
from ne.govern.guardrails.grounding import GroundingGuardrail
from ne.govern.guardrails.rate import RateLimitGuardrail, SlidingWindowCounter
from ne.govern.guardrails.recipients import RecipientAllowlistGuardrail
from ne.govern.guardrails.spend import (
    InMemorySpendLedger,
    SpendCapGuardrail,
    SpendLedger,
)
from ne.govern.guardrails.toolargs import ToolArgGuardrail
from ne.govern.guardrails.verified import VerifiedDomainGuardrail

# Canonical payload keys — the single source of truth shared by every guardrail.
KEY_PRINCIPAL = "principal"
KEY_TOOL = "tool"
KEY_ARGS = "args"
KEY_RECIPIENTS = "recipients"
KEY_EGRESS_URLS = "egress_urls"
KEY_ESTIMATED_COST = "estimated_cost_usd"


def build_tool_call_payload(
    *,
    principal: Any,
    tool: str,
    args: Mapping[str, Any] | None = None,
    recipients: Sequence[str] = (),
    egress_urls: Sequence[str] = (),
    estimated_cost_usd: str | Decimal | None = None,
) -> dict[str, Any]:
    """Assemble the canonical guardrail payload (the NE-033 seam helper).

    Normalizes optional collections to concrete tuples/dicts so downstream
    guardrails see a stable shape regardless of how the caller supplied them.
    """
    return {
        KEY_PRINCIPAL: principal,
        KEY_TOOL: tool,
        KEY_ARGS: dict(args) if args is not None else {},
        KEY_RECIPIENTS: tuple(recipients),
        KEY_EGRESS_URLS: tuple(egress_urls),
        KEY_ESTIMATED_COST: estimated_cost_usd,
    }


__all__ = [
    "KEY_ARGS",
    "KEY_EGRESS_URLS",
    "KEY_ESTIMATED_COST",
    "KEY_PRINCIPAL",
    "KEY_RECIPIENTS",
    "KEY_TOOL",
    "GroundingGuardrail",
    "GuardrailEngine",
    "InMemorySpendLedger",
    "RateLimitGuardrail",
    "RecipientAllowlistGuardrail",
    "SlidingWindowCounter",
    "SpendCapGuardrail",
    "SpendLedger",
    "SqlAlchemySlidingWindowCounter",
    "SqlAlchemySpendLedger",
    "ToolArgGuardrail",
    "VerifiedDomainGuardrail",
    "build_tool_call_payload",
    "ensure_guardrail_state_schema",
]
