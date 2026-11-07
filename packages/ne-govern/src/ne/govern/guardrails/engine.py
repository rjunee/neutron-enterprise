# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""GuardrailEngine — runs the C12 guardrails and AUDITS every block, by construction.

Mirrors :class:`ne.govern.pdp.GovernedPdp`'s C6 coupling so the two compose into
the C12 invariant (a fully hijacked agent is blocked in code OR gated to a human,
for every scenario):

  * the ``AuditSink`` is a REQUIRED positional argument — there is no sink-less
    ``GuardrailEngine``, so a block cannot be emitted without an audit trail;
  * on any FAILED verdict, exactly ONE ``guardrail.block`` ``AuditEvent`` is
    appended BEFORE :class:`~ne.govern.errors.GuardrailViolation` is raised;
  * on the all-pass path the engine performs the ATOMIC AUTHORITATIVE reservation
    (:meth:`_reserve_on_pass`, NE-070H): the rate counter and spend ledger enforce
    the cap by the conditional write itself, so concurrent multi-worker chargers
    cannot both pass ``check()`` and overshoot the cap. A reservation loss rides
    the SAME audited-block + ``GuardrailViolation`` path as a check-time block
    (same rule vocabulary), so C12 is strengthened, never weakened. ``check()``
    stays the side-effect-free fast pre-check;
  * if a guardrail raises internally, a ``guardrail.error`` event is appended and
    :class:`~ne.govern.errors.GuardrailEvaluationError` is raised (fail closed);
  * if the audit append itself fails, :class:`~ne.govern.errors.AuditEmissionError`
    is raised (chained) and NO decision escapes un-audited;
  * ``new_audit_event`` construction (which resolves the FROZEN config hash) is
    OUTSIDE the try, so ``ConfigHashNotFrozenError`` propagates rather than being
    swallowed into ``AuditEmissionError`` (plan §1.4 / SPEC §4).

Audit-redaction discipline (mirrors :mod:`ne.govern.pdp`): raw argument,
recipient, and context VALUES never travel into the audit ``detail`` or
``provenance`` — only rule names and violation-class details (which name arg keys
and violation classes, never values).

RUN-TRACE THREADING (NE-DEMO-PDP-TRACE, NE-166): ``guardrail.block`` and
``guardrail.error`` carry ``trace_id=current_run_trace_id()`` — the ambient
run-scoped correlation id — for the same reason ``pdp.decide`` does. This engine
is step (1) of ``GovernedToolExecutor.execute``, immediately BEFORE the PDP, so a
recipient-allowlist deny (``NE_GOVERN_GUARDRAILS__RECIPIENTS__ALLOWED_RECIPIENTS``
is deny-by-default) is frequently the event that explains why an action stopped.
An un-traced block is invisible on the trace it is supposed to explain. The
carrier is :mod:`ne.events.correlation`, NOT :mod:`ne.runtime.correlation`:
``ne-runtime`` depends on ``ne-govern``, so reading the id from the runtime layer
would be a circular package dependency — do not "simplify" this import upward.
ABSENCE STAYS ABSENT: a block raised outside any run scope stamps ``None``, and
no path here mints or defaults an id, because a fabricated id would write a false
association into the append-only, hash-chained audit record.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from ne.contracts.governance import AuditSink, Guardrail, GuardrailVerdict
from ne.events import new_audit_event
from ne.events.correlation import current_run_trace_id
from ne.govern.errors import (
    AuditEmissionError,
    GuardrailEvaluationError,
    GuardrailViolation,
)
from ne.govern.guardrails._common import failed
from ne.govern.guardrails.rate import RateLimitGuardrail, SlidingWindowCounter
from ne.govern.guardrails.spend import SpendCapGuardrail, SpendLedger
from ne.schema import AuditOutcome


