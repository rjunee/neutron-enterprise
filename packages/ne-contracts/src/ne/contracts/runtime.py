# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Runtime domain Protocols (SPEC section 4).

Agent sessions, tool/sub-agent orchestration, scheduling, memory, and the chat
channel surface.

Durability decision (SPEC section 4): these Protocols are durability-engine
agnostic. DBOS Transact is the durable-execution engine, wrapped by the
``@ne_workflow`` / ``@ne_step`` decorators that ship in ``ne-runtime`` (NE-033) —
NOT here. A future Temporal move is an engagement-time port, not a plugin slot;
there is deliberately no durability slot in the plugin registry.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from ne.contracts.frozen import AnyMap, FrozenDict
from ne.contracts.governance import ApprovalRequest
from ne.contracts.types import ApprovalTier, Citation, Lane, Principal


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class ToolSpec(_Frozen):
    """Declaration of a callable tool and its approval tier."""

    name: str
    description: str
    tier: ApprovalTier
    input_schema: AnyMap = FrozenDict()


class SubAgentSpec(_Frozen):
    """A request to dispatch a bounded sub-agent."""

    agent: str
    task: str
    max_depth: int = 1
    max_concurrency: int = 1
    context: AnyMap = FrozenDict()
    route_lane_hint: Lane | None = None
    """NE-161, added 1.5.0, ADR-0008. MODEL-EMITTED ⇒ UNTRUSTED: this is a
    request FROM the (possibly hijacked) planner, never a routing authority.
    NARROW-ONLY toward :attr:`~ne.contracts.types.Lane.PRIVATE_VLLM` — the
    dispatcher clamps any attempt to widen toward ``FRONTIER_API`` back to the
    trusted, server-registered ``AgentDefinition`` binding (design doc §10.2).
    Default ``None`` keeps every pre-1.5.0 ``SubAgentSpec`` constructor valid."""


class SubAgentResult(_Frozen):
    """Outcome of a sub-agent dispatch."""

    agent: str
    output: str
    succeeded: bool


class ScheduledJob(_Frozen):
    """A cron-scheduled agent run.

    ``job_id`` is assigned by the :class:`Scheduler` (the id returned by
    ``schedule()``); it is ``None`` before registration.
    """

    name: str
    cron: str
    agent: str
    toolset: tuple[str, ...] = ()
    job_id: str | None = None


class MemoryEvent(_Frozen):
    """A single episodic/semantic memory entry."""

    kind: str
    content: str
    occurred_at: datetime
    provenance: str = ""


class VerifiedProvenance(_Frozen):
    """Structural verified-answer provenance (design docs/design/
    ne-verified-answer-first-chat.md §Layer-0).

    Constructed ONLY by the deterministic ``VerifiedRouter`` (U3) from the
    parsed ``VerifiedAnswer`` artifact — the model's free text has no channel
    into it. This is what closes the H1 forgeability flaw: a text-substring
    check over the model's own output (e.g. ``"template_id" in output``) is
    forgeable (the model can emit the literal string, or copy a real
    ``VerifiedAnswer`` JSON block that was replayed to it from history); a
    structural, code-constructed provenance object is not.

    Added 1.6.0, ADR-0009. ``extra='forbid'`` is deliberate hardening for a
    provenance carrier: it is all-or-nothing (no defaulted fields) because
    optionality lives on the carrier field (``AgentRunResult.verified`` /
    ``TurnFrame.verified``), not on the provenance itself.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    template_id: str
    template_version: int
    config_hash: str
    row_count: int
    truncated: bool


class GroundedProvenance(_Frozen):
    """Structural grounded-answer provenance (design docs/design/
    ne-grounded-agent.md §6 G0).

    Constructed ONLY by the G3 runtime post-step from the deterministic
    grounding gate — the model's free text has no channel into it, exactly
    the same forgeability discipline ``VerifiedProvenance`` (ADR-0009)
    establishes for the verified-answer-first path.

    Field meanings:

    - ``fetches`` — the per-verified-fetch provenance objects recorded this
      turn (the same ``VerifiedProvenance`` shape defined above).
    - ``citation_count`` — the number of citations present in the final
      answer.
    - ``gate`` — the gate version string (G1 ``GATE_VERSION``).
    - ``fallback`` — the model's first draft was rejected by the
      deterministic gate. Combined with ``assist_used`` this distinguishes
      the deterministic verified-table fallback (``assist_used=False``)
      from an assisted grounded re-draft (``assist_used=True``).
    - ``assist_used`` — the one-pass model assist's output was ADOPTED into
      the emitted answer (it passed the gate re-check), not merely that the
      assist ran; a rejected or erroring assist leaves this ``False``.
    - ``prompt_echo_count`` — tokens grounded only by prompt echo.
    - ``strictness`` — the deployment's grounding-strictness tier the gate
      ran at this turn (``"strict"`` | ``"standard"`` in v1). Stamped ONLY
      from deployment config (``OntologySettings``) by the G3 runtime —
      never caller/request-selectable (a per-request tier would be a
      self-service assurance downgrade). Default ``"strict"`` is the
      pre-1.10 semantics: every pre-1.10 constructor and serialized payload
      deserializes to the tier the gate actually implemented then.

    The ``(fallback, assist_used)`` pair is the clean 4-way encoding:

    - ``(False, False)`` — clean pass: the original draft passed the gate
      untouched.
    - ``(False, True)`` — pass-polish adopted: the draft passed the gate and
      the assist's re-worded candidate ALSO passed the re-check and was
      emitted instead.
    - ``(True, True)`` — assisted grounded re-draft: the draft FAILED the
      gate, but the assist's re-drafted candidate passed the re-check and
      was emitted instead of the deterministic table.
    - ``(True, False)`` — deterministic verified-table fallback: the draft
      FAILED the gate and no assist candidate was adopted (assist disabled,
      it raised, or its candidate also failed the re-check), so the
      deterministic verified-table text was emitted.

    Added 1.7.0, ADR-0010. ``extra="forbid"`` is deliberate hardening for a
    provenance carrier: all-or-nothing, optionality lives on the carrier
    field (``AgentRunResult.grounded`` / ``TurnFrame.grounded``), not on the
    provenance itself.

    ``strictness`` added 1.10.0, ADR-0013 — additive-with-default so the
    frozen ``extra="forbid"`` model stays backward compatible; kept a
    separate field (not encoded into ``gate``) because ``gate`` is the
    rulebook identity while ``strictness`` is the deployment posture.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    fetches: tuple[VerifiedProvenance, ...]
    citation_count: int
    gate: str
    fallback: bool
    assist_used: bool
    prompt_echo_count: int
    strictness: str = "strict"


