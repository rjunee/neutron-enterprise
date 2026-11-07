# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""LiteLLMGateway — the concrete :class:`ne.contracts.ModelGateway`.

Embeds a litellm ``Router`` (library only, never the proxy) behind the frozen
completion seam. Routing is per-task with a lane-aware, fail-loud fallback
chain; every terminating call emits a ``CostEvent`` through the injected sink —
including a streaming call that fails mid-drain or is abandoned by its consumer
(``GeneratorExit``), each of which finalizes with an ``outcome=error`` audit.

Only the *seams* of the larger model subsystem exist here — the future units
that fill them in are:

* **NE-011** ModelRegistry — hydrates/validates ``settings.models``.
* **NE-012** egress / AIR_GAP enforcement — implemented in
  :mod:`ne.gateway.egress` (``EgressPolicy`` / ``build_egress_guard``), wired in
  through ``egress_guard`` and invoked before every provider attempt. Left
  ``None`` here it stays a no-op by the frozen seam contract; production
  composition roots pass the real guard.
* **NE-013** vLLM lane — a real ``PRIVATE_VLLM`` serving path.
* **NE-014** full instrumentation (DONE) — post-hoc span attrs via
  :func:`ne.gateway.instrumentation.annotate_span_after_call`; an optional
  ``audit_sink`` seam emits an ``AuditEvent`` alongside every ``CostEvent``,
  sharing the same frozen ``config_hash``.
* **NE-162** pre-egress PII redaction (DONE) — an injected
  ``redactor: ne.contracts.governance.Redactor | None`` seam, applied to a
  PER-ATTEMPT COPY of the outbound body for every ``FRONTIER_API`` candidate in
  both :meth:`complete` and :meth:`_stream`, via :mod:`ne.gateway.redaction`.
  Deliberately the OPPOSITE default-safety of ``egress_guard``: ``None`` there
  is a documented no-op, but ``None`` here is a REFUSAL — a frontier model
  configured/attempted without a redactor raises
  :class:`~ne.gateway.errors.RedactionRequiredError`, both at construction
  (``settings.models`` contains a frontier entry) and per-attempt (defense in
  depth against a caller mutating ``settings.models`` after construction).
  ``PRIVATE_VLLM`` candidates are never redacted (sovereign lane, untouched).
* **NE-163a** per-model-call cost/tokens in audit provenance (DONE) — the
  per-call SUCCESS ``AuditEvent`` provenance additionally carries stringified
  ``cost_usd`` / ``input_tokens`` / ``output_tokens`` / ``response_model``
  (omitted when unknown), mirroring the sibling ``CostEvent``, so audit
  replay by ``trace_id`` exposes per-model-call cost (NE-167 substrate).
* **NE-OBS B12** per-call latency/TTFT/principal on the chain (DONE) — the
  same per-call provenance additionally carries ``latency_ms`` (every
  terminating call: complete, clean stream, mid-drain failure, consumer
  abandon), ``ttft_ms`` (streams only, first chunk received), and
  ``principal`` (IFF the request carried one — byte-equal to the sibling
  ``CostEvent.actor``; omitted on service-actor fallback). ``CostEvent``
  itself gains optional ``latency_ms``/``ttft_ms`` slots (events schema
  1.1.0) carrying the identical values. Same NE-163a conventions:
  stringified in provenance, OMITTED when unknown — a measured 0 is
  ``"0"``, never dropped; an unknown is absent, never ``"0"``.
