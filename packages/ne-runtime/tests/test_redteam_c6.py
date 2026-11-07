# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-REDTEAM — adversarial attacks against the C6 governed executor.

Each test drives a REAL :class:`~ne.runtime.middleware.GovernedToolExecutor`
(built via the conftest ``make_executor`` seam — real PDP, real guardrails, real
approval gate, real audit sink) with a HOSTILE input and asserts the chokepoint
DEFEATS it: no un-PDP'd handler runs, every attempt is audited, and forged
principal context / internal-caller position grants no trust. This is the
adversarial complement to ``test_middleware_c6.py`` (the existence proof).

Every test states its MUTATION-CHECK: the exact source revert that makes it FAIL,
proving it is non-vacuous.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from ne.contracts.types import Principal
from ne.runtime.errors import (
    ApprovalIntegrityError,
    ToolDeniedError,
    ToolExecutionError,
)
from ne.runtime.tools import ToolBinding

from .conftest import (
    ALLOWED_RECIPIENT,
    AutoApprovingGate,
    NamedApproverGate,
    RecordingAuditSink,
    ToolLog,
    UnusedGate,
    make_executor,
    make_principal,
    standard_bindings,
)

# A forged, self-asserted "I am an elevated superuser at tier 0" principal. The
# governance plane must ignore EVERY one of these claims — tiering is decided from
# the tool spec + the tiers.rego mirror + the PDP decision, never from caller-
# supplied identity context.
_FORGED_ELEVATED = Principal(
    id="user:evil",
    groups=("finance-admins", "platform-admins"),
    claims={"tier": "0", "role": "admin", "is_superuser": "true"},
)


def _outcomes(sink: RecordingAuditSink) -> list[tuple[str, str]]:
    return [(getattr(e, "action", ""), str(e.outcome)) for e in sink.events]


def _pdp_decides_for(sink: RecordingAuditSink, resource: str) -> list[Any]:
    return [
        e
        for e in sink.events
        if getattr(e, "action", "") == "pdp.decide" and getattr(e, "resource", "") == resource
    ]


# --- (a) forged elevated claims cannot lower the HITL gate tier ---------------


def test_forged_elevated_principal_claims_cannot_lower_gating() -> None:
    """A Tier-3 tool still routes through the human gate even when the caller
    forges ``claims['tier']='0'`` + ``is_superuser`` — the gate IS consulted and
    the handler runs ONLY because a valid NAMED approval was granted.

    MUTATION-CHECK: if ``_effective_tier`` (ne/runtime/middleware.py:87) trusted
    ``principal.claims['tier']`` instead of ``max(spec, mirror, decided)``, the
    Tier-3 finance call would gate at 0 and SKIP the approval gate → gate.requests
    empty → this test fails.
    """
    log = ToolLog()
    sink = RecordingAuditSink()
    gate = NamedApproverGate("alice")
    executor = make_executor(standard_bindings(log), sink, approval_gate=gate)

    result = asyncio.run(
        executor.execute("finance.payment.initiate", {"amount": 10}, principal=_FORGED_ELEVATED)
    )

    assert result == "paid"
    assert log.names() == ["finance.payment.initiate"]
    # The human gate WAS consulted at Tier-3 — the forged tier=0 claim was ignored.
    assert gate.requests, "the HITL gate was never consulted (forged tier lowered gating!)"
    assert int(gate.requests[0].tier) == 3
    assert ("tool.approval", "success") in _outcomes(sink)


# --- (b) forged elevated claims + auto-approve trap: never runs ---------------


def test_forged_principal_tier3_with_no_named_approver_never_runs() -> None:
    """The same forged-superuser principal + an APPROVED-but-unnamed approval is an
    integrity violation: the Tier-3 handler NEVER runs.

    MUTATION-CHECK: reverting the named-approver requirement in
    ``_require_named_approval`` (ne/runtime/middleware.py:243-266) — accepting an
    APPROVED outcome without a named approver — lets the payment execute.
    """
    log = ToolLog()
    sink = RecordingAuditSink()
    executor = make_executor(standard_bindings(log), sink, approval_gate=AutoApprovingGate())

    with pytest.raises(ApprovalIntegrityError):
        asyncio.run(
            executor.execute(
                "finance.payment.initiate", {"amount": 999999}, principal=_FORGED_ELEVATED
            )
        )

    assert log.names() == []
    assert ("tool.execute", "success") not in _outcomes(sink)
    assert ("tool.approval", "denied") in _outcomes(sink)


# --- (c) a denied decision is RE-EVALUATED on retry (no allow cache) ----------