class AgentRunResult(_Frozen):
    """The result of a single agent run."""

    output: str
    session_id: str
    verified: VerifiedProvenance | None = None
    """NE-VAF-U0, added 1.6.0, ADR-0009. Structural verified-answer
    provenance — populated ONLY by the deterministic VerifiedRouter (U3),
    never from model text. Default None keeps every pre-1.6.0 constructor
    valid."""
    grounded: GroundedProvenance | None = None
    """NE-GA-G0, added 1.7.0, ADR-0010. Structural grounded-answer
    provenance — populated ONLY by the deterministic grounding post-step
    (G3), never from model text. Default None keeps every pre-1.7.0
    constructor valid."""


class InboundMessage(_Frozen):
    """A message arriving from a chat channel."""

    channel: str
    sender: str
    text: str


class OutboundMessage(_Frozen):
    """A message (with citations) to deliver to a chat channel."""

    channel: str
    text: str
    citations: tuple[Citation, ...] = ()


class AgentSession(Protocol):
    """A live agent session bound to a principal."""

    @property
    def session_id(self) -> str: ...

    @property
    def agent_name(self) -> str: ...

    async def run(
        self,
        prompt: str,
        *,
        principal: Principal,
        context: Mapping[str, Any] | None = None,
    ) -> AgentRunResult: ...

    async def close(self) -> None: ...


class ToolRegistry(Protocol):
    """In-memory registry of tools available to agents (sync)."""

    def register(self, tool: ToolSpec) -> None: ...

    def get(self, name: str) -> ToolSpec: ...

    def list_tools(self) -> Sequence[ToolSpec]: ...


class SubAgentDispatcher(Protocol):
    """Bounded fan-out to sub-agents."""

    async def dispatch(self, spec: SubAgentSpec) -> SubAgentResult: ...


class Scheduler(Protocol):
    """Cron scheduler for agent runs (sync registration).

    ``list_jobs()`` returns jobs with ``job_id`` set (the id returned by
    ``schedule()``).
    """

    def schedule(self, job: ScheduledJob) -> str: ...

    def cancel(self, job_id: str) -> None: ...

    def list_jobs(self) -> Sequence[ScheduledJob]: ...


class MemoryProvider(Protocol):
    """Append/recall over an agent's memory."""

    async def append(self, event: MemoryEvent) -> None: ...

    async def recall(
        self,
        query: str,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 20,
    ) -> Sequence[MemoryEvent]: ...


class ChatAdapter(Protocol):
    """Chat channel surface (SPEC section 4): messages in/out, approval prompts, digests."""

    async def send_message(self, message: OutboundMessage) -> None: ...

    async def send_approval_prompt(self, channel: str, request: ApprovalRequest) -> None: ...

    async def send_digest(self, channel: str, text: str) -> None: ...

    def on_message(self, handler: Callable[[InboundMessage], Awaitable[None]]) -> None: ...
