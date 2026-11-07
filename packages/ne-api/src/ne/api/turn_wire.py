# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The cross-process turn wire — ``TurnRequest`` + ``TurnFrame`` (NE-103c §5.1/§4.3).

DECORATOR-FREE and importable by BOTH the ne-api process (enqueues) and the
ne-worker process (executes + emits). ``ne.api`` NEVER imports ``ne.worker``; this
module is the shared vocabulary they both speak through Postgres.

``TurnRequest`` is the serializable workflow argument (one JSON string, the
``approvals_durable.py`` house pattern). Its principal fields are ALWAYS derived
from the verified bearer via :meth:`TurnRequest.from_principal` — never from request
input (spec §4.4). The ``Principal`` (``id``/``groups``/``claims``) round-trips
losslessly through ``model_dump_json`` because the claims came out of a verified JWT
and are already JSON-clean.

``TurnFrame`` is the transport-independent frame the worker emits and ne-api's SOLE
``sse_frame`` encoder renders (spec §2.3 payloads verbatim + a ``seq`` envelope that
is stripped before SSE encoding, so browser bytes stay byte-identical).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ne.api.sse import safe_error_detail
from ne.contracts.identity import AuthenticatedRequestContext
from ne.contracts.runtime import GroundedProvenance, VerifiedProvenance
from ne.contracts.types import Lane, Principal

PROTECTED_TRANSPORT_GENERATION = "agent-transport-v1"
ProtectedContentStatusWire = Literal["source_free", "source_bound_container", "legacy_unavailable"]


class ProtectedTurnRef(BaseModel):
    """Content-free address of one exact protected Conversation-turn slot."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    protected_ref: str = Field(min_length=1)
    protected_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_status: ProtectedContentStatusWire


class ProtectedTerminalPayload(BaseModel):
    """Encrypted terminal body opened by API and never written to DBOS streams."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    output: str
    citations: tuple[dict[str, str], ...] = ()
    verified: VerifiedProvenance | None = None
    grounded: GroundedProvenance | None = None
    trace_id: str | None = None
    message_id: str | None = None


