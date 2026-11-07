# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The inline PDP-lane transit channel — REAL C6 decisions observed off the audit sink.

NE-137 Part A. The chat turn's SSE stream gains an ADDITIVE ``{type:'tool'}`` frame
per governed tool call as it transits the C6 ``GovernedToolExecutor``:
allow / deny / pending plus approval-terminal states, with the REAL ``audit_seq``
read back from the hash chain.

The transits are NOT fabricated: they are DERIVED from the real audit events the
real PDP (``pdp.decide``), the real ``DurableApprovalGate`` (``approval.requested``),
and the real executor (``tool.approval`` / ``tool.execute``) already emit. A single
:class:`ToolTransitObservingSink` wraps the ONE composition-root audit sink and
OBSERVES each ``tool:`` event AFTER delegating ``append`` to the inner sink — so it
can never suppress, reorder, or fail-open a governance write (C6 fail-closed is
untouched). Observation runs ONLY when a chat turn set :data:`TURN_TOOL_TRANSITS`;
every non-chat route + ne-worker sees byte-identical pass-through behavior.

Design §0 law 5 / "never lie": ``audit_seq`` is the REAL chain position looked up by
``event_id`` (degrades to ``None`` — rendered as '—' — when unresolvable, never
invented); the pending ``approval_id`` is the REAL ``DurableApprovalGate`` request_id
(actionable at ``/approvals``). When a terminal approval status or approved
execution carries that same id in its OWN audit provenance, the transit carries it
too; it is never recovered from a prior event or a per-tool cache. A generic deny
transit fires ONLY on a real PDP deny or a real pre-gate validation refusal
(``tool.validate[denied]``); approval terminal truth uses distinct decisions.
Enqueue mode binds the SAME channel inside ne-worker's turn step
(NE-TURN-PROGRESS, ``ne.worker.workflows``) and streams each transit as a ``tool``
``turn_wire.TurnFrame`` while the turn runs — both chat modes carry the PDP lane.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from contextvars import ContextVar
from typing import Any, Literal

import anyio.to_thread
import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict

from ne.audit.store import ne_audit_log
from ne.contracts.events import AuditEventLike
from ne.contracts.governance import AuditSink, AuditVerification
from ne.schema import AuditOutcome

ToolDecision = Literal[
    "allow",
    "deny",
    "pending",
    "rejected",
    "expired",
    "escalated",
    "integrity-failed",
    "approval-failed",
    "failed",
]