def test_denied_pdp_decision_is_re_evaluated_on_retry_no_allow_cache() -> None:
    """Calling a denied tool twice re-consults the PDP BOTH times — there is no
    per-principal/per-action decision cache that could go stale-open.

    MUTATION-CHECK: a decision cache that remembered the first decision (or a
    short-circuit that skipped ``self._pdp.decide`` on a repeat) would emit only
    ONE ``pdp.decide`` event → this test (which asserts exactly two) fails.
    """
    log = ToolLog()
    sink = RecordingAuditSink()
    executor = make_executor(
        standard_bindings(log),
        sink,
        approval_gate=UnusedGate(),
        pdp_overrides={
            "read.docs.search": {
                "effect": "deny",
                "tier": 0,
                "reason": "explicitly denied",
                "obligations": {},
                "policy_version": "1.0.0",
            }
        },
    )
    principal = make_principal()

    for _ in range(2):
        with pytest.raises(ToolDeniedError):
            asyncio.run(executor.execute("read.docs.search", {"q": "x"}, principal=principal))

    decides = _pdp_decides_for(sink, "tool:read.docs.search")
    assert len(decides) == 2, "the PDP was not re-consulted on retry (decision cached?)"
    assert all(str(e.outcome) == "denied" for e in decides)
    assert log.names() == []


# --- (d) re-entrant / nested execution is INDEPENDENTLY governed --------------


def test_reentrant_nested_tool_call_is_independently_governed() -> None:
    """A handler that (having been handed the executor) re-enters
    ``executor.execute`` for a DENIED Tier-3 tool is still denied — being "already
    inside" the executor grants ZERO trust; the nested call is PDP'd afresh and the
    denied finance handler NEVER runs.

    MUTATION-CHECK: if the executor cached an internal-caller/"already-governed"
    flag that bypassed the PDP for nested calls, the nested finance tool would
    execute un-PDP'd (no ``pdp.decide`` for it, and the finance handler would run).
    """
    log = ToolLog()
    sink = RecordingAuditSink()
    holder: dict[str, Any] = {}

    async def _reentrant_handler(args: Any) -> str:
        # Re-enter the SAME executor for a Tier-3 tool the PDP denies.
        return await holder["ex"].execute(
            "finance.payment.initiate", {"amount": 1}, principal=make_principal()
        )

    bindings = standard_bindings(log)
    # Replace the (allowed, Tier-0) read tool's handler with the re-entrant one.
    bindings[0] = ToolBinding(spec=bindings[0].spec, handler=_reentrant_handler)

    executor = make_executor(
        bindings,
        sink,
        approval_gate=NamedApproverGate("alice"),
        pdp_overrides={
            "finance.payment.initiate": {
                "effect": "deny",
                "tier": 3,
                "reason": "nested finance denied",
                "obligations": {},
                "policy_version": "1.0.0",
            }
        },
    )
    holder["ex"] = executor

    # The outer (allowed) tool runs its handler, which re-enters for the denied
    # tool; the propagated ToolDeniedError surfaces as a ToolExecutionError.
    with pytest.raises(ToolExecutionError):
        asyncio.run(executor.execute("read.docs.search", {"q": "x"}, principal=make_principal()))

    # The nested finance tool was PDP-denied and its handler never ran.
    assert "finance.payment.initiate" not in log.names()
    finance_decides = _pdp_decides_for(sink, "tool:finance.payment.initiate")
    assert len(finance_decides) == 1
    assert str(finance_decides[0].outcome) == "denied"


# --- (e) C12: a secret arg VALUE never appears in any audit record ------------


def test_c12_secret_value_in_tool_args_never_appears_in_audit_detail() -> None:
    """A sentinel secret planted as a tool ARGUMENT VALUE must not leak into ANY
    audit event's detail/provenance/serialization — the executor audits names,
    tiers, statuses and error classes only, never raw arg values (C12).

    MUTATION-CHECK: if the executor serialized raw ``args`` into the ``tool.execute``
    (or any) audit ``detail`` — e.g. ``detail={"args": dict(args)}`` in
    ``_run_handler`` — the sentinel would appear in the serialized event.
    """
    sentinel = "NE-REDTEAM-SECRET-a1b2c3d4"
    log = ToolLog()
    sink = RecordingAuditSink()
    executor = make_executor(standard_bindings(log), sink, approval_gate=UnusedGate())

    asyncio.run(
        executor.execute(
            "write.internal.notes.append", {"note": sentinel}, principal=make_principal()
        )
    )

    assert log.names() == ["write.internal.notes.append"]
    assert sink.events, "the run produced no audit events at all"
    for event in sink.events:
        blob = json.dumps(
            {
                "detail": getattr(event, "detail", None),
                "provenance": dict(getattr(event, "provenance", {})),
            }
        )
        assert sentinel not in blob
        assert sentinel not in str(event)
        model_dump = getattr(event, "model_dump_json", None)
        if callable(model_dump):
            assert sentinel not in str(model_dump())
    # Sanity: also confirm the happy-path allowlisted recipient constant is not the
    # thing under test — the sentinel is the only planted secret here.
    assert ALLOWED_RECIPIENT not in sentinel
