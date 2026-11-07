# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Models domain Protocols (SPEC section 4).

The single model seam is :class:`ModelGateway` (provider-opaque, C3 model
independence) plus an in-memory :class:`ModelRegistry`. No provider SDK is
imported here.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from ne.contracts.frozen import AnyMap, FrozenDict
from ne.contracts.tokens import ContextBudget
from ne.contracts.types import ChatMessage, Lane


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class Usage(_Frozen):
    """Token accounting for a completion."""

    prompt_tokens: int
    completion_tokens: int


class Completion(_Frozen):
    """A non-streaming model completion."""

    text: str
    model_id: str
    usage: Usage
    cost_usd: float | None = None
    finish_reason: str | None = None
    """Why the provider stopped generating, carried VERBATIM as the provider
    reported it (``"stop"``, ``"length"``, ``"tool_calls"``, ...), or ``None``
    when it reported nothing.

    NE-FRONTIER-OUTPUT: before this field the gateway read ``finish_reason``,
    used it for the empty-content :class:`ReasoningExhaustedError` case, and then
    DISCARDED it. A completion that was truncated but NOT empty
    (``finish_reason == "length"`` with real content) was therefore returned
    through a :class:`Completion` that was indistinguishable from a whole one,
    and no consumer — nor the success audit row — could tell. A cut-off answer
    presented as a complete one is a silent degrade, so the fact is now carried
    rather than dropped. It is a provider status token, never text: no prompt or
    response substring can ride it (C12).

    It is NOT normalized to an enum on purpose. Providers disagree on the
    spelling of everything except ``"length"``/``"stop"``, and inventing a
    Neutron enum here would make the contract a place where a provider fact gets
    quietly re-stated. :attr:`truncated` is the one derived reading the codebase
    makes, and it tests only for the value every provider spells the same way."""

    @property
    def truncated(self) -> bool:
        """``True`` when the provider stopped because it hit the output budget.

        The one question a caller has to be able to ask: is this the whole
        answer, or the part of it that fit? ``finish_reason == "length"`` is the
        OpenAI-compatible spelling that litellm normalizes every provider onto
        (both the vLLM private lanes and the frontier lanes on the pinned
        1.90.3). ``None`` — a provider that reported nothing — is NOT read as
        truncated: absence of evidence is not evidence of a cut, and guessing
        the other way would flag every mock/fixture completion in the suite."""
        return self.finish_reason == "length"


class CompletionChunk(_Frozen):
    """One streamed delta of a completion."""

    delta: str
    model_id: str


class CompletionRequest(_Frozen):
    """A per-task completion request with optional lane pinning."""

    messages: tuple[ChatMessage, ...]
    task: str
    lane: Lane | None = None
    params: AnyMap = FrozenDict()
    trace_id: str | None = None
    """Opaque run-scoped correlation id carried AS DATA into the gateway (NE-166,
    added 1.4.0, ADR-0007) so every audit/cost event produced during one agent run
    shares ONE non-null id — including under the DEFAULT no-op OTel tracer, where
    live-span derivation yields ``None``. Never message content, never PII: it is
    an opaque identifier only (typically a ``uuid4().hex``), minted at the run
    root and threaded through as ordinary data. Default ``None`` keeps every
    pre-1.4.0 constructor call valid."""
    principal_id: str | None = None
    """Opaque principal IDENTIFIER of the calling principal, carried AS DATA into
    the gateway (NE-COST-PRINCIPAL, added 1.15.0, ADR-0018) so ``CostEvent.actor``
    can be stamped with the principal that actually caused the call rather than
    the gateway's own service identity. Shape matches ``Principal.id`` / the
    audit-chain actor (e.g. ``"user:ada"``, ``"agent:demo-delegate"``) so the two
    are joinable. This introduces **no additional identifier class**: the value is
    the one the audit chain already stamps as ``actor``. It is NOT guaranteed to
    be PII-free — ``Principal.id`` is the raw OIDC subject, and for an IdP whose
    ``sub`` is an email address it is an email address (``CostEvent.actor``'s own
    schema example is ``"user:ada@acme.com"``). The C12 property this field holds
    is that it widens nothing, not that the identifier is anonymous.
    Default ``None`` keeps every
    pre-1.15.0 constructor call valid; ``None`` (or an empty string) means the
    emitting gateway falls back to its own service actor — honest attribution for
    a principal-less call (boot-time warmup, background sync)."""
    requested_model: str | None = None
    """Exact boot-registry model alias requested for this completion
    (BD-15, added 1.17.0, ADR-0020). ``None`` preserves the governed task route
    selected by the gateway; an explicit value may replace only that route's
    primary for this call and remains subject to registry, lane, egress, and
    fallback policy. This is the gateway alias (for example
    ``"frontier-anthropic"``), not a provider SDK model string or endpoint.
    Default ``None`` keeps every pre-1.17.0 constructor and payload valid."""