class GuardrailEngine:
    """Runs deterministic guardrails, audits every block, records on all-pass."""

    def __init__(
        self,
        guardrails: Sequence[Guardrail],
        sink: AuditSink,
        *,
        rate_counter: SlidingWindowCounter | None = None,
        spend_ledger: SpendLedger | None = None,
    ) -> None:
        self._guardrails = tuple(guardrails)
        self._sink = sink
        self._rate_counter = rate_counter
        self._spend_ledger = spend_ledger

    async def enforce(self, payload: Mapping[str, Any]) -> tuple[GuardrailVerdict, ...]:
        """Run every guardrail; block+audit on failure, record+return on all-pass."""
        verdicts: list[GuardrailVerdict] = []
        for guardrail in self._guardrails:
            try:
                verdict = guardrail.check(payload)
            except Exception as exc:
                await self._append_error(payload, exc)
                raise GuardrailEvaluationError(
                    f"guardrail {type(guardrail).__name__} raised; call denied (C12 fail closed)"
                ) from exc
            verdicts.append(verdict)

        failures = tuple(v for v in verdicts if not v.passed)
        if failures:
            await self._append_block(payload, failures)
            raise GuardrailViolation(failures)

        await self._reserve_on_pass(payload)
        return tuple(verdicts)

    async def _reserve_on_pass(self, payload: Mapping[str, Any]) -> None:
        # The pass-path record is now the ATOMIC AUTHORITATIVE reservation (NE-070H):
        # the rate counter and spend ledger enforce the cap by the conditional
        # write ITSELF, closing the check-then-record TOCTOU. A reservation loss is
        # a NEW deny point AFTER all checks passed — it can only fire when a
        # concurrent worker consumed the last of the cap between check() and here.
        # Every loss is an AUDITED guardrail.block + GuardrailViolation (C12
        # strengthened, never weakened).
        #
        # RATE is reserved BEFORE SPEND deliberately: if the spend charge then
        # loses, the worst residue is a PHANTOM rate event (fail-safe over-count),
        # never phantom spend. A spend-loss can never leave a charge behind because
        # SpendLedger.charge is all-or-nothing.
        # A reservation BACKEND failure (transient Postgres/SQLAlchemy error from
        # record_if_within/charge) must fail closed WITH a guardrail.error audit,
        # exactly like a check-time guardrail exception (Codex NE-070H P2): this
        # pass-path reservation runs AFTER enforce()'s check-loop try/except, so
        # without this wrapper a raw backend exception would escape unaudited.
        try:
            for guardrail in self._guardrails:
                if self._rate_counter is not None and isinstance(guardrail, RateLimitGuardrail):
                    reservation = guardrail.reservation(payload)
                    if reservation is not None:
                        key, window_s, max_calls = reservation
                        if not self._rate_counter.record_if_within(key, window_s, max_calls):
                            failures = (
                                failed(
                                    "rate.limit-exceeded",
                                    "per-principal/tool rate limit reached (concurrent)",
                                ),
                            )
                            await self._append_block(payload, failures)
                            raise GuardrailViolation(failures)
            for guardrail in self._guardrails:
                if self._spend_ledger is not None and isinstance(guardrail, SpendCapGuardrail):
                    plan = guardrail.charge_plan(payload)
                    if plan and not self._spend_ledger.charge(plan):
                        failures = (
                            failed(
                                "spend.cap-exceeded",
                                "cap exceeded (concurrent reservation)",
                            ),
                        )
                        await self._append_block(payload, failures)
                        raise GuardrailViolation(failures)
        except (GuardrailViolation, AuditEmissionError):
            # Intended, already-audited cap-loss deny (via _append_block), or an
            # audit-sink failure that is itself fail-closed — propagate as-is,
            # never re-audit as a guardrail.error.
            raise
        except Exception as exc:
            await self._append_error(payload, exc)
            raise GuardrailEvaluationError(
                "reservation backend failed; call denied (C12 fail closed)"
            ) from exc

    def _actor_for(self, payload: Mapping[str, Any]) -> str:
        principal = payload.get("principal")
        actor = getattr(principal, "id", None)
        return actor if isinstance(actor, str) and actor else "unknown"

    def _resource_for(self, payload: Mapping[str, Any]) -> str:
        tool = payload.get("tool")
        return tool if isinstance(tool, str) and tool else "unknown"

    async def _append_block(
        self, payload: Mapping[str, Any], failures: tuple[GuardrailVerdict, ...]
    ) -> None:
        # Event construction OUTSIDE the try (ConfigHashNotFrozenError must
        # propagate). detail carries ONLY rule names + violation-class details —
        # never raw arg/recipient/context values.
        event = new_audit_event(
            action="guardrail.block",
            resource=self._resource_for(payload),
            outcome=AuditOutcome.denied,
            actor=self._actor_for(payload),
            detail=json.dumps(
                {
                    "rules": [v.rule for v in failures],
                    "details": [v.detail for v in failures],
                }
            ),
            trace_id=current_run_trace_id(),
            provenance={"source": "ne-govern", "component": "guardrails"},
        )
        try:
            await self._sink.append(event)
        except Exception as exc:
            raise AuditEmissionError(
                "audit append failed for guardrail.block; call denied (C12 fail closed)"
            ) from exc

    async def _append_error(self, payload: Mapping[str, Any], exc: Exception) -> None:
        event = new_audit_event(
            action="guardrail.error",
            resource=self._resource_for(payload),
            outcome=AuditOutcome.error,
            actor=self._actor_for(payload),
            detail=json.dumps({"error_class": type(exc).__name__}),
            trace_id=current_run_trace_id(),
            provenance={"source": "ne-govern", "component": "guardrails"},
        )
        try:
            await self._sink.append(event)
        except Exception as append_exc:
            raise AuditEmissionError(
                "audit append failed for guardrail.error; call denied (C12 fail closed)"
            ) from append_exc
