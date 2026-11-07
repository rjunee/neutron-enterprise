# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Config-driven construction — the NE-033 middleware seams.

``create_pdp`` resolves the ``ne.plugins.policy_decision_point`` entry point
named by ``settings.pdp`` (a programmatic ``registry.register`` override wins)
and constructs it. The ``sink`` is a REQUIRED keyword-only argument: there is no
sink-less path, so the C6 audit coupling cannot be omitted when NE-033 wires the
PDP as non-optional middleware.

``create_guardrail_engine`` assembles the four deterministic C12 guardrails from
``settings.guardrails`` and couples them to the same required ``sink`` (mirroring
the PDP). ``create_redactor`` resolves the ``ne.plugins.redactor`` entry point
named by ``settings.redactor`` (adopt-first default: Presidio) and wraps it in
the fail-closed :class:`~ne.govern.redaction.stage.RedactionStage`. These three
are the seams NE-033 wires non-optionally around every tool call.
"""

from __future__ import annotations

from collections.abc import Sequence

from ne.contracts.governance import AuditSink, Guardrail, PolicyDecisionPoint, Redactor
from ne.contracts.plugins import PluginRegistry
from ne.govern.config import GovernSettings
from ne.govern.guardrails.engine import GuardrailEngine
from ne.govern.guardrails.rate import RateLimitGuardrail, SlidingWindowCounter
from ne.govern.guardrails.recipients import RecipientAllowlistGuardrail
from ne.govern.guardrails.spend import InMemorySpendLedger, SpendCapGuardrail, SpendLedger
from ne.govern.guardrails.toolargs import ToolArgGuardrail
from ne.govern.redaction.exempt import AllowlistExemptRedactor
from ne.govern.redaction.stage import RedactionStage


def create_pdp(
    settings: GovernSettings | None = None,
    *,
    sink: AuditSink,
    registry: PluginRegistry | None = None,
) -> PolicyDecisionPoint:
    """Resolve and construct the configured PDP, coupled to ``sink`` (required)."""
    settings = settings or GovernSettings()
    registry = registry or PluginRegistry()
    cls = registry.resolve("policy_decision_point", settings.pdp)
    if hasattr(cls, "from_settings"):
        return cls.from_settings(settings, sink=sink)  # type: ignore[no-any-return]
    return cls(sink=sink)  # type: ignore[no-any-return]


def create_guardrail_engine(
    settings: GovernSettings | None = None,
    *,
    sink: AuditSink,
    ledger: SpendLedger | None = None,
    counter: SlidingWindowCounter | None = None,
    extra_guardrails: Sequence[Guardrail] = (),
    registry: PluginRegistry | None = None,
) -> GuardrailEngine:
    """Assemble the four C12 guardrails from settings, coupled to ``sink`` (required).

    ``ledger`` / ``counter`` are the spend ledger + rate counter the engine records
    into; a composition root running MORE THAN ONE executor (session + sub-agent
    dispatch + cron) MUST construct ONE pair (see
    :func:`ne.runtime.factory.create_shared_guardrail_state`) and pass it to EVERY
    ``create_guardrail_engine`` so a principal's/department's spend cap and rate
    limit are tallied ONCE platform-wide, not multiplied per executor (C12,
    boundary-audit finding #1). When omitted, a fresh in-memory pair is built.

    ``extra_guardrails`` are ADDITIONAL deterministic rails a composition root
    injects, APPENDED after the four base rails (which stay FIRST and unchanged).
    The canonical use is ne-identity's ``AgentScopeGuardrail`` (the sole enforcer
    of ``CredentialScope.tools``, NE-036) — named here in prose only, never
    imported: the param is typed by the frozen
    :class:`ne.contracts.governance.Guardrail` Protocol PRECISELY so injecting a
    cross-package rail never forces ne-govern (or ne-runtime) to import the rail's
    package, keeping the C3 import ban intact. Extras must be ADDITIONAL rails, not
    duplicates of the base four: an injected second :class:`SpendCapGuardrail`
    would be double-charged in :meth:`GuardrailEngine._reserve_on_pass`
    (over-restrictive, not a bypass) — no isinstance guard is added, since
    rejecting duplicates would couple the Protocol seam to concrete rail types.
    """
    settings = settings or GovernSettings()
    guardrails_cfg = settings.guardrails
    ledger = ledger or InMemorySpendLedger()
    counter = counter or SlidingWindowCounter()
    guardrails: list[Guardrail] = [
        SpendCapGuardrail(guardrails_cfg.spend, ledger),
        RateLimitGuardrail(guardrails_cfg.rate, counter),
        RecipientAllowlistGuardrail(guardrails_cfg.recipients),
        ToolArgGuardrail(guardrails_cfg.tools),
        *extra_guardrails,
    ]
    return GuardrailEngine(guardrails, sink, rate_counter=counter, spend_ledger=ledger)


def create_redactor(
    settings: GovernSettings | None = None,
    *,
    registry: PluginRegistry | None = None,
    exempt_addresses: Sequence[str] = (),
) -> Redactor:
    """Resolve the configured redaction backend and wrap it in the fail-closed stage.

    ``exempt_addresses`` are exact, deployment-owned email addresses (the send
    allowlist and the configured draft mailboxes) that pre-egress redaction must
    NOT mask — see :class:`ne.govern.redaction.exempt.AllowlistExemptRedactor`
    for the policy statement and its (frontier-egress-GLOBAL) exposure surface.
    Empty (the default) is byte-identical to the previous behavior.

    Every entry must be a FULL bare email address. A shorter entry — a bare
    domain, a bare local part, a non-address mailbox identity — is a substring
    wildcard, not an address, and the wrapper raises
    :class:`~ne.govern.errors.RedactionConfigError` rather than build it. This
    seam is called from a composition root at boot, so that refusal is a refused
    boot: never a silently widened exemption (NE-U8-F1).
    """
    settings = settings or GovernSettings()
    registry = registry or PluginRegistry()
    resolved = registry.resolve("redactor", settings.redactor)
    backend = resolved() if isinstance(resolved, type) else resolved
    stage = RedactionStage(backend)
    if exempt_addresses:
        return AllowlistExemptRedactor(stage, exempt_addresses)
    return stage