class ModelEntry(_Frozen):
    """A registered model, its provenance, and routing metadata."""

    model_id: str
    provider: str
    license: str
    origin: str
    lanes: tuple[Lane, ...]
    capabilities: tuple[str, ...] = ()
    context_window: int | None = None


class ModelGateway(Protocol):
    """Provider-opaque completion seam.

    ``stream`` is a *sync* method that RETURNS an ``AsyncIterator`` (the
    platform convention for streaming seams): callers ``async for`` over the
    returned iterator; the method itself is not a coroutine.
    """

    async def complete(self, request: CompletionRequest) -> Completion: ...

    def stream(self, request: CompletionRequest) -> AsyncIterator[CompletionChunk]: ...


class TurnModelBudget(_Frozen):
    """The resolution :class:`TurnContextBudget` answers with (NE-CTX-IN-FORCE).

    ``model`` NAMES the gateway alias whose ``budget`` this is — the FIRST
    candidate the resolving gateway will actually attempt for the turn. The name
    travels with the number so a reporting surface (the context ledger's
    "reported == enforced" contract) can state WHICH model the enforced window
    belongs to without re-deriving candidate order — re-derivation is exactly
    the defect class ADR-0029 round 4 closed (four call sites each carrying a
    private copy of the ordering rule, each drifting from the real call path).
    """

    model: str
    budget: ContextBudget


@runtime_checkable
class TurnContextBudget(Protocol):
    """A gateway that can name the context budget of the model IN FORCE for a turn.

    NE-CTX-IN-FORCE (ADR-0029). :class:`ModelGateway` is deliberately
    provider-opaque and stays frozen; this is a SEPARATE, OPTIONAL capability a
    concrete gateway may also satisfy. The Layer-2 trim guard
    (``ne.runtime.model.GatewayModel``) tests for it with ``isinstance`` and
    falls back to its composed boot budget when a gateway does not implement it —
    so no existing ``ModelGateway`` implementation is obliged to grow a method.

    ⚠️ A WRAPPING DECORATOR MUST FORWARD THIS EXPLICITLY. ``runtime_checkable``
    membership is METHOD PRESENCE, so a decorator that omits the method makes
    ``isinstance`` False for the whole stack and silently returns the capability
    to its fallback — a quiet, correct-looking no-op. That is not hypothetical:
    it made this mechanism inert on every canonical Agent Run once already.
    Every wrapper on a governed path carries it (``ReceiptGatedGateway``,
    ``DemoGroundedHallucinationGateway``, ``_RunContinuityGateway``); the
    fallback is for genuine test doubles.

    WHY IT LIVES ON THE GATEWAY: the model in force for a turn is
    ``requested_model`` if a governed per-run selection pinned one, else the
    FIRST candidate the task's route yields *under the caller's lane pin, as it
    stands right now* — and the live route table (the governed repoint register)
    is read by the gateway alone. Nothing in ne-runtime can see it, so nothing
    in ne-runtime can derive this number.

    ``lane`` is the SAME lane pin the caller is about to put on its
    ``CompletionRequest`` (ADR-0029 amendment, NE-CTX-IN-FORCE round 3). Without
    it the resolver named the route primary even when a lane pin filters that
    primary OUT of the candidate list entirely — a budget computed for a model
    that will never be called, which on a wide-primary/narrow-fallback route
    makes the trim guard skip trimming and the gateway pre-flight refuse the
    turn. ``None`` means "no lane pin", exactly as on ``CompletionRequest``.
    """

    async def context_budget_for_turn(
        self, task: str, *, requested_model: str | None = None, lane: Lane | None = None
    ) -> TurnModelBudget | None:
        """The name and budget of the model this turn will actually be served by.

        ASYNC (ADR-0029 amendment, round 4, same 1.26.0 cycle): on the
        canonical Run path "the model in force" is the head of the
        evidence-filtered selection catalog — the async ``choices()`` read the
        attempt loop itself makes. A sync resolver was structurally FORCED onto
        a different authority (the posture-only configured catalog), which is
        the round-4 defect: serving/lifecycle evidence removed a model from the
        attempt order while the budget still named it. Every implementation
        must resolve the answer from THE SAME function its own attempt path
        walks, not from a parallel copy of the ordering rule.

        ``None`` when the model cannot be named (an unrouted task, an
        unregistered ``requested_model``, a lane pin no candidate matches) —
        the caller keeps its own default rather than inventing a window. Never
        raises: the routing error this would report is raised by ``complete``
        moments later, with its own typed exception and its own audit.
        """
        ...


class ModelRegistry(Protocol):
    """In-memory registry of available models (sync)."""

    def register(self, entry: ModelEntry) -> None: ...

    def get(self, model_id: str) -> ModelEntry: ...

    def list_models(
        self, *, lane: Lane | None = None, capability: str | None = None
    ) -> Sequence[ModelEntry]: ...