* **NE-COST-PRINCIPAL** (DONE) — every emitted ``CostEvent.actor`` is the
  CALLING PRINCIPAL's identifier (``request.principal_id``) when the request
  carries one, else the gateway's own service actor (``self._actor``) — see
  :meth:`LiteLLMGateway._cost_actor`. Gateway ``AuditEvent``s are unaffected:
  they keep ``self._actor`` (the gateway's own egress action), deliberately,
  in both :meth:`complete` and :meth:`_finalize_stream`.

litellm / Router are imported ONLY from :mod:`ne.gateway._litellm` (C3).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import format_trace_id

from ne.contracts import (
    AuditSink,
    ChatMessage,
    Completion,
    CompletionChunk,
    CompletionRequest,
    ContextBudgetExceededError,
    Lane,
    TurnModelBudget,
    Usage,
    thaw,
)
from ne.contracts.governance import Redactor
from ne.contracts.tokens import TokenEstimator
from ne.events import new_audit_event, new_cost_event
from ne.gateway._litellm import Router
from ne.gateway.config import GatewaySettings, ModelDef, TaskRoute, overhead_margin_for
from ne.gateway.cost import CostSink, extract_cost, extract_usage
from ne.gateway.errors import (
    EmptyCompletionError,
    FrontierProviderError,
    LaneMismatchError,
    ModelCallFailedError,
    ReasoningExhaustedError,
    ReasoningWithoutAnswerError,
    RedactionRequiredError,
    UnknownModelIdError,
    UnknownRoutedModelError,
    UnknownTaskError,
)
from ne.gateway.instrumentation import annotate_span_after_call
from ne.gateway.redaction import redact_payload
from ne.gateway.ssrf import redact_url_for_logs
from ne.obs import genai_span
from ne.schema import AuditOutcome

# The join text used to merge multiple system-role message contents into ONE
# leading system message (see :func:`_normalize_system_first`) — the same
# separator ne-runtime's independent (not imported; ne-gateway/ne-runtime
# cannot import each other) merge uses, derived from the SAME C3 contract.
_SYSTEM_JOINER = "\n\n"


def _normalize_system_first(messages: Sequence[ChatMessage]) -> tuple[ChatMessage, ...]:
    """The C3 chokepoint guarantee: AT MOST ONE system message, ALWAYS first.

    Every ``CompletionRequest`` this gateway receives — from every caller, on
    the way to every provider candidate — is normalized here BEFORE token
    estimation, egress redaction, and the provider call itself, so a strict
    chat template (e.g. Qwen3.6-35B-A3B-FP8's, which HTTP-400s on any system
    message that is not first) never sees a non-conforming sequence,
    regardless of what any single caller assembled. The merge is order-stable
    and lossless: every system-role message's content is collected in
    ENCOUNTER order and joined with :data:`_SYSTEM_JOINER` into ONE leading
    system message; every non-system message keeps its original relative
    order. A request with no system messages, or already conforming (at most
    one system message, already at index 0), passes through UNCHANGED — a
    conforming input is never rewritten. Exact behavior is pinned by
    ``tests/test_message_normalization.py``, not by this prose.
    """
    system_contents = [m.content for m in messages if m.role == "system"]
    if not system_contents:
        return tuple(messages)
    merged = ChatMessage(role="system", content=_SYSTEM_JOINER.join(system_contents))
    rest = [m for m in messages if m.role != "system"]
    return (merged, *rest)


def _to_wire_messages(messages: Sequence[ChatMessage]) -> list[Any]:
    """Serialize the normalized ``ChatMessage`` sequence to the provider wire body.

    THE tool-result shape guarantee (NE-GW-TOOL-RESULT). ``ChatMessage`` has
    exactly two fields — ``role`` and ``content`` — and NO tool-call id. On the
    OpenAI wire, ``role: "tool"`` does not mean "content that came from a tool";
    it means "the result of tool call ``tool_call_id``", and the id is
    MANDATORY. Our ``tool`` role therefore cannot be emitted as an OpenAI tool
    message: there is no id to emit, and (decision D3, prompted in-band
    tool-calling) there is no native ``tool_calls`` assistant message upstream
    for one to pair with — every tool result we produce is, in wire terms, an
    ORPHAN. Providers differ on what they do with an orphan:

    * litellm 1.90.3's Anthropic transform
      (``litellm_core_utils/prompt_templates/factory.py``,
      ``convert_to_anthropic_tool_result``) does an unguarded
      ``message["tool_call_id"]`` lookup while building the ``tool_result``
      block, so the orphan raises ``KeyError('tool_call_id')``, surfaced as
      ``AnthropicException - 'tool_call_id'`` / ``BadRequestError``. Measured
      live 2026-08-26: EVERY frontier turn that followed a tool call failed
      this way and fell through to the private lane — the demo answered from
      the local model with no denial and no skip record, because policy had
      ALLOWED frontier and the PROVIDER CALL is what failed.
    * an OpenAI-compatible server (the private vLLM lane) happens to accept it,
      which is exactly the "portable by accident" failure mode
      :func:`_normalize_system_first` exists to prevent.

    So the ``tool`` role is normalized HERE, at the single wire-serialization
    chokepoint both :meth:`complete` and :meth:`stream` route through, to a
    ``user`` message carrying the SAME content byte-for-byte (the content
    already self-describes as ``"<tool> returned: ..."``). This is
    provider-agnostic — there is no per-provider branch — and it is the ONLY
    place the wire body is built, so no caller and no candidate can bypass it.
    The role rewrite is byte-length-neutral (``"tool"`` and ``"user"`` are both
    4 characters), so the token pre-flight estimate stays lockstep-identical to
    ne-runtime's. Exact behavior is pinned by
    ``tests/test_tool_result_wire_shape.py``, not by this prose.
    """
    wire: list[Any] = []
    for message in messages:
        dumped = message.model_dump()
        if dumped["role"] == "tool":
            dumped["role"] = "user"
        wire.append(dumped)
    return wire


def _reasoning_block_count(message: object) -> int:
    """How many reasoning/thinking blocks the provider's reply carried — COUNT ONLY.

    Exists because ``reasoning_content`` alone cannot answer "did this model
    reason?". Read from litellm 1.90.3, not assumed:

    * ``AnthropicConfig.extract_response_content``
      (``llms/anthropic/chat/transformation.py``) collects every ``thinking``
      and ``redacted_thinking`` content block onto ``message.thinking_blocks``,
      then derives ``message.reasoning_content`` as the concatenation of ONLY
      each block's ``thinking`` field.
    * ``ChatCompletionThinkingBlock`` (``types/llms/openai.py``) is exactly
      ``{type, thinking, signature}``; ``ChatCompletionRedactedThinkingBlock``
      is exactly ``{type, data}``.

    Two consequences, and they are why this returns a number and not text:

    1. There is NO answer text on a thinking block that ``reasoning_content``
       does not already carry. The only other fields are ``signature`` /
       ``data`` — opaque provider replay tokens. Serving those, or serving the
       reasoning as the answer, would be fabricating an answer.
    2. The two channels can nonetheless DISAGREE. A ``redacted_thinking`` block
       has no ``thinking`` field at all, and a provider that returns thinking
       with its display omitted returns ``thinking=""`` plus a signature — both
       leave ``reasoning_content`` empty while the model demonstrably reasoned
       and billed the tokens for it. The block count sees that; the char count
       cannot.

    Vendor-neutral by construction: ``thinking_blocks`` is litellm's own
    normalized ``Message`` field (``types/utils.py``), not an Anthropic wire
    name, and nothing here branches on provider or model id.
    """
    blocks = getattr(message, "thinking_blocks", None)
    if blocks is None:
        return 0
    try:
        return len(blocks)  # type: ignore[arg-type]
    except TypeError:
        # Present but unsized: report presence, never content.
        return 1


def _redacted_attempts(attempts: list[tuple[str, Exception]]) -> str:
    """Render a fallback-chain failure list with each provider exception's text
    credential-redacted, so an ``api_base`` credential a provider error may echo
    can never reach an audit detail or error message."""
    return "; ".join(f"{n}: {type(e).__name__}: {redact_url_for_logs(str(e))}" for n, e in attempts)


def _sanitize_attempt_error(model: ModelDef, exc: Exception) -> Exception:
    """Return the failure safe to retain in a candidate attempt chain.

    Frontier provider and response-parsing errors are reduced to their class
    name because either kind can carry provider-controlled text that echoes a
    prompt. Private-lane errors stay intact for local diagnosis.
    """
    if model.lane == Lane.FRONTIER_API:
        return FrontierProviderError(type(exc).__name__)
    return exc


def _estimate_messages_tokens(estimator: TokenEstimator, messages: list[Any]) -> int:
    """Estimate the token count of the UN-redacted outbound message body.

    Serializes with ``json.dumps(..., default=str)`` so any non-JSON leaf (e.g.
    an enum or datetime that slipped into a message dict) degrades to its string
    form rather than raising. Estimating the un-redacted body is accurate for
    the PRIVATE_VLLM reproducer (design §3.3); frontier redaction only shrinks
    the body (placeholders ≤ originals), and that small drift is absorbed by the
    overhead margin — a pre-flight skip on a frontier candidate is therefore
    conservative, never a false pass."""
    return estimator.estimate(json.dumps(messages, ensure_ascii=False, default=str))


# LiteLLM request params that carry prompt text to the provider and therefore
# consume the serving window ALONGSIDE the messages. ``tools``/``functions`` are
# the dominant contributors — a large tool catalog can dwarf a short prompt, so a
# messages-only estimate would let it slip past the pre-flight and blow the window
# at the provider (Codex C1). ``tool_choice``/``response_format`` are smaller but
# still egress. Non-prompt params (``max_tokens``, ``temperature``, ``stream``,
# ``mock_response``, …) are excluded: they do not consume prompt context.
_PROMPT_BEARING_PARAM_KEYS: tuple[str, ...] = (
    "tools",
    "functions",
    "tool_choice",
    "response_format",
)


def _estimate_prompt_tokens(
    estimator: TokenEstimator, messages: list[Any], params: Mapping[str, Any]
) -> int:
    """Estimate the FULL un-redacted prompt-bearing payload — messages PLUS any
    tool/function schemas and other prompt-bearing request params that egress to
    the provider and consume the serving window (Codex C1: a short prompt with a
    large tool catalog must skip / fall back, not silently overflow the window).

    Only the keys in :data:`_PROMPT_BEARING_PARAM_KEYS` are folded in; every other
    request param is a control knob that does not occupy prompt context. Including
    them here is one-sided safe: the estimate can only grow, so a pre-flight skip
    stays conservative (never a false pass). Serialization mirrors
    :func:`_estimate_messages_tokens` (``default=str`` degrades non-JSON leaves)."""
    payload: list[Any] = list(messages)
    for key in _PROMPT_BEARING_PARAM_KEYS:
        value = params.get(key)
        if value is not None:
            payload.append(value)
    return estimator.estimate(json.dumps(payload, ensure_ascii=False, default=str))


def _final_failure(
    attempts: list[tuple[str, Exception]],
    *,
    task: str | None = None,
    candidates: Sequence[ModelDef] | None = None,
) -> Exception:
    """The typed error raised when every candidate failed (design §3.3).

    If EVERY attempt was a budget pre-flight skip, raise the PRIMARY candidate's
    ``ContextBudgetExceededError`` (decided: the primary's counts are the honest
    summary of "this prompt is too big for the whole chain"). A mixed set (some
    budget-skips, some provider errors) raises ``ModelCallFailedError`` carrying
    the whole mix — pinned by test.

    ``task`` and ``candidates`` only enrich the PUBLISHED sentence (which task
    was being routed, and the lane of each attempted model); both are optional,
    so the budget-skip arm and any direct construction are unaffected. The lane
    map is derived from the RESOLVED ``ModelDef``s rather than re-read from
    config, so it always describes the models this call actually tried."""
    if attempts and all(isinstance(e, ContextBudgetExceededError) for _, e in attempts):
        return attempts[0][1]
    lanes = {d.name: d.lane.value for d in candidates} if candidates else None
    return ModelCallFailedError(tuple(attempts), task=task, lanes=lanes)


def _current_trace_id() -> str | None:
    """Capture the active span's trace_id as a hex string, or ``None`` under the
    default no-op tracer (``ctx.is_valid`` is False → existing tests unaffected).

    ENRICHMENT FALLBACK ONLY (NE-166): an explicit ``request.trace_id`` — the
    data carrier threaded in from the caller's run-scoped correlation id — always
    wins when present. This function's span-derived value is used only when the
    request supplies no explicit id, which is exactly today's behavior for a
    caller that predates NE-166 (``trace_id`` defaults to ``None``)."""
    ctx = trace.get_current_span().get_span_context()
    return format_trace_id(ctx.trace_id) if ctx.is_valid else None


def _usage_provenance(
    pt: int | None,
    ct: int | None,
    cost: float | None,
    response_model: object | None,
    *,
    latency_ms: int | None,
    ttft_ms: int | None,
    principal: str | None,
) -> dict[str, str]:
    """NE-163a/B12 — stringified NON-PII usage metadata merged into the per-call
    ``AuditEvent`` provenance so ``/v1/audit/replay?trace_id`` exposes per-node
    cost/tokens/latency (NE-167 substrate); keys are OMITTED when unknown
    (never the string ``'None'``). ``cost_usd`` uses ``str(cost)`` — the shortest
    round-tripping float repr — so it equals the sibling ``CostEvent.cost_usd``
    EXACTLY (Codex P2: a fixed ``.6f`` format silently zeroed sub-1e-6 USD costs
    and broke the per-trace cost-aggregation equality the NE-167 view relies on).

    B12 keys, same disciplines:

    * ``latency_ms`` / ``ttft_ms`` — stringified whole-ms ints, byte-equal to
      the sibling ``CostEvent`` slots (one measurement, stringified once). A
      measured ``0`` IS emitted as ``"0"`` (a sub-millisecond call is a
      measurement); only ``None`` (not measured) omits the key — absent means
      unknown, never zero, and zero means zero, never unknown.
    * ``principal`` — the calling principal's identifier, present IFF the
      request carried one, byte-equal to the sibling ``CostEvent.actor`` in
      that case. When the CostEvent fell back to the gateway's service actor
      the key is OMITTED — the service actor is not a principal, and stamping
      it here would fabricate attribution (§0.4 recorded-attribution-only).
      C12/PII posture: this is the SAME identity value ``CostEvent.actor``
      already carries (NE-COST-PRINCIPAL) — no new identity class, no secret
      literals, no widening.
    """
    out: dict[str, str] = {}
    if cost is not None:
        out["cost_usd"] = str(cost)
    if pt is not None:
        out["input_tokens"] = str(pt)
    if ct is not None:
        out["output_tokens"] = str(ct)
    if response_model is not None:
        out["response_model"] = str(response_model)
    if latency_ms is not None:
        out["latency_ms"] = str(latency_ms)
    if ttft_ms is not None:
        out["ttft_ms"] = str(ttft_ms)
    if principal is not None:
        out["principal"] = principal
    return out


#: The response-message channels a completion's output can arrive on, as litellm
#: normalizes them. A FIXED allowlist, deliberately: the census below reports
#: which of THESE were populated, so a provider field nobody anticipated can
#: never widen what a record publishes.
_RESPONSE_CHANNELS: tuple[str, ...] = (
    "content",
    "reasoning_content",
    "thinking_blocks",
    "tool_calls",
    "function_call",
    "refusal",
    "annotations",
    "audio",
    "provider_specific_fields",
)


def _channel_census(message: object) -> str:
    """Which response channels carried something, and how much — counts only.

    Exists because of a concrete diagnostic dead end (live, 2026-08-26): the
    provider BILLED 182 and 65 completion tokens on two calls whose ``content``
    and ``reasoning_content`` were both empty. The tokens went somewhere, the
    record could not say where, and the only way to find out was to re-run a
    query that no longer reproduces. This is the answer to "where did the output
    go?" on the next occurrence, without capturing one byte of it.

    C12: the channel NAMES are litellm's own field identifiers (server-owned, a
    fixed allowlist above) and the values are SIZES — ``len()`` of a string, a
    list, or a mapping. No channel's CONTENT is read, including
    ``provider_specific_fields``, whose keys are counted and never named.
    Renders ``"none"`` when every channel was empty, which is itself the finding.
    """
    populated: list[str] = []
    for channel in _RESPONSE_CHANNELS:
        value = getattr(message, channel, None)
        if value is None:
            continue
        try:
            size = len(value)  # type: ignore[arg-type]
        except TypeError:
            # A scalar (or an object without a length) that is nonetheless
            # present: report its presence, never its value.
            size = 1 if value else 0
        if size:
            populated.append(f"{channel}={size}")
    return ",".join(populated) or "none"


def _routing_provenance(passed_over: Sequence[str]) -> dict[str, str]:
    """Where in THIS request's attempt sequence the serving candidate sat.

    A record that names only the model that answered cannot distinguish "the
    route's first choice answered" from "the first choice was unreachable and
    the next candidate answered instead" — a governance fact, since the second
    means customer text went to a different model than the route declares.

    * ``route_position`` — ``"primary"`` when NOTHING was attempted before the
      candidate that served, ``"fallback"`` when something was and did not
      serve. This describes the attempt sequence, NOT the configured chain: a
      lane-pinned request whose only eligible candidate served is ``primary``,
      because no fallback occurred. Callers pass the names accumulated in
      ``attempts`` BEFORE the serving candidate, so a provider error and a
      budget pre-flight skip both count — in either case that candidate was
      reached for and did not answer.
    * ``fallback_from`` — those names, in attempt order, present IFF the
      position is ``fallback``. Omitted rather than emptied when nothing was
      passed over: an empty string would read as a name.

    C12: DECLARED MODEL NAMES ONLY. The exceptions in ``attempts`` are not read
    here — provider error text can echo prompt substrings (design §10.3 rule 5)
    and an ``api_base`` can carry a credential; neither may ride a key that a
    console renders. The failure detail already has its own redacted home in
    the all-candidates-failed record's ``detail``.
    """
    if not passed_over:
        return {"route_position": "primary"}
    return {"route_position": "fallback", "fallback_from": ",".join(passed_over)}


def _request_principal(request: CompletionRequest) -> str | None:
    """B12 — the request's calling principal, or ``None`` when it has none.

    The SINGLE truthiness decision shared by :meth:`LiteLLMGateway._cost_actor`
    (which falls back to the service actor) and the provenance ``principal``
    key (which is omitted): deriving both from this one function makes the
    byte-equality between ``CostEvent.actor`` and ``provenance['principal']``
    structural — they are the same string when a principal exists, and the
    provenance key cannot exist when the CostEvent fell back. A falsy value
    (``None`` or ``""`` — ADR-0018 truthiness) names no one.
    """
    return request.principal_id if request.principal_id else None


def _requested_model_provenance(request: CompletionRequest) -> dict[str, str]:
    """The governed per-call choice, present only when the caller supplied it.

    ``model_name`` on the sibling provenance remains the candidate that actually
    served (or was actually attempted). Keeping the two keys independent makes a
    declared fallback visible and keeps a legacy request free of invented state.
    """
    if request.requested_model is None:
        return {}
    return {"requested_model": request.requested_model}


class LiteLLMGateway:
    """Provider-opaque completion gateway over an embedded litellm Router."""

    def __init__(
        self,
        settings: GatewaySettings,
        cost_sink: CostSink,
        *,
        actor: str = "ne-gateway",
        egress_guard: Callable[[ModelDef], None] | None = None,
        audit_sink: AuditSink | None = None,
        redactor: Redactor | None = None,
        route_provider: Callable[[], Mapping[str, TaskRoute]] | None = None,
    ) -> None:
        self._settings = settings
        self._cost_sink = cost_sink
        self._actor = actor
        self._egress_guard = egress_guard
        # NE-MODEL-ROUTING §5: the governed LIVE route seam, deliberately the
        # SAME shape as ``EgressPolicy(settings_provider=...)`` — a per-call
        # provider over a durable governed register, so a change takes effect on
        # the next call in every process sharing the store, with no restart.
        #
        # ``None`` is the byte-identical legacy path: ``_routes()`` returns
        # ``settings.tasks`` and nothing downstream can tell the difference.
        # The provider can only ever RE-SELECT among ``settings.models`` (see
        # ``_candidates``) — it cannot introduce a model, an ``api_base``, a
        # provider, a credential, or a task. Every reachable endpoint is still
        # exactly the boot-composed, SSRF-validated set below.
        self._route_provider = route_provider
        self._audit_sink = audit_sink
        self._redactor = redactor
        self._estimator = settings.estimator()
        self._by_name: dict[str, ModelDef] = {d.name: d for d in settings.models}
        # NE-162 fail-closed redaction: ``None`` is a REFUSAL on the frontier lane,
        # never a silent no-op (the deliberate asymmetry with egress_guard). The
        # guarantee is enforced by the per-attempt runtime check in
        # ``_prepare_egress_body`` — it fires exactly when a FRONTIER_API candidate
        # is about to egress without a redactor, so no unredacted bytes ever leave.
        # We deliberately do NOT assert this at construction: constructing a gateway
        # over a FRONTIER_API model without a redactor is legitimate in tests and
        # non-production composition roots (routing/egress/lane coverage that never
        # actually egresses to the frontier lane); the sanctioned production
        # composition root (``ne.api.platform._default_gateway``) always wires
        # ``create_redactor(...)``, so a real frontier deployment is always covered.
        self._router = Router(
            model_list=[
                {
                    "model_name": d.name,
                    "litellm_params": {
                        "model": d.litellm_model,
                        **({"api_base": d.api_base} if d.api_base else {}),
                        **d.extra_params,
                    },
                }
                for d in settings.models
            ],
            num_retries=settings.num_retries,
            timeout=settings.timeout_seconds,
        )
        self._tracer = trace.get_tracer("ne.gateway")

    def _cost_actor(self, request: CompletionRequest) -> str:
        """NE-COST-PRINCIPAL: the actor to stamp on the CostEvent for this call.

        Computed FRESH from ``request`` on every call — never cached on
        ``self`` — so a principal-less request can NEVER inherit an earlier
        request's principal. ``request.principal_id`` wins when it is a
        truthy value (a genuine principal identifier); a falsy value (``None``
        or ``""`` — ADR-0018's decided divergence from ``trace_id``'s
        explicit-empty-wins semantics: an empty string names no one) falls
        back to the gateway's own service actor (``self._actor``) — the
        honest attribution for a boot-time warmup or background sync call
        that has no calling principal.

        CostEvent attribution ONLY. Gateway ``AuditEvent``s deliberately keep
        ``self._actor`` unchanged (C10-conservative): they record the
        gateway's own egress action, joinable to the causing principal via
        ``trace_id``, not by widening the audit actor — B12 records the
        principal as a PROVENANCE key on those events instead
        (:func:`_usage_provenance`), leaving the chained ``actor`` field
        untouched.
        """
        principal = _request_principal(request)
        return principal if principal is not None else self._actor

    # -- routing ---------------------------------------------------------------

    def _routes(self) -> Mapping[str, TaskRoute]:
        """The routing table in force RIGHT NOW — ONE provider read.

        ``complete``/``_stream`` snapshot this ONCE and thread the result into
        BOTH :meth:`_candidates` and :meth:`_call_params`, so a governed flip
        landing mid-call can never split one call's candidate list and its
        params across two different routes (the ``EgressPolicy.__call__``
        discipline, same reason).

        The override REPLACES A PRIMARY, nothing else. ``fallbacks`` and
        ``params`` are carried over from the boot route, and a task the boot
        config does not route is not minted here — so a runtime table can
        neither widen a route nor invent a capability, only re-point one.
        """
        boot = self._settings.tasks
        if self._route_provider is None:
            return boot
        overrides = self._route_provider()
        if not overrides:
            return boot
        merged: dict[str, TaskRoute] = dict(boot)
        for task, override in overrides.items():
            base = boot.get(task)
            if base is None:
                # Not a routed task at boot ⇒ not a route. The unknown-task
                # refusal below stays the answer.
                continue
            merged[task] = TaskRoute(
                model=override.model, fallbacks=base.fallbacks, params=base.params
            )
        return merged

    async def context_budget_for_turn(
        self, task: str, *, requested_model: str | None = None, lane: Lane | None = None
    ) -> TurnModelBudget | None:
        """NE-CTX-IN-FORCE: name + budget of the model this turn will be served by.

        Satisfies the optional :class:`ne.contracts.models.TurnContextBudget`
        capability so the Layer-2 runtime trim guard can size itself to the
        model ACTUALLY IN FORCE instead of to whatever the composition root's
        default task happened to route to at boot.

        THE candidate is ``self._candidates(...)[0]`` — the LITERAL function
        ``complete``/``_stream`` call one moment later, not a re-statement of
        its rule. Three review rounds each found a hand-rolled copy of the
        candidate order drifting from the real one (an ignored lane pin was
        round 3's); consuming the real function is what makes a fourth drift
        structurally impossible HERE: any future change to candidate order
        (a new filter, a new pin, a reordering) moves the budget with it,
        because there is nothing separate left to forget to change.

        The same rule, NOT the same snapshot, and the difference is stated
        because it is real: this is one ``self._routes()`` read (inside
        ``_candidates``) and ``complete`` takes its own a moment later off the
        same mutable DB-backed register. A governed repoint landing between
        them makes the two disagree. The DANGEROUS direction — trimmed to a
        wide window, then served by a narrow model — is caught by the
        per-candidate budget PRE-FLIGHT in :meth:`complete`, which measures
        the estimate against the window of the model it is ACTUALLY about to
        call; the other direction merely wastes window for one turn.

        LATER fallbacks are deliberately not considered (design §2.2: the trim
        targets the first candidate, not min-over-candidates). A SECONDARY
        roll onto a narrower fallback is likewise the per-candidate
        pre-flight's belt.

        ``async`` per the ADR-0029 round-4 amendment; this implementation has
        nothing to await — the route table and registry are sync reads — but
        the Run-path implementation must await the evidence-filtered catalog
        its own attempt loop reads, and one contract serves both.

        Returns ``None``, never raises, when the model cannot be named: the
        SAME conditions make ``_candidates`` raise, so the refusal cases below
        are exactly ``complete``'s typed errors (``UnknownTaskError`` /
        ``UnknownModelIdError`` / ``UnknownRoutedModelError`` /
        ``LaneMismatchError``), each raised and audited there an instant
        later while the caller keeps its composed default here.
        """
        try:
            first = self._candidates(task, lane, requested_model=requested_model)[0]
        except (
            UnknownTaskError,
            UnknownModelIdError,
            UnknownRoutedModelError,
            LaneMismatchError,
        ):
            return None
        return TurnModelBudget(model=first.name, budget=self._settings.context_budget_for(first))

    def _candidates(
        self,
        task: str,
        lane: Lane | None,
        routes: Mapping[str, TaskRoute] | None = None,
        *,
        requested_model: str | None = None,
    ) -> list[ModelDef]:
        table = self._routes() if routes is None else routes
        route = table.get(task)
        if route is None:
            raise UnknownTaskError(task, list(table))
        if requested_model is not None:
            requested = self._by_name.get(requested_model)
            if requested is None:
                raise UnknownModelIdError(requested_model, list(self._by_name))
            # Validate the EXPLICIT candidate before filtering the fallback
            # chain. Otherwise a caller could request a frontier model while
            # pinning the private lane and silently receive a matching private
            # fallback — exactly the false-success BD-15 must prevent.
            if lane is not None and requested.lane != lane:
                raise LaneMismatchError(
                    task,
                    lane.value,
                    [(requested.name, requested.lane.value)],
                )
        primary = requested_model if requested_model is not None else route.model
        # An explicit model on a version-pinned Agent policy is an exact
        # governed effect, never a hint. Automatic policy calls retain the boot
        # fallback chain, as do legacy shared tasks whose compatibility contract
        # predates per-Run policy pinning. The Agent worker owns authorized auto
        # fallback one candidate/effect at a time.
        names = (
            [primary]
            if requested_model is not None and task.startswith("agent.")
            else list(dict.fromkeys((primary, *route.fallbacks)))
        )
        # A name outside the BOOT model set is a LOUD refusal, never a silent
        # revert to the boot primary (NE-MODEL-ROUTING §2.1): a swap that
        # quietly did not happen is indistinguishable from one that did.
        for n in names:
            if n not in self._by_name:
                raise UnknownRoutedModelError(task, n, list(self._by_name))
        defs = [self._by_name[n] for n in names]
        if lane is None:
            return defs
        matching = [d for d in defs if d.lane == lane]
        if not matching:
            raise LaneMismatchError(task, lane.value, [(d.name, d.lane.value) for d in defs])
        return matching

    async def _record_model_observation(
        self,
        *,
        model: ModelDef,
        request: CompletionRequest,
        operation: str,
        outcome: AuditOutcome,
        trace_id: str | None,
    ) -> None:
        """Append one timestamped, names-only observation of an actual attempt.

        Callers invoke this only after provider transport was attempted. Budget
        skips and policy refusals therefore cannot masquerade as model-health
        failures, while the event's factory-owned timestamp remains the
        authoritative observation time for a later live-choice reader.
        """
        if self._audit_sink is None:
            return
        await self._audit_sink.append(
            new_audit_event(
                action="ne.gateway.model.observed",
                resource=model.litellm_model,
                outcome=outcome,
                actor=self._actor,
                detail=(f"operation={operation} model={model.name} outcome={outcome.value}"),
                trace_id=trace_id,
                provenance={
                    "task": request.task,
                    "lane": model.lane.value,
                    "model_name": model.name,
                    "operation": operation,
                    **_requested_model_provenance(request),
                },
            )
        )

    def _call_params(
        self, request: CompletionRequest, routes: Mapping[str, TaskRoute] | None = None
    ) -> tuple[dict[str, Any], str | None]:
        table = self._routes() if routes is None else routes
        route = table[request.task]
        # ``request.params`` is an ``AnyMap`` (NE-GATEWAY-FROZENDICT): pydantic
        # froze every NESTED mapping value into a ``FrozenDict``. litellm's
        # ``type_to_response_format_param`` (and, symmetrically, any other
        # nested param litellm inspects) requires ``isinstance(x, dict)`` and
        # raises ``TypeError`` on any other ``Mapping`` — a bare ``FrozenDict``
        # included. Both lanes forward ``merged`` byte-identical or
        # near-identical to litellm (``PRIVATE_VLLM`` byte-identical via
        # ``_prepare_egress_body``; ``FRONTIER_API`` redacted but not
        # re-frozen), so the egress boundary normalizes every top-level value
        # to plain builtins via the sanctioned ``ne.contracts.thaw`` (see its
        # docstring, sanctioned caller #2) before it ever reaches litellm.
        merged: dict[str, Any] = {
            key: thaw(value) for key, value in {**route.params, **dict(request.params)}.items()
        }
        # ``kpi_tag`` is a reserved request-param key (CompletionRequest has no
        # kpi_tag field): it is stamped onto the CostEvent, never forwarded to
        # litellm.
        raw = merged.pop("kpi_tag", None)
        kpi_tag = None if raw is None else str(raw)
        return merged, kpi_tag

    # -- redaction (NE-162) ------------------------------------------------------

    async def _prepare_egress_body(
        self,
        model: ModelDef,
        request: CompletionRequest,
        messages: list[Any],
        merged: dict[str, Any],
        kpi_tag: str | None,
    ) -> tuple[list[Any], dict[str, Any]]:
        """Return the per-attempt (messages, params) actually sent to litellm.

        ``PRIVATE_VLLM`` candidates pass through byte-identical (sovereign
        lane). ``FRONTIER_API`` candidates are redacted into brand-new copies —
        the shared ``messages``/``merged`` built once per request are NEVER
        mutated.

        SCOPE: ``self._redactor`` is ONE injected instance, and this method is
        the ONLY frontier-egress path in both ne-api and ne-worker. It therefore
        applies to EVERY ``FRONTIER_API`` completion, for every principal, every
        agent, and every field of every body. The redactor is handed a string,
        not a caller, so any policy it carries (e.g. ne-govern's configured
        address exemption) is necessarily global to frontier egress — never
        scoped to one tool, tenant, or call path. Anything needing caller
        awareness must be enforced before this boundary, not inside a redactor.

        Called OUTSIDE the provider try/except so a redactor failure
        (``self._redactor is None`` or a raised ``RedactionError``) propagates
        UNWRAPPED like an ``egress_guard`` denial: it is a policy refusal, not
        a provider failure, so it must never be caught into ``attempts`` and
        rolled to another frontier candidate.
        """
        if model.lane != Lane.FRONTIER_API:
            return messages, merged

        if self._redactor is None:
            # Defense-in-depth: the __init__ assertion covers settings.models
            # at construction time; this covers a caller mutating the list
            # afterward. None is a REFUSAL on the frontier lane, never a no-op.
            raise RedactionRequiredError(
                f"model {model.name!r} is FRONTIER_API but no redactor is "
                "injected on this gateway; pre-egress redaction is mandatory "
                "(C2/C12 fail-closed) — wire ne.govern.create_redactor(...) at "
                "the composition root"
            )

        trace_id = request.trace_id if request.trace_id is not None else _current_trace_id()
        try:
            # Offloaded to a worker thread (asyncio.to_thread): the real
            # PresidioRedactor backend synchronously launches-and-waits a
            # subprocess for EVERY string leaf, so calling redact_payload
            # directly on this coroutine would block the event loop for the
            # whole redaction walk — one frontier request with a large body
            # (tool schemas, RAG context) would stall every other concurrent
            # request. Exceptions propagate through to_thread unwrapped, so
            # the fail-closed except-and-abort semantics below are unchanged.
            call_messages, call_params, categories, hits = await asyncio.to_thread(
                redact_payload, self._redactor, messages, merged
            )
        except Exception as exc:
            # Best-effort audit of the refusal: never let an audit-append
            # failure mask the original redaction failure (fail-closed still
            # wins). Detail carries the exception CLASS NAME ONLY — backend
            # error text (e.g. Presidio stderr) may itself carry PII and must
            # never reach an audit record.
            with contextlib.suppress(BaseException):
                if self._audit_sink is not None:
                    await self._audit_sink.append(
                        new_audit_event(
                            action="redact.pre_egress",
                            resource=model.litellm_model,
                            outcome=AuditOutcome.denied,
                            actor=self._actor,
                            detail=(
                                f"pre-egress redaction FAILED ({type(exc).__name__}); "
                                "frontier egress refused (fail-closed)"
                            ),
                            kpi_tag=kpi_tag,
                            trace_id=trace_id,
                            provenance={
                                "task": request.task,
                                "lane": model.lane.value,
                                "model_name": model.name,
                                **_requested_model_provenance(request),
                            },
                        )
                    )
            raise

        if self._audit_sink is not None:
            categories_str = ",".join(f"{k}={v}" for k, v in categories.items()) or "none"
            await self._audit_sink.append(
                new_audit_event(
                    action="redact.pre_egress",
                    resource=model.litellm_model,
                    outcome=AuditOutcome.success,
                    actor=self._actor,
                    detail=f"pre-egress redaction: hits={hits} categories={categories_str}",
                    kpi_tag=kpi_tag,
                    trace_id=trace_id,
                    provenance={
                        "task": request.task,
                        "lane": model.lane.value,
                        "model_name": model.name,
                        **_requested_model_provenance(request),
                        "redaction_hits": str(hits),
                        "redaction_categories": categories_str,
                    },
                )
            )
        return call_messages, call_params

    # -- completion ------------------------------------------------------------

    async def complete(self, request: CompletionRequest) -> Completion:
        # ONE route snapshot for the whole call (NE-MODEL-ROUTING §5).
        routes = self._routes()
        candidates = self._candidates(
            request.task,
            request.lane,
            routes,
            requested_model=request.requested_model,
        )
        merged, kpi_tag = self._call_params(request, routes)
        # C3 chokepoint (NE-AGENT-MSG-ORDER): normalize to AT MOST ONE leading
        # system message BEFORE token estimation, redaction, and every provider
        # attempt — so every caller and every candidate gets a conforming
        # sequence, regardless of what was assembled upstream. ``_to_wire_messages``
        # then applies the tool-result shape guarantee (NE-GW-TOOL-RESULT).
        messages: list[Any] = _to_wire_messages(_normalize_system_first(request.messages))
        # Estimate the un-redacted prompt-bearing body ONCE — messages PLUS any
        # tool/function schemas in the shared params (Codex C1: tools egress and
        # consume the window). Pure arithmetic, reused per candidate against each
        # candidate's own window/reserve (design §3.3); the prompt-bearing content
        # is candidate-independent (only ``max_tokens`` varies per candidate).
        estimated = _estimate_prompt_tokens(self._estimator, messages, merged)
        attempts: list[tuple[str, Exception]] = []

        for model in candidates:
            if self._egress_guard is not None:
                # Policy denial != provider failure: guard exceptions propagate
                # UNWRAPPED (the NE-012 seam), so no fallback and no CostEvent.
                # ORDER (pinned): egress-guard runs BEFORE the budget pre-flight.
                self._egress_guard(model)
            # Budget pre-flight (design §3.3): FRESH dict per candidate — the
            # confirmed shared-``merged`` poisoning fix — then default max_tokens
            # from this model (never override an explicit caller value). If the
            # estimated prompt cannot fit this candidate's input budget, append
            # the typed error OUTSIDE the provider try-block (no
            # FrontierProviderError wrap, no CostEvent, no redaction) and roll to
            # the next, larger-window candidate.
            candidate_params = dict(merged)
            candidate_params.setdefault("max_tokens", model.max_output_tokens)
            effective = int(candidate_params["max_tokens"])
            input_budget = (
                model.context_window - effective - overhead_margin_for(model.context_window)
            )
            if estimated > input_budget:
                attempts.append(
                    (
                        model.name,
                        ContextBudgetExceededError(
                            stage="gateway-preflight",
                            estimated_tokens=estimated,
                            input_budget=max(0, input_budget),
                            context_window=model.context_window,
                            dropped_turns=0,
                        ),
                    )
                )
                # An explicit per-call choice is a governed primary, not a hint.
                # Its runtime trim budget was inherited from the boot primary;
                # when this selected model cannot hold the resulting payload,
                # falling through would turn an unsafe down-route refusal into a
                # successful answer from a model the caller did not select. Keep
                # ordinary task-route budget fallback unchanged, but stop this
                # explicit request at the common audited failure path below.
                if request.requested_model == model.name:
                    break
                continue
            # NE-162: redaction runs BEFORE the provider try-block, OUTSIDE the
            # except-and-continue below, so a redaction refusal (None redactor
            # or RedactionError) propagates UNWRAPPED — never caught into
            # ``attempts`` and rolled to another frontier candidate.
            call_messages, call_params = await self._prepare_egress_body(
                model, request, messages, candidate_params, kpi_tag
            )
            started = time.monotonic()
            trace_id: str | None = None
            try:
                with genai_span(
                    self._tracer,
                    "ne.gateway.complete",
                    operation="chat",
                    provider=model.provider,
                    request_model=model.litellm_model,
                    lane=model.lane.value,
                    kpi_tag=kpi_tag,
                ):
                    resp = await self._router.acompletion(
                        model=model.name, messages=call_messages, **call_params
                    )
                    pt, ct = extract_usage(resp)
                    cost = extract_cost(resp)
                    response_model = getattr(resp, "model", None)
                    # B12: ONE monotonic reading — the span (float ms, below)
                    # and the chain/CostEvent (the same delta rounded to whole
                    # ms) both consume THIS delta. A second time.monotonic()
                    # call would mint a second number for the same call.
                    elapsed_ms = (time.monotonic() - started) * 1000
                    annotate_span_after_call(
                        trace.get_current_span(),
                        task=request.task,
                        latency_ms=elapsed_ms,
                        response_model=response_model,
                        input_tokens=pt,
                        output_tokens=ct,
                        cost_usd=cost,
                    )
                    trace_id = (
                        request.trace_id if request.trace_id is not None else _current_trace_id()
                    )
            except Exception as exc:
                await self._record_model_observation(
                    model=model,
                    request=request,
                    operation="complete",
                    outcome=AuditOutcome.error,
                    trace_id=request.trace_id,
                )
                # Frontier provider errors are reduced to the class name ONLY:
                # 4xx text can echo prompt substrings (design §10.3 rule 5).
                attempts.append((model.name, _sanitize_attempt_error(model, exc)))
                continue

            # B12: whole-ms int derived ONCE from the span's own delta — the
            # CostEvent slot and the provenance string are this same value
            # (equality by construction, the cost_usd discipline).
            latency_ms = round(elapsed_ms)
            self._cost_sink.emit(
                new_cost_event(
                    model=model.litellm_model,
                    provider=model.provider,
                    lane=model.lane.value,
                    actor=self._cost_actor(request),
                    input_tokens=pt,
                    output_tokens=ct,
                    cost_usd=cost,
                    task=request.task,
                    latency_ms=latency_ms,
                    kpi_tag=kpi_tag,
                    trace_id=trace_id,
                )
            )
            # Reasoning-channel policy (deliberate, C12): a reasoning model's
            # think text (litellm ``message.reasoning_content`` — vLLM's qwen3
            # parser field and Anthropic extended thinking both normalize here
            # on litellm 1.90.3, though the Anthropic path leaves it EMPTY when
            # the provider withholds the thinking text, which is why the block
            # COUNT is read separately below) is DISCARDED at this boundary. It is model
            # working scratch over customer text: it never enters
            # ``Completion.text`` (so never agent history, never a rendered
            # answer, never the grounding gate's input) and never an audit
            # record as TEXT — only its CHAR COUNT is recorded, in the
            # success-audit provenance below, so a trace shows THAT the model
            # reasoned and how much, never WHAT it reasoned.
            try:
                message = resp.choices[0].message
                text = message.content or ""
                reasoning_chars = len(getattr(message, "reasoning_content", None) or "")
                raw_finish = getattr(resp.choices[0], "finish_reason", None)
                # Narrowed to ``str | None`` HERE, once, so both the audit
                # provenance (which is a str->str map) and the Completion field
                # take the identical value — a provider returning a non-string
                # is read as "reported nothing" rather than stringified into a
                # fact nobody stated.
                finish_reason: str | None = raw_finish if isinstance(raw_finish, str) else None
            except Exception as exc:
                # The provider completed a real, billable transport, but its
                # response cannot satisfy the Completion contract. The
                # CostEvent above therefore remains truthful while this actual
                # provider attempt is recorded as an error and the declared
                # fallback chain remains available. Frontier parse failures use
                # the exact same class-only reduction as transport failures.
                await self._record_model_observation(
                    model=model,
                    request=request,
                    operation="complete",
                    outcome=AuditOutcome.error,
                    trace_id=trace_id,
                )
                attempts.append((model.name, _sanitize_attempt_error(model, exc)))
                continue
            # "Did this model reason?" is asked of BOTH normalized reasoning
            # channels, because they disagree. ``reasoning_chars`` is litellm's
            # concatenation of each thinking block's ``thinking`` field; a
            # redacted block has no such field, and a provider whose thinking
            # display defaults to omitted returns that field EMPTY (signature
            # only) — so a reply can carry reasoning that ``reasoning_chars``
            # reports as zero. Counting the blocks closes exactly that gap and
            # reads no content to do it (see _reasoning_block_count).
            thinking_blocks = _reasoning_block_count(message)
            if not text and (reasoning_chars > 0 or thinking_blocks > 0):
                # A reasoning model delivered NO answer. Two distinct facts live
                # here and they get two distinct terminals, because the class
                # name is what an attempt chain renders for a reader and
                # because only one of them has a remediation:
                #
                # * CUT OFF inside the think channel — finish_reason 'length'
                #   with reasoning text present (live-isolated 2026-08-04 on the
                #   GPU demo lane: 'length' at exactly max_tokens, all of it
                #   <think>). Raising max_output_tokens genuinely fixes it, so
                #   ReasoningExhaustedError says so.
                # * ENDED NORMALLY with reasoning and no content block — live
                #   2026-08-26, anthropic/claude-sonnet-5: finish_reason 'stop',
                #   144 completion tokens, content empty, reasoning_content
                #   empty, thinking_blocks=1. The budget was never the limit
                #   (144 of 24576), so the exhausted terminal would name a fix
                #   that changes nothing and a cause that did not occur.
                #   ReasoningWithoutAnswerError names what actually happened.
                #
                # Either way the treatment is the same and unchanged: fail TYPED
                # and LOUD rather than return "" (a silent empty answer the
                # runtime could only retry into UnexpectedModelBehavior); the
                # CostEvent above still meters the burned tokens; the audit
                # below records the refusal COUNTS-ONLY; and appending to
                # ``attempts`` lets a DECLARED fallback candidate serve the
                # request (visible routing, never a silent downgrade).
                #
                # What is NEVER done: promote the reasoning into the answer. A
                # thinking block holds no answer text (see
                # _reasoning_block_count), so serving this channel would be
                # fabrication, not recovery. A reply carrying nothing at all —
                # no content, no reasoning, no thinking block — is a different
                # shape and falls through to its own handling below.
                # The same "where did the billed output go?" evidence the empty
                # row carries. It belongs on THIS row too, and arguably more:
                # this is the row raised when the census is NOT "none", so it is
                # the row a reader reaches when asking that question.
                census = _channel_census(message)
                reasoning_failure: Exception
                if reasoning_chars > 0 and finish_reason == "length":
                    reasoning_failure = ReasoningExhaustedError(
                        model.name,
                        completion_tokens=ct,
                        reasoning_chars=reasoning_chars,
                        max_tokens=effective,
                        finish_reason=finish_reason,
                    )
                    reasoning_detail = (
                        f"task={request.task} lane={model.lane.value} "
                        f"model={model.name} reasoning exhausted the output "
                        f"budget: completion_tokens={ct} "
                        f"reasoning_chars={reasoning_chars} "
                        f"max_tokens={effective}"
                    )
                else:
                    reasoning_failure = ReasoningWithoutAnswerError(
                        model.name,
                        completion_tokens=ct,
                        reasoning_chars=reasoning_chars,
                        thinking_blocks=thinking_blocks,
                        max_tokens=effective,
                        finish_reason=finish_reason,
                    )
                    reasoning_detail = (
                        f"task={request.task} lane={model.lane.value} "
                        f"model={model.name} reasoned and returned NO answer: "
                        f"completion_tokens={ct} "
                        f"reasoning_chars={reasoning_chars} "
                        f"thinking_blocks={thinking_blocks} "
                        f"max_tokens={effective} "
                        f"finish_reason={finish_reason!r} "
                        f"channels={census}"
                    )
                if self._audit_sink is not None:
                    await self._audit_sink.append(
                        new_audit_event(
                            action="ne.gateway.complete",
                            resource=model.litellm_model,
                            outcome=AuditOutcome.error,
                            actor=self._actor,
                            detail=reasoning_detail,
                            kpi_tag=kpi_tag,
                            trace_id=trace_id,
                            provenance={
                                "task": request.task,
                                "lane": model.lane.value,
                                "model_name": model.name,
                                **_requested_model_provenance(request),
                                "reasoning_chars": str(reasoning_chars),
                                # The channel the reasoning actually arrived on,
                                # counts only. Present unconditionally on this
                                # row: ``reasoning_chars=0`` beside
                                # ``thinking_blocks=1`` is precisely the
                                # display-omitted / redacted shape, and it is
                                # the pair — not either number alone — that
                                # tells the reader the model reasoned.
                                "thinking_blocks": str(thinking_blocks),
                                # WHERE the billed output went, counts only —
                                # see _channel_census.
                                "response_channels": census,
                                # The provider's own stated stop reason, a
                                # provider enum and never text. It is what
                                # separates a truncated reply from one that
                                # ended of its own accord, i.e. which of the two
                                # terminals above this row carries.
                                **({"finish_reason": finish_reason} if finish_reason else {}),
                                # This candidate DID serve tokens before it
                                # failed to answer, so its placement in the
                                # attempt sequence is a real fact about the
                                # record. ``attempts`` does not yet contain
                                # this model — it is appended below.
                                **_routing_provenance([name for name, _ in attempts]),
                                **_usage_provenance(
                                    pt,
                                    ct,
                                    cost,
                                    response_model,
                                    latency_ms=latency_ms,
                                    ttft_ms=None,
                                    principal=_request_principal(request),
                                ),
                            },
                        )
                    )
                await self._record_model_observation(
                    model=model,
                    request=request,
                    operation="complete",
                    outcome=AuditOutcome.error,
                    trace_id=trace_id,
                )
                attempts.append((model.name, reasoning_failure))
                continue
            if not text:
                # NE-EMPTY-COMPLETION: content AND reasoning both empty — the
                # provider returned nothing to serve. Until this it was the one
                # remaining shape that still took the legacy "" path: audited as
                # a plain SUCCESS, metered, and handed to the runtime as a
                # zero-length answer. Downstream that is not read as "no answer";
                # pydantic-ai sees a response with a part but no usable content,
                # reports it as non-actionable ("Please return text or call a
                # tool"), and the runtime translates THAT into "the reply did not
                # match the required answer format" — a schema rejection that
                # never happened. The reader was told the wrong fact about the
                # wrong thing, and the success row left no trace of what arrived.
                #
                # So it fails typed here, exactly as the reasoning-exhausted
                # sibling above does: the CostEvent already emitted still meters
                # the real (billable) transport, the audit row below records the
                # refusal with counts only, and appending to ``attempts`` lets a
                # DECLARED fallback candidate serve the request — visible
                # routing, never a silent empty answer.
                empty = EmptyCompletionError(
                    model.name,
                    completion_tokens=ct,
                    max_tokens=effective,
                    finish_reason=finish_reason,
                )
                census = _channel_census(message)
                if self._audit_sink is not None:
                    await self._audit_sink.append(
                        new_audit_event(
                            action="ne.gateway.complete",
                            resource=model.litellm_model,
                            outcome=AuditOutcome.error,
                            actor=self._actor,
                            detail=(
                                f"task={request.task} lane={model.lane.value} "
                                f"model={model.name} returned an EMPTY completion: "
                                f"no content and no reasoning "
                                f"(completion_tokens={ct} max_tokens={effective} "
                                f"finish_reason={finish_reason!r} "
                                f"channels={census})"
                            ),
                            kpi_tag=kpi_tag,
                            trace_id=trace_id,
                            provenance={
                                "task": request.task,
                                "lane": model.lane.value,
                                "model_name": model.name,
                                **_requested_model_provenance(request),
                                # The discriminator, queryable: ZERO means the
                                # provider generated nothing; NON-ZERO on an
                                # empty reply means it generated tokens that
                                # arrived in no field this gateway reads — a
                                # mapping gap, not a silent model. Recorded so
                                # the next occurrence is diagnosable from the
                                # chain alone, without re-running the query.
                                "empty_completion_tokens": str(ct if ct is not None else 0),
                                # The provider's own stated stop reason, kept
                                # UNCONDITIONALLY on this row (unlike the
                                # success row, where only "length" is a
                                # signal). On a reply with no content it is the
                                # single most diagnostic field there is, and it
                                # is a provider enum, never text.
                                **({"finish_reason": finish_reason} if finish_reason else {}),
                                # WHERE the billed output went, counts only —
                                # see _channel_census.
                                "response_channels": census,
                                **_routing_provenance([name for name, _ in attempts]),
                                **_usage_provenance(
                                    pt,
                                    ct,
                                    cost,
                                    response_model,
                                    latency_ms=latency_ms,
                                    ttft_ms=None,
                                    principal=_request_principal(request),
                                ),
                            },
                        )
                    )
                await self._record_model_observation(
                    model=model,
                    request=request,
                    operation="complete",
                    outcome=AuditOutcome.error,
                    trace_id=trace_id,
                )
                attempts.append((model.name, empty))
                continue
            # NE-FRONTIER-OUTPUT: a completion that hit the output budget but
            # still returned content is NOT the empty case above — it has a real
            # partial answer, and throwing it away (or routing past it to a
            # fallback that would have to regenerate the whole thing) would be
            # worse than serving it. What must not happen is serving it as if it
            # were whole. So the record says so: the success row carries the
            # reason in its detail and its provenance, and the returned
            # Completion carries `finish_reason` for every consumer downstream.
            # A NON-truncated completion is unchanged in both — no new detail
            # text, no new provenance key — so the presence of the key IS the
            # signal, and every pre-existing success row stays byte-identical.
            truncated = finish_reason == "length"
            detail = f"task={request.task} lane={model.lane.value} model={model.name}"
            if truncated:
                detail += (
                    f" TRUNCATED: the model stopped at the output budget "
                    f"(finish_reason=length, completion_tokens={ct} "
                    f"max_tokens={effective}); the served answer is PARTIAL"
                )
            if self._audit_sink is not None:
                await self._audit_sink.append(
                    new_audit_event(
                        action="ne.gateway.complete",
                        resource=model.litellm_model,
                        outcome=AuditOutcome.success,
                        actor=self._actor,
                        detail=detail,
                        kpi_tag=kpi_tag,
                        trace_id=trace_id,
                        provenance={
                            "task": request.task,
                            "lane": model.lane.value,
                            "model_name": model.name,
                            **_requested_model_provenance(request),
                            # Present IFF the answer was cut off (see above):
                            # a whole answer records nothing new here.
                            **({"finish_reason": finish_reason} if truncated else {}),
                            # WHICH candidate this was: the route's first
                            # choice, or the one that answered after an earlier
                            # candidate did not. ``attempts`` holds exactly the
                            # candidates passed over before this one.
                            **_routing_provenance([name for name, _ in attempts]),
                            # Counts-only reasoning visibility (C12): present
                            # IFF the model reasoned this call; the text itself
                            # is discarded (policy comment above).
                            **(
                                {"reasoning_chars": str(reasoning_chars)}
                                if reasoning_chars > 0
                                else {}
                            ),
                            # ttft_ms=None: a non-streaming call HAS no first
                            # token distinct from its completion — the key is
                            # a stream fact and is omitted here (B12).
                            **_usage_provenance(
                                pt,
                                ct,
                                cost,
                                response_model,
                                latency_ms=latency_ms,
                                ttft_ms=None,
                                principal=_request_principal(request),
                            ),
                        },
                    )
                )
            await self._record_model_observation(
                model=model,
                request=request,
                operation="complete",
                outcome=AuditOutcome.success,
                trace_id=trace_id,
            )
            # ``text`` is message.content ONLY — reasoning_content is discarded
            # by policy (comment above), never concatenated into the answer.
            # ``finish_reason`` rides ALONG the text, never INSIDE it: appending
            # a truncation marker to the answer would corrupt every structured
            # consumer of this seam (the runtime's tool-call parse, the eval
            # judge's JSON parse, the graph extractor's wall validation).
            return Completion(
                text=text,
                model_id=model.name,
                usage=Usage(prompt_tokens=pt or 0, completion_tokens=ct or 0),
                cost_usd=cost,
                finish_reason=finish_reason,
            )

        if self._audit_sink is not None:
            await self._audit_sink.append(
                new_audit_event(
                    action="ne.gateway.complete",
                    resource=request.task,
                    outcome=AuditOutcome.error,
                    actor=self._actor,
                    detail="all model candidates failed: " + _redacted_attempts(attempts),
                    trace_id=request.trace_id,
                    provenance={
                        "task": request.task,
                        **_requested_model_provenance(request),
                    },
                )
            )
        raise _final_failure(attempts, task=request.task, candidates=candidates)

    # -- streaming -------------------------------------------------------------

    def stream(self, request: CompletionRequest) -> AsyncIterator[CompletionChunk]:
        """Return the streaming iterator.

        This is a *sync* method (per the frozen ModelGateway Protocol): calling
        the ``async def`` generator ``_stream`` returns its async-generator
        object synchronously, which is exactly the ``AsyncIterator`` the caller
        ``async for``-iterates.
        """
        return self._stream(request)

    async def _stream(self, request: CompletionRequest) -> AsyncIterator[CompletionChunk]:
        # ONE route snapshot for the whole call — mirrors complete().
        routes = self._routes()
        candidates = self._candidates(
            request.task,
            request.lane,
            routes,
            requested_model=request.requested_model,
        )
        merged, kpi_tag = self._call_params(request, routes)
        # C3 chokepoint: mirror complete() — normalize to AT MOST ONE leading
        # system message before anything else sees the sequence, then the same
        # ``_to_wire_messages`` tool-result shape guarantee complete() applies.
        messages: list[Any] = _to_wire_messages(_normalize_system_first(request.messages))
        # Mirror complete(): estimate messages PLUS prompt-bearing params (tool
        # schemas etc.) so a large tool catalog can't slip past the pre-flight on
        # the stream path either (Codex C1).
        estimated = _estimate_prompt_tokens(self._estimator, messages, merged)
        attempts: list[tuple[str, Exception]] = []

        model: ModelDef | None = None
        stream_obj = None
        trace_id: str | None = None
        # Re-bound at every attempt below; meaningful only once a stream is
        # established (the ``model is None`` guard). The 0.0 initializer exists
        # for definite-assignment only — it can never reach ``_finalize_stream``
        # because ``model``/``stream_obj`` are set in the same iteration that
        # last re-bound it.
        started = 0.0
        # Re-bound at the winning attempt (below). The empty initializer exists
        # for definite assignment only — it cannot reach ``_finalize_stream``,
        # which is guarded by ``model is None``.
        passed_over: tuple[str, ...] = ()
        for candidate in candidates:
            if self._egress_guard is not None:
                # ORDER (pinned): egress-guard BEFORE budget pre-flight.
                self._egress_guard(candidate)
            # Budget pre-flight mirrors complete() (design §3.3): fresh dict per
            # candidate, default max_tokens, skip over-budget candidates OUTSIDE
            # the provider try-block before any stream is established.
            candidate_params = dict(merged)
            candidate_params.setdefault("max_tokens", candidate.max_output_tokens)
            effective = int(candidate_params["max_tokens"])
            input_budget = (
                candidate.context_window - effective - overhead_margin_for(candidate.context_window)
            )
            if estimated > input_budget:
                attempts.append(
                    (
                        candidate.name,
                        ContextBudgetExceededError(
                            stage="gateway-preflight",
                            estimated_tokens=estimated,
                            input_budget=max(0, input_budget),
                            context_window=candidate.context_window,
                            dropped_turns=0,
                        ),
                    )
                )
                # Mirror complete(): an explicit selected primary whose payload
                # fails deterministic pre-flight is terminal. The common failure
                # block below records the refusal before raising; no fallback
                # stream may establish under a different model identity.
                if request.requested_model == candidate.name:
                    break
                continue
            # NE-162: identical treatment to complete() — redaction runs before
            # the provider try-block so a refusal propagates UNWRAPPED, before
            # any stream is established.
            call_messages, call_params = await self._prepare_egress_body(
                candidate, request, messages, candidate_params, kpi_tag
            )
            started = time.monotonic()
            try:
                with genai_span(
                    self._tracer,
                    "ne.gateway.stream",
                    operation="chat",
                    provider=candidate.provider,
                    request_model=candidate.litellm_model,
                    lane=candidate.lane.value,
                    kpi_tag=kpi_tag,
                ):
                    stream_obj = await self._router.acompletion(
                        model=candidate.name, messages=call_messages, stream=True, **call_params
                    )
                    # Token usage / response model are not known until the stream
                    # is drained, by which point this establish-phase span has
                    # closed — so only task + establish latency are attached here.
                    annotate_span_after_call(
                        trace.get_current_span(),
                        task=request.task,
                        latency_ms=(time.monotonic() - started) * 1000,
                    )
                    trace_id = (
                        request.trace_id if request.trace_id is not None else _current_trace_id()
                    )
                model = candidate
                # Frozen HERE, before any later mutation of ``attempts`` could
                # reach it: the candidates passed over BEFORE the one that
                # established this stream. Every finalization path (clean,
                # mid-drain failure, consumer abandon) records the same
                # placement, because it is a fact about how this stream was
                # routed, not about how it ended.
                passed_over = tuple(name for name, _ in attempts)
                break
            except Exception as exc:
                await self._record_model_observation(
                    model=candidate,
                    request=request,
                    operation="stream",
                    outcome=AuditOutcome.error,
                    trace_id=request.trace_id,
                )
                attempts.append((candidate.name, _sanitize_attempt_error(candidate, exc)))
                continue

        if model is None or stream_obj is None:
            if self._audit_sink is not None:
                await self._audit_sink.append(
                    new_audit_event(
                        action="ne.gateway.stream",
                        resource=request.task,
                        outcome=AuditOutcome.error,
                        actor=self._actor,
                        detail="all model candidates failed: " + _redacted_attempts(attempts),
                        trace_id=request.trace_id,
                        provenance={
                            "task": request.task,
                            **_requested_model_provenance(request),
                        },
                    )
                )
            raise _final_failure(attempts, task=request.task, candidates=candidates)

        # Every TERMINATING stream finalizes exactly once: clean exhaustion →
        # success; a mid-drain provider failure or a consumer abandon
        # (GeneratorExit from aclose()/stop-button) → error. This keeps the C6
        # 'every call → CostEvent + audit' guarantee true for a stream that
        # egressed data (the pre-fix bare drain emitted nothing on either abnormal
        # exit). Both abnormal arms best-effort finalize then re-raise bare — an
        # emission failure must never mask the original termination.
        last_chunk: Any = None
        # B12 ``ttft_ms``: the monotonic delta from the winning attempt's start
        # to the FIRST CHUNK RECEIVED from the provider (the plan's
        # "first-chunk delta in the stream wrapper") — measured on receipt, not
        # on yield, so a role-priming or usage-only first chunk still marks the
        # provider's first byte. ``None`` survives to finalization only when the
        # stream terminated before any chunk arrived (absent means unknown).
        ttft_ms: int | None = None
        # NE-FRONTIER-OUTPUT: the stream's terminating reason. A stream is where
        # truncation is HARDEST to notice — the consumer has already been handed
        # every token before anything knows how it ended — so the reason is
        # captured from the chunk that carries it (NOT ``last_chunk``: on an
        # OpenAI-compatible stream the final chunk is the usage-only one, whose
        # ``choices`` is empty and which therefore reports no finish_reason at
        # all). Last non-None wins; ``None`` means the provider never said.
        stream_finish_reason: str | None = None
        try:
            async for chunk in stream_obj:
                if ttft_ms is None:
                    ttft_ms = round((time.monotonic() - started) * 1000)
                last_chunk = chunk
                if not chunk.choices:
                    # Usage-only terminal chunk (OpenAI-compatible streams emit one
                    # with empty ``choices`` when stream_options include_usage is
                    # requested via params). Nothing to yield; it stays bound as
                    # ``last_chunk`` so its usage feeds the cost event below.
                    continue
                chunk_finish = getattr(chunk.choices[0], "finish_reason", None)
                if isinstance(chunk_finish, str):
                    stream_finish_reason = chunk_finish
                delta = chunk.choices[0].delta.content
                if delta is None:
                    continue
                yield CompletionChunk(delta=delta, model_id=model.name)
        except GeneratorExit:
            # Consumer abandoned the stream (aclose() / stop-button). PEP 525: we
            # may await here but MUST NOT yield, and MUST re-raise bare. The
            # suppress guards against an emission failure MASKING the original
            # termination — the sync cost emit inside finalize runs first, so it
            # lands even if the audit await is cancelled mid-cancellation.
            with contextlib.suppress(BaseException):
                await self._finalize_stream(
                    model=model,
                    request=request,
                    passed_over=passed_over,
                    kpi_tag=kpi_tag,
                    last_chunk=last_chunk,
                    started=started,
                    ttft_ms=ttft_ms,
                    trace_id=trace_id,
                    outcome=AuditOutcome.error,
                    observe_model=False,
                    detail=(
                        f"task={request.task} lane={model.lane.value} model={model.name} "
                        "stream interrupted by consumer (GeneratorExit) after partial drain"
                    ),
                )
            raise
        except BaseException as exc:
            # CancelledError (also BaseException) must finalize too; re-raise bare.
            # A FRONTIER_API stream that fails mid-drain gets the SAME
            # class-name-only treatment as an establish-phase provider failure
            # (design §10.3 rule 5): provider exception text raised while
            # iterating the stream can itself carry prompt/response
            # substrings that URL-redaction alone does not strip. Only the
            # PRIVATE_VLLM lane keeps the (URL-redacted) original text.
            detail_text = (
                type(exc).__name__
                if model.lane == Lane.FRONTIER_API
                else f"{type(exc).__name__}: {redact_url_for_logs(str(exc))}"
            )
            with contextlib.suppress(BaseException):
                await self._finalize_stream(
                    model=model,
                    request=request,
                    passed_over=passed_over,
                    kpi_tag=kpi_tag,
                    last_chunk=last_chunk,
                    started=started,
                    ttft_ms=ttft_ms,
                    trace_id=trace_id,
                    outcome=AuditOutcome.error,
                    observe_model=isinstance(exc, Exception),
                    detail=(
                        f"task={request.task} lane={model.lane.value} model={model.name} "
                        f"mid-stream failure: {detail_text}"
                    ),
                )
            raise
        else:
            await self._finalize_stream(
                model=model,
                request=request,
                passed_over=passed_over,
                kpi_tag=kpi_tag,
                last_chunk=last_chunk,
                started=started,
                ttft_ms=ttft_ms,
                trace_id=trace_id,
                outcome=AuditOutcome.success,
                observe_model=True,
                finish_reason=stream_finish_reason,
                detail=f"task={request.task} lane={model.lane.value} model={model.name}",
            )

    async def _finalize_stream(
        self,
        *,
        model: ModelDef,
        request: CompletionRequest,
        passed_over: Sequence[str],
        kpi_tag: str | None,
        last_chunk: Any,
        started: float,
        ttft_ms: int | None,
        trace_id: str | None,
        outcome: AuditOutcome,
        observe_model: bool,
        detail: str,
        finish_reason: str | None = None,
    ) -> None:
        """Emit the terminal CostEvent + AuditEvent for a stream, whatever the
        outcome. The CostEvent is emitted FIRST and synchronously so it always
        lands even if the audit await is cancelled mid-cancellation; token/cost
        fields are None-guarded off the last seen chunk (litellm mock streams do
        not surface usage, so they are all None unless usage was attached).

        B12 latency: a stream's terminal ``latency_ms`` is measured HERE — one
        monotonic reading against the SAME ``started`` origin the establish
        phase used — because a stream's duration is not knowable until it
        terminates. It is the full establish→termination delta on ALL THREE
        termination paths (clean exhaustion, mid-drain failure, consumer
        abandon): an abandoned stream still consumed tokens and time, and its
        record says so. The establish-phase span's ``ne.gateway.latency_ms``
        attribute remains the narrower establish-only delta — a different fact
        about a different span, not a second measurement of this one.
        ``ttft_ms`` was measured in the drain loop (first chunk received);
        ``None`` means no chunk ever arrived and the key/slot stays absent.

        NE-FRONTIER-OUTPUT ``finish_reason``: the reason the provider gave for
        stopping, captured in the drain loop. It is recorded ONLY when it says
        the stream was cut off at the output budget (``"length"``) — a stream
        that ended normally, or a provider that reported nothing, produces the
        byte-identical row it produced before this parameter existed. The
        truncated row says so in BOTH its detail (readable) and its provenance
        (queryable), because a consumer that has already been handed every token
        has no other way to learn the answer stopped early.
        """
        truncated = finish_reason == "length"
        if truncated:
            detail += (
                " TRUNCATED: the model stopped at the output budget "
                "(finish_reason=length); the streamed answer is PARTIAL"
            )
        latency_ms = round((time.monotonic() - started) * 1000)
        pt: int | None = None
        ct: int | None = None
        cost: float | None = None
        if last_chunk is not None and getattr(last_chunk, "usage", None) is not None:
            pt, ct = extract_usage(last_chunk)
            cost = extract_cost(last_chunk)
        response_model = getattr(last_chunk, "model", None) if last_chunk is not None else None
        self._cost_sink.emit(
            new_cost_event(
                model=model.litellm_model,
                provider=model.provider,
                lane=model.lane.value,
                actor=self._cost_actor(request),
                input_tokens=pt,
                output_tokens=ct,
                cost_usd=cost,
                task=request.task,
                latency_ms=latency_ms,
                ttft_ms=ttft_ms,
                kpi_tag=kpi_tag,
                trace_id=trace_id,
            )
        )
        if self._audit_sink is not None:
            await self._audit_sink.append(
                new_audit_event(
                    action="ne.gateway.stream",
                    resource=model.litellm_model,
                    outcome=outcome,
                    actor=self._actor,
                    detail=detail,
                    kpi_tag=kpi_tag,
                    trace_id=trace_id,
                    provenance={
                        "task": request.task,
                        "lane": model.lane.value,
                        "model_name": model.name,
                        **_requested_model_provenance(request),
                        # Present IFF the stream was cut off at the output
                        # budget: a whole stream records nothing new here.
                        **({"finish_reason": "length"} if truncated else {}),
                        # The candidates passed over before this stream was
                        # established (frozen at the winning attempt) — the
                        # same placement on all three termination paths.
                        **_routing_provenance(passed_over),
                        **_usage_provenance(
                            pt,
                            ct,
                            cost,
                            response_model,
                            latency_ms=latency_ms,
                            ttft_ms=ttft_ms,
                            principal=_request_principal(request),
                        ),
                    },
                )
            )
        if observe_model:
            await self._record_model_observation(
                model=model,
                request=request,
                operation="stream",
                outcome=outcome,
                trace_id=trace_id,
            )