class ToolTransit(BaseModel):
    """One governed tool-call transit for the inline PDP lane (frozen, forbid).

    Rendered to the browser via :meth:`sse_payload` as a ``{type:'tool'}`` SSE frame.
    Every field is derived from a REAL audit event; ``tier``/``audit_seq`` render
    honestly as ``None`` (the console shows '—') rather than inventing a value.

    ``reason`` is the policy bundle's OWN sentence about the decision, carried
    verbatim off the audit ``detail`` the PDP already wrote. It is populated ONLY
    where the audit event genuinely carries one — today that is the ``pdp.decide``
    deny alone (see :meth:`ToolTransitObservingSink._observe`) — and is ``None``
    everywhere else, which the console renders as its existing "the server sent no
    reason". Nothing here composes, rewords or substitutes a sentence.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    tier: int | None
    decision: ToolDecision
    approval_id: str | None = None
    audit_seq: int | None
    reason: str | None = None

    def sse_payload(self) -> dict[str, Any]:
        """The additive ``tool`` SSE payload — ``approval_id``/``reason`` ONLY when set.

        Both optional keys are OMITTED rather than serialized as ``null`` (the
        ``verified``/``grounded``/``trace_id`` discipline), so every transit that
        carries neither renders BYTE-IDENTICALLY to its pre-existing bytes. The
        console reads an omitted key and an explicit ``null`` identically, so the
        absent-value render is unchanged either way.
        """
        payload: dict[str, Any] = {
            "type": "tool",
            "name": self.name,
            "tier": self.tier,
            "decision": self.decision,
            "audit_seq": self.audit_seq,
        }
        if self.approval_id is not None:
            payload["approval_id"] = self.approval_id
        if self.reason is not None:
            payload["reason"] = self.reason
        return payload


class TurnTransitChannel:
    """The per-turn transit queue + the turn's remembered per-tool PDP tier.

    ``publish`` is non-blocking (``put_nowait``): the observing sink is on the hot
    governance path and must never await on a full/awaiting consumer. The SSE loop
    drains the queue. ``pdp_tier_by_tool`` remembers the tier a ``pdp.decide`` event
    carried so the later ``tool.execute[success]`` allow card can render it (the
    execute event carries no tier in its audit detail — risk #4).
    """

    def __init__(self) -> None:
        self.queue: asyncio.Queue[ToolTransit] = asyncio.Queue()
        self.pdp_tier_by_tool: dict[str, int | None] = {}

    def publish(self, transit: ToolTransit) -> None:
        self.queue.put_nowait(transit)


#: The per-turn transit channel — set ONLY by the chat route (mirrors TURN_PRINCIPAL
#: in ne.api.tools). Unset ⇒ the observing sink is a pure pass-through.
TURN_TOOL_TRANSITS: ContextVar[TurnTransitChannel | None] = ContextVar(
    "ne_api_turn_tool_transits", default=None
)


def _seq_for_event(engine: sa.Engine, event_id: str) -> int | None:
    """Read the REAL chain ``seq`` for ``event_id`` on a short-lived connection.

    The ``routers/audit.py`` ``anyio.to_thread`` precedent: a single indexed select
    against the SAME audit database the sink just wrote to (the row is committed by
    the time the observer runs, because ``append`` delegated to the inner sink first).
    Returns ``None`` when the row is not yet visible / not found — the transit renders
    an honest '—' rather than an invented seq.
    """
    with engine.connect() as conn:
        row = conn.execute(
            sa.select(ne_audit_log.c.seq).where(ne_audit_log.c.event_id == event_id)
        ).first()
    return int(row.seq) if row is not None else None


class ToolTransitObservingSink:
    """Wrap the ONE audit sink; OBSERVE ``tool:`` events into the turn's transit channel.

    ``append`` delegates to the inner sink FIRST (inner errors propagate UNTOUCHED —
    governance fail-closed is unchanged), THEN, only when a chat turn set the channel,
    maps the just-appended event to a :class:`ToolTransit` and publishes it. Every
    observation failure is CONTAINED: a seq-lookup failure degrades ``audit_seq`` to
    ``None``; any mapping failure skips the publish. Neither ever raises into ``append``
    (a raise here would fail-close a legitimate tool call — the exact hazard this class
    must not introduce). ``verify`` delegates unchanged.
    """

    def __init__(self, inner: AuditSink, *, audit_engine: sa.Engine) -> None:
        self._inner = inner
        self._engine = audit_engine

    async def append(self, event: AuditEventLike) -> None:
        # Governance write FIRST — fail-closed semantics are the inner sink's, untouched.
        await self._inner.append(event)
        channel = TURN_TOOL_TRANSITS.get()
        if channel is None:
            return
        resource = getattr(event, "resource", None)
        if not isinstance(resource, str) or not resource.startswith("tool:"):
            return
        try:
            transit = await self._observe(event, resource, channel)
        except Exception:
            # A mapping failure NEVER raises into the governance append path.
            return
        if transit is not None:
            channel.publish(transit)

    async def verify(
        self, *, start: Any | None = None, end: Any | None = None
    ) -> AuditVerification:
        return await self._inner.verify(start=start, end=end)

    async def _observe(
        self, event: AuditEventLike, resource: str, channel: TurnTransitChannel
    ) -> ToolTransit | None:
        """Map a real ``tool:`` audit event to a lane transit (or ``None`` — skip).

        Mirrors the exact events the governed spine emits (never fabricates):
          * ``pdp.decide``          → remember the tier; publish a DENY iff outcome
            is ``denied`` (a real PDP deny; ``tool.unknown``'s own pdp.decide-denied
            publishes the SCRAM — we deliberately do NOT also map ``tool.unknown``, to
            avoid a double card; ``pdp.decide`` outcome=error is a backend failure, not
            a decision, so it publishes nothing). This is the ONE event whose audit
            ``detail`` carries the policy bundle's own ``reason``, so it is the ONE
            transit that serves one;
          * ``approval.requested``  → PENDING with the REAL gate request_id;
          * ``tool.approval[denied]`` → the exact server-owned approval terminal
            status (with the exact request id from this event's provenance);
          * ``tool.validate[denied]`` → DENY (a real pre-gate typed-input refusal; tier
            from the remembered pdp.decide);
          * ``tool.execute[success]`` → ALLOW (tier from the remembered pdp.decide,
            with the exact consumed approval id from this event's provenance when
            present; ungated execution therefore still carries no id).
          * ``tool.execute[error]`` → FAILED only when this exact event carries a
            consumed approval id; an ungated error is not an approval transition.

        NO OTHER TRANSIT SERVES A ``reason``, because no other event carries one.
        ``approval.requested`` writes ``{tier, action}``; ``tool.approval`` writes
        ``{status, tier}``; ``tool.validate[denied]`` writes ``{error_class}`` — a
        Python exception CLASS name, deliberately the only thing C12 lets that
        refusal record, and an operator triage token rather than a policy sentence.
        Reading any of those into the gate card would be this module writing prose
        about a governance decision, which is the one thing it must never do.
        """
        action = getattr(event, "action", None)
        # ``AuditEvent.outcome`` may be the ``AuditOutcome`` enum OR its stored string
        # value (pydantic ``use_enum_values``); ``==`` on the str-backed enum matches
        # BOTH forms, so the decision derivation is robust to either representation.
        outcome = getattr(event, "outcome", None)
        name = resource[len("tool:") :]

        if action == "pdp.decide":
            tier = self._tier_from_detail(event)
            channel.pdp_tier_by_tool[name] = tier
            if outcome == AuditOutcome.denied:
                return ToolTransit(
                    name=name,
                    tier=tier,
                    decision="deny",
                    audit_seq=await self._seq(event),
                    reason=self._reason_from_detail(event),
                )
            return None

        if action == "approval.requested":
            approval_id = self._approval_id(event)
            return ToolTransit(
                name=name,
                tier=self._tier_from_detail(event),
                decision="pending",
                approval_id=approval_id,
                audit_seq=await self._seq(event),
            )

        if action == "tool.approval" and outcome == AuditOutcome.denied:
            return ToolTransit(
                name=name,
                tier=self._tier_from_detail(event),
                decision=self._approval_terminal_decision(event),
                approval_id=self._approval_id(event),
                audit_seq=await self._seq(event),
            )

        if action == "tool.validate" and outcome == AuditOutcome.denied:
            # Pre-gate typed-input refusal (NE-ACTION-VALIDATE-BEFORE-GATE). The event's
            # detail carries only error_class (C12) — tier comes from the SAME turn's
            # remembered pdp.decide (validation runs strictly after the PDP, step 2b).
            # No `reason`: error_class is an internal exception class name, not the
            # policy's sentence, so this card keeps the honest absent note.
            return ToolTransit(
                name=name,
                tier=channel.pdp_tier_by_tool.get(name),
                decision="deny",
                audit_seq=await self._seq(event),
            )

        if action == "tool.execute" and outcome == AuditOutcome.success:
            return ToolTransit(
                name=name,
                tier=channel.pdp_tier_by_tool.get(name),
                decision="allow",
                approval_id=self._approval_id(event),
                audit_seq=await self._seq(event),
            )

        if action == "tool.execute" and outcome == AuditOutcome.error:
            approval_id = self._approval_id(event)
            if approval_id is not None:
                return ToolTransit(
                    name=name,
                    tier=channel.pdp_tier_by_tool.get(name),
                    decision="failed",
                    approval_id=approval_id,
                    audit_seq=await self._seq(event),
                )

        return None

    async def _seq(self, event: AuditEventLike) -> int | None:
        """The REAL audit seq, or ``None`` (honest '—') if the lookup fails."""
        event_id = getattr(event, "event_id", None)
        if not isinstance(event_id, str) or not event_id:
            return None
        try:
            return await anyio.to_thread.run_sync(_seq_for_event, self._engine, event_id)
        except Exception:
            # A seq-lookup failure degrades to None — never fabricated, never raised.
            return None

    @staticmethod
    def _tier_from_detail(event: AuditEventLike) -> int | None:
        """Defensively parse the ``tier`` out of the event's JSON ``detail`` string."""
        detail = getattr(event, "detail", None)
        if not isinstance(detail, str) or not detail:
            return None
        with contextlib.suppress(Exception):
            parsed = json.loads(detail)
            if isinstance(parsed, dict):
                tier = parsed.get("tier")
                if isinstance(tier, int):
                    return tier
        return None

    @staticmethod
    def _reason_from_detail(event: AuditEventLike) -> str | None:
        """Defensively parse the policy ``reason`` out of the event's JSON ``detail``.

        Mirrors :meth:`_tier_from_detail` — absent, non-string, non-JSON and
        non-object ``detail`` all degrade to ``None`` rather than raising, and a
        ``reason`` that is not a NON-EMPTY string degrades the same way. (``opa.py``
        defaults a missing OPA ``reason`` to ``""``; an empty string is not a
        sentence, so it renders the console's existing absent note rather than an
        empty gate body.)

        C12 — THE ONE VALUE THIS MUST NOT SERVE. ``pdp.py`` writes ``str(exc)`` into
        this SAME ``detail["reason"]`` key when the policy BACKEND fails: that string
        interpolates the OPA decision path, the sidecar URL, up to 200 bytes of raw
        OPA response body, and (via the Cedar evaluator) internal rule ids and the
        live principal id. Every such event is stamped ``effect: "error"`` at the one
        site that builds it (``GovernedPdp._append_audit`` with ``effect="error"``),
        so this refuses that marker OUTRIGHT rather than relying on the caller's
        ``outcome == denied`` guard to be the only thing standing between an operator
        diagnostic and an end user's screen. Both guards are load-bearing and both
        are tested.
        """
        detail = getattr(event, "detail", None)
        if not isinstance(detail, str) or not detail:
            return None
        with contextlib.suppress(Exception):
            parsed = json.loads(detail)
            if isinstance(parsed, dict):
                if parsed.get("effect") == "error":
                    return None
                reason = parsed.get("reason")
                if isinstance(reason, str) and reason:
                    return reason
        return None

    @staticmethod
    def _approval_id(event: AuditEventLike) -> str | None:
        """The REAL DurableApprovalGate request_id from ``provenance['approval_id']``."""
        provenance = getattr(event, "provenance", None)
        if not isinstance(provenance, dict):
            return None
        value = provenance.get("approval_id")
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _approval_terminal_decision(event: AuditEventLike) -> ToolDecision:
        """Project only the status carried by this ``tool.approval`` event.

        ``approved`` paired with an audit ``denied`` outcome is the runtime's
        never-auto-approve integrity refusal (an APPROVED outcome had no named
        approver). Unknown or malformed legacy detail degrades to the neutral
        ``approval-failed`` state; it is never presented as a human rejection.
        """
        detail = getattr(event, "detail", None)
        if isinstance(detail, str) and detail:
            with contextlib.suppress(Exception):
                parsed = json.loads(detail)
                if isinstance(parsed, dict):
                    status = parsed.get("status")
                    if status in {"rejected", "expired", "escalated"}:
                        return status
                    if status == "approved":
                        return "integrity-failed"
        return "approval-failed"