class LegacyTurnRequest(BaseModel):
    """Pre-cutover durable request retained for rollback-compatible enqueue.

    This is deliberately a separate carrier from :class:`TurnRequest`: plaintext
    is valid only while the protected transport generation is inactive and can
    therefore never be added accidentally to a protected request payload.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    turn_id: str
    session_id: str
    agent_name: str
    department: str
    prompt: str
    principal_id: str
    principal_groups: tuple[str, ...] = ()
    principal_claims: dict[str, Any] = {}
    trace_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    requested_model: str | None = None
    requested_model_lane: Lane | None = None
    request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    claimed_attempt_id: str = Field(min_length=1)

    @classmethod
    def from_principal(
        cls,
        *,
        turn_id: str,
        session_id: str,
        agent_name: str,
        department: str,
        prompt: str,
        principal: Principal,
        trace_id: str,
        request_fingerprint: str,
        idempotency_fingerprint: str,
        claimed_attempt_id: str,
        requested_model: str | None = None,
        requested_model_lane: Lane | None = None,
    ) -> LegacyTurnRequest:
        return cls(
            turn_id=turn_id,
            session_id=session_id,
            agent_name=agent_name,
            department=department,
            prompt=prompt,
            principal_id=principal.id,
            principal_groups=tuple(principal.groups),
            principal_claims=dict(principal.claims),
            trace_id=trace_id,
            requested_model=requested_model,
            requested_model_lane=requested_model_lane,
            request_fingerprint=request_fingerprint,
            idempotency_fingerprint=idempotency_fingerprint,
            claimed_attempt_id=claimed_attempt_id,
        )

    def to_principal(self) -> Principal:
        return Principal(
            id=self.principal_id,
            groups=tuple(self.principal_groups),
            claims=dict(self.principal_claims),
        )


class TurnRequest(BaseModel):
    """The serializable durable-workflow argument for a chat turn (frozen, forbid).

    ``principal_*`` fields are bearer-derived ONLY (:meth:`from_principal`); the
    console body carries just ``{prompt}`` (spec §1.2 #3), and ``extra='forbid'``
    rejects any smuggled field regardless.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    turn_id: str
    session_id: str
    agent_name: str
    department: str
    protected_input: ProtectedTurnRef
    transport_generation: Literal["agent-transport-v1"] = PROTECTED_TRANSPORT_GENERATION
    authenticated_context: AuthenticatedRequestContext
    principal_id: str
    principal_groups: tuple[str, ...] = ()
    principal_claims: dict[str, Any] = {}
    # Worker admission is attempt-bound and fail closed.  Every field below is
    # server-minted before enqueue; legacy/unbound payloads are not runnable.
    trace_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    requested_model: str | None = None
    requested_model_lane: Lane | None = None
    request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    claimed_attempt_id: str = Field(min_length=1)

    @classmethod
    def from_principal(
        cls,
        *,
        turn_id: str,
        session_id: str,
        agent_name: str,
        department: str,
        protected_input: ProtectedTurnRef,
        authenticated_context: AuthenticatedRequestContext,
        principal: Principal,
        trace_id: str,
        request_fingerprint: str,
        idempotency_fingerprint: str,
        claimed_attempt_id: str,
        requested_model: str | None = None,
        requested_model_lane: Lane | None = None,
    ) -> TurnRequest:
        """Build a ``TurnRequest`` whose identity fields come ONLY from ``principal``.

        The single sanctioned constructor for the enqueue path — the principal is
        the verified-bearer :class:`ne.contracts.types.Principal`, never request
        input (spec §4.4). Prevents an accidental caller from populating the
        principal fields from anything on the wire.
        """
        return cls(
            turn_id=turn_id,
            session_id=session_id,
            agent_name=agent_name,
            department=department,
            protected_input=protected_input,
            authenticated_context=authenticated_context,
            principal_id=principal.id,
            principal_groups=tuple(principal.groups),
            principal_claims=dict(principal.claims),
            trace_id=trace_id,
            requested_model=requested_model,
            requested_model_lane=requested_model_lane,
            request_fingerprint=request_fingerprint,
            idempotency_fingerprint=idempotency_fingerprint,
            claimed_attempt_id=claimed_attempt_id,
        )

    @model_validator(mode="after")
    def _bind_verified_identity(self) -> TurnRequest:
        """Refuse a delayed request whose verifier receipt names another caller."""
        if self.authenticated_context.principal_id != self.principal_id:
            raise ValueError("authenticated context belongs to a different principal")
        if (
            self.authenticated_context.department is not None
            and self.authenticated_context.department != self.department
        ):
            raise ValueError("authenticated context belongs to a different department")
        return self

    def to_principal(self) -> Principal:
        """Rebuild the :class:`Principal` inside the worker (the fail-closed re-check
        of the department claim rides along in ``DepartmentAgentSession.run``)."""
        return Principal(
            id=self.principal_id,
            groups=tuple(self.principal_groups),
            claims=dict(self.principal_claims),
        )


FrameType = Literal["token", "citations", "tool", "done", "error"]


class TurnFrame(BaseModel):
    """One transport-envelope frame (spec §2.3 payload + a dedupe ``seq``).

    ``seq`` is stripped before SSE encoding (browser payloads stay exactly spec
    §2.3). ``done``/``error`` are terminal and mutually exclusive; a reader stops at
    either or at stream-close.
    """

    model_config = ConfigDict(extra="forbid")

    seq: int
    type: FrameType
    delta: str | None = None
    citations: list[dict[str, Any]] | None = None
    session_id: str | None = None
    output: str | None = None
    # PRE-009C protected enqueue variant. The worker stores the complete terminal
    # frame bundle in the exact Conversation terminal slot and sends only this
    # content-free pointer through DBOS. API must materialize it before SSE.
    protected_terminal: ProtectedTurnRef | None = None
    detail: str | None = None
    # NE-137 (additive, enqueue-ready): the ``tool`` frame fields. All default None,
    # so a token/citations/done/error frame serializes BYTE-IDENTICALLY to pre-NE-137.
    name: str | None = None
    tier: int | None = None
    decision: str | None = None
    approval_id: str | None = None
    audit_seq: int | None = None
    # The policy bundle's own sentence about the decision, carried verbatim off the
    # audit detail by ToolTransit. Only the PDP deny transit has one; None (key
    # omitted) everywhere else, so every pre-existing tool frame is byte-identical.
    reason: str | None = None
    # NE-VAF-U0 (additive, router-ready): structural verified-answer provenance
    # on the ``done`` frame. Default None so every existing frame serializes
    # byte-identically (exclude-none) to pre-U0; populated by U3/U4, surfaced
    # + asserted by U5.
    verified: VerifiedProvenance | None = None
    # NE-GA-G0 (additive, gate-ready): structural grounded-answer provenance
    # on the ``done`` frame. Default None so every existing frame serializes
    # byte-identically (exclude-none); populated by G3/G5.
    grounded: GroundedProvenance | None = None
    # BD-15: the server-owned run trace joins the done frame to governed model
    # selection/call evidence. Optional so legacy frames remain unchanged.
    trace_id: str | None = None
    message_id: str | None = None

    @model_validator(mode="after")
    def _terminal_shape(self) -> TurnFrame:
        if self.type == "done" and (self.output is None) == (self.protected_terminal is None):
            raise ValueError("done frame requires exactly one plaintext or protected terminal")
        if self.type != "done" and self.protected_terminal is not None:
            raise ValueError("only a done frame may carry a protected terminal")
        return self

    def sse_payload(self) -> dict[str, Any]:
        """The spec §2.3 payload rendered to the browser — ``seq`` stripped, only
        the keys the frame type carries (byte-identity with inline-mode frames)."""
        payload: dict[str, Any] = {"type": self.type}
        if self.type == "token":
            payload["delta"] = self.delta
        elif self.type == "citations":
            payload["citations"] = self.citations or []
        elif self.type == "tool":
            # Same key set as ToolTransit.sse_payload. approval_id is the exact
            # request id from THIS transit's audit provenance (pending request,
            # approved/failed execution, or approval terminal status) and is
            # omitted when the current event supplied none (ungated allow/error
            # or PDP/validation deny).
            payload["name"] = self.name
            payload["tier"] = self.tier
            payload["decision"] = self.decision
            payload["audit_seq"] = self.audit_seq
            if self.approval_id is not None:
                payload["approval_id"] = self.approval_id
            if self.reason is not None:
                payload["reason"] = self.reason
        elif self.type == "done":
            if self.protected_terminal is not None:
                raise ValueError("protected terminal must be materialized before SSE encoding")
            payload["session_id"] = self.session_id
            payload["output"] = self.output
            if self.message_id is not None:
                payload["message_id"] = self.message_id
            # NE-VAF-U4: surface structural verified-answer provenance on the
            # relay path too (enqueue==inline parity) — omitted when None so a
            # pre-U4 done frame stays byte-identical.
            if self.verified is not None:
                payload["verified"] = self.verified.model_dump(mode="json")
            # NE-GA-G0: relay parity for structural grounded-answer
            # provenance — omitted when None so every pre-G0 done frame
            # stays byte-identical.
            if self.grounded is not None:
                payload["grounded"] = self.grounded.model_dump(mode="json")
            # Kept after structural provenance to match turn_event_stream's
            # insertion order exactly (inline/relay SSE byte parity).
            if self.trace_id is not None:
                payload["trace_id"] = self.trace_id
        elif self.type == "error":
            payload["detail"] = self.detail
        return payload


def token_frame(seq: int, delta: str) -> TurnFrame:
    """A ``token`` frame carrying an output delta (v1: one per turn, the full text)."""
    return TurnFrame(seq=seq, type="token", delta=delta)


def citations_frame(seq: int, citations: list[dict[str, str]]) -> TurnFrame:
    """One server-captured citation set served by governed knowledge search."""
    return TurnFrame(seq=seq, type="citations", citations=citations)


def tool_frame(
    seq: int,
    *,
    name: str,
    tier: int | None,
    decision: str,
    approval_id: str | None = None,
    audit_seq: int | None = None,
    reason: str | None = None,
) -> TurnFrame:
    """A ``tool`` frame carrying a REAL C6 governed-decision transit (NE-137).

    Enqueue-ready: ne-worker can emit these once it sets the transit channel (a
    follow-up; the type is wire-ready here). Renders BYTE-IDENTICALLY to the inline
    ``ToolTransit.sse_payload`` (same key set; ``approval_id`` and ``reason``
    omitted when this exact transit supplied none). Server-owned approval
    terminal/failure decision values pass through unchanged."""
    return TurnFrame(
        seq=seq,
        type="tool",
        name=name,
        tier=tier,
        decision=decision,
        approval_id=approval_id,
        audit_seq=audit_seq,
        reason=reason,
    )


def done_frame(
    seq: int,
    session_id: str,
    output: str | None = None,
    *,
    protected_terminal: ProtectedTurnRef | None = None,
    verified: VerifiedProvenance | None = None,
    grounded: GroundedProvenance | None = None,
    trace_id: str | None = None,
    message_id: str | None = None,
) -> TurnFrame:
    """The terminal ``done`` frame — ``output`` is the authoritative full text.

    ``verified`` (NE-VAF-U0, keyword-only) optionally carries structural
    verified-answer provenance; default None keeps every pre-U0 call site
    (positional) untouched and byte-identical on the wire.

    ``grounded`` (NE-GA-G0, keyword-only) optionally carries structural
    grounded-answer provenance; default None keeps every existing call site
    untouched and byte-identical on the wire."""
    return TurnFrame(
        seq=seq,
        type="done",
        session_id=session_id,
        output=output,
        protected_terminal=protected_terminal,
        verified=verified,
        grounded=grounded,
        trace_id=trace_id,
        message_id=message_id,
    )


class TurnStepResult(BaseModel):
    """The serialized ``chat.turn.execute`` step result (NE-VAF-U4).

    Authoritative output text + structural verified provenance so enqueue-mode
    turns carry provenance identically to inline (design §Layer-0 'Enqueue
    parity'). Honest caveat: a durable workflow recovered ACROSS this upgrade
    would hold a pre-U4 bare-string step output and fail ``model_validate_json``
    here — acceptable pre-GA, since durability defaults 'off' and the failure
    is loud (a raised ``ValidationError``), never silent.
    """

    model_config = ConfigDict(extra="forbid")

    output: str
    verified: VerifiedProvenance | None = None
    grounded: GroundedProvenance | None = None
    # Binding-captured, ACL-trimmed citations. Default-empty preserves old
    # serialized step results during rolling upgrades.
    citations: list[dict[str, str]] = Field(default_factory=list)
    # Private execution-attempt nonce. It is never emitted to the browser; the
    # durable workflow uses it only to publish/compensate the exact prepared row.
    attempt_id: str | None = None
    #: NE-TURN-PROGRESS: how many ``tool`` frames the step wrote onto the turn's
    #: frame stream (seqs ``0..frames_emitted-1``) while the turn ran, so the
    #: workflow numbers the terminal ``token``/``done`` pair AFTER them and the
    #: relay's monotonic-seq dedup never drops a terminal. Defaults 0 (a recovered
    #: pre-upgrade step result validates and renumbers to the old 0/1 shape).
    frames_emitted: int = 0


def error_frame(seq: int, exc: BaseException) -> TurnFrame:
    """A terminal ``error`` frame with a SAFE detail (spec §2.4 step 4 discipline)."""
    return TurnFrame(seq=seq, type="error", detail=safe_error_detail(exc))
