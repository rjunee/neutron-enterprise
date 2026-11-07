# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""GatewayModel — the C3-clean pydantic-ai Model that routes via the ModelGateway.

This adapter is the ONLY model path in ne-runtime. It implements the pydantic-ai
``Model`` surface over the frozen ``ne.contracts.models.ModelGateway`` Protocol —
so ne-runtime imports NO provider SDK and does not depend on ne-gateway (C3; the
import-linter ``c3-provider-sdk-chokepoint`` contract stays KEPT, and the C6
guardian test source-scans this file for ``pydantic_ai.models.<provider>``
imports as a belt over the braces).

Prompted tool-calling (decision D3): ``ne.contracts.models.Completion`` has no
``tool_calls`` field, so tool-calling is done ENTIRELY in-band. When the agent
passes ``function_tools``, we append ONE system message: the tool catalog plus a
strict instruction to answer with EXACTLY one line of JSON
``{"tool_call": {"name": "<tool>", "arguments": {...}}}`` to call a tool, or plain
text otherwise. The reply is parsed: a well-formed single-object tool_call becomes
a :class:`ToolCallPart`; ANYTHING else (json error, wrong shape, extra keys,
non-dict arguments, prose) becomes a final-answer :class:`TextPart`. Parsing NEVER
raises. A well-formed tool call whose name is UNREGISTERED (not in the request's
function/output tool set) is routed through the REQUIRED ``on_unknown_tool`` hook
BEFORE the :class:`ModelResponse` is returned — so governance (the ``tool.unknown``
AuditEvent + an audited guardrail verdict + an audited PDP DENY, via
:class:`~ne.runtime.middleware.GovernedToolExecutor`) fires before pydantic-ai's
``ToolManager`` can reject the call with its stock "Unknown tool name" retry.
After the hook returns, the ``ToolCallPart`` is still returned unchanged so the
library issues that retry prompt and the loop can recover to a final answer.
``tools=`` / ``tool_choice=`` are NEVER placed in ``CompletionRequest.params`` (D3).

Instructions forwarding (NE-CTXB-0): ``ModelRequest.instructions`` (the skills
index) is derived via pydantic-ai's own ``Model._get_instruction_parts`` — using
``model_request_parameters.instruction_parts`` for the normal agent flow, falling
back to the message-history scan for direct ``model.request()`` callers — then
joined with :meth:`InstructionPart.join` and prepended as ONE system
:class:`~ne.contracts.types.ChatMessage` at the head of ``_map_history``'s output.
Exact behavior (index, empty-string handling, per-request dedup) is pinned by
``tests/test_agent_session_instructions.py``, not by this prose.

Message-shape contract (C3, NE-AGENT-MSG-ORDER): the outgoing ``CompletionRequest.
messages`` sequence carries AT MOST ONE system-role message, and when present it
is ALWAYS at index 0. Model independence is not luck: a strict chat template
(e.g. Qwen3.6-35B-A3B-FP8's) HTTP-400s on any system message that is not first,
so an agent whose shape only happens to work on a permissive template is
portable by accident. Every system-bearing source this adapter produces —
mapped ``instructions``, mid-history ``SystemPromptPart``s, and the in-band D3
tool catalog — is folded into ONE leading system message by
:func:`_merge_system_head`, an order-stable, lossless ``"\n\n"``-joined merge
(nothing is dropped; the merge is provably lossless and order-stable, not just
plausibly so). Both :meth:`GatewayModel.request` and the context-budget
:meth:`GatewayModel._trim` estimator route through the single :meth:`GatewayModel
._assemble` helper so the trim estimate stays byte-identical to what is
actually sent. This is a provider-agnostic boundary normalization — NO
per-model/per-provider conditional exists anywhere in this file. Exact
behavior (count, position, losslessness, order) is pinned by
``tests/test_message_order_contract.py``, not by this prose (written-rules
discipline: normative claims trace to tests).

Protected source delivery (PRE-015A0-ENFORCEMENT, SPEC §13): when constructed with a
``source_guard`` (``None`` ⇒ BYTE-IDENTICAL legacy ``request``), every history part
holding an opaque protected-source reference is materialized through the injected
:class:`~ne.runtime.source_results.ProtectedSourceResultGuard` twice — once before the
trim/assembly so the estimate covers the bytes actually sent, and once immediately
before ``_gateway.complete`` so drift between the two passes substitutes a fixed safe
withheld string instead of delivering. The substitution is ephemeral and reaches only
the outbound ``CompletionRequest``; the caller's ``messages`` list keeps the opaque
reference, so every later turn reauthorizes it under that turn's own verified
authorization. Exact behavior is pinned by ``tests/test_model_adapter.py``, not by
this prose.

Context-budget trim guard (NE-CTXB-3, design §3.4/§3.5): when constructed with a
``budget`` (the platform ALWAYS passes it; ``None`` ⇒ BYTE-IDENTICAL legacy
``request``), ``request`` runs a pairing-safe whole-turn trim over ``messages``
BEFORE ``_map_history``. The PROTECTED HEAD (first request's ``SystemPromptPart``s
+ the mapped ``instructions`` message) and the PROTECTED SUFFIX (everything from
the LAST ``UserPromptPart``-bearing ``ModelRequest`` onward — the current turn's
prompt and ALL of this run's tool-call/return/retry exchange) are never dropped;
the DROPPABLE MIDDLE is prior history grouped into WHOLE TURNS, oldest dropped
first until the lockstep-estimated assembled size (mapped head + kept + suffix +
the in-band tool catalog) fits ``budget.input_budget``. If head+suffix alone
overflow, a typed, audited :class:`ContextBudgetExceededError` (``stage=
"runtime-trim"``) is raised — a governed refusal, NEVER a silent truncation of
current-turn tool output. A counts-only ``context.budget`` audit event fires on
EVERY drop and EVERY raise (C10/C12). The estimator params, overhead margin, and
effective ``max_tokens`` all derive from the ONE ``GatewaySettings`` (threaded via
the platform ``model_copy``) so the runtime estimate is byte-identical to the
gateway pre-flight's (lockstep). The EXACT partition/drop/raise semantics live in
``tests/test_context_budget_trim.py`` + ``_property`` + ``_agent_integration``, not
in this prose (written-rules discipline: normative claims trace to tests).

NE-CTX-IN-FORCE: the budget is a property of the model ACTUALLY IN FORCE for the
turn, re-derived on EVERY request, not of whatever model the composition root's
default task routed to at boot. ``budget=`` is the composed DEFAULT and the
on/off switch; when the injected gateway also satisfies
:class:`~ne.contracts.models.TurnContextBudget` (the real ``LiteLLMGateway``
does), each request asks it for the budget of the model it is about to route to
— the governed per-run selection if one is pinned, else the task's primary under
the CURRENT route table — and trims to that. A gateway without the capability (a
test double, a wrapping decorator) leaves the composed default in force, which is
the previous behaviour exactly. This is what lets every lane declare its own true
serving window: before it, one deployment-wide budget meant a 1M-window frontier
model had to be declared at the smallest lane's window or the trim guard would
size a prompt the model in force could not hold.
"""

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from pydantic_ai.messages import (
    InstructionPart,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.usage import RequestUsage

from ne.contracts.errors import ContextBudgetExceededError
from ne.contracts.frozen import FrozenDict
from ne.contracts.governance import AuditSink
from ne.contracts.models import CompletionRequest, ModelGateway, TurnContextBudget
from ne.contracts.runtime import ToolSpec
from ne.contracts.tokens import ByteTokenEstimator, ContextBudget
from ne.contracts.types import ChatMessage, Lane
from ne.events import new_audit_event
from ne.runtime.config import RuntimeSettings
from ne.runtime.correlation import (
    current_run_principal_id,
    current_run_requested_model,
    current_run_requested_model_lane,
    current_run_trace_id,
)
from ne.runtime.errors import RuntimeAuditError
from ne.runtime.source_results import HistoryDelivery, ProtectedSourceResultGuard
from ne.schema import AuditOutcome

# The pydantic-ai ``ModelSettings`` keys this adapter HONORS. Everything else in
# the (16-key on pydantic-ai 2.5.0) ``ModelSettings`` TypedDict is EXPLICITLY
# ignored — a control knob that does not occupy prompt context or that the
# in-band D3 protocol cannot express. This frozenset is the single source of the
# supported set; ``tests/test_model_settings_guardian.py`` pins BOTH the full
# annotation list (a NEW pydantic-ai key must be a LOUD supported-vs-ignored
# decision, not a silent drop — the same class as the instructions bug) AND that
# every non-supported key leaves ``params`` byte-identical to the legacy path.
SUPPORTED_MODEL_SETTINGS_KEYS: frozenset[str] = frozenset({"max_tokens"})

# The join text used to merge multiple system-role message contents into ONE
# leading system message (see :func:`_merge_system_head`). A blank line keeps
# each source's text visually distinct without inventing any new punctuation
# or markup that could confuse a downstream parser.
_SYSTEM_JOINER = "\n\n"

# The in-band D3 CALL protocol. Says how to call a tool; says NOTHING about how a
# final answer is transported, because that is not a property of this adapter — it
# is a property of the Agent's declared ``output_type``, which the adapter does not
# own. Kept separate from the final-answer clause below so a caller whose
# output_type is NOT plain text cannot be handed a catalog that contradicts its own
# output contract (see _PLAIN_TEXT_FINAL_ANSWER).
_TOOL_CALL_PROTOCOL = (
    "You may call at most one tool per turn. To call a tool, respond with EXACTLY "
    "one line of JSON and NOTHING else: "
    '{"tool_call": {"name": "<tool-name>", "arguments": {<json-args>}}}. '
    'BOTH keys are REQUIRED: "name" (one of the tool names listed above, spelled '
    'exactly) and "arguments" (a JSON object). A tool_call without a "name" key is '
    "invalid and will be rejected. "
)

#: The DEFAULT final-answer clause: correct for every ``output_type=str`` loop
#: (the department session, sub-agents, cron jobs), where a final answer really is
#: plain text. It is WRONG for a structured-output loop, which is why it is a
#: separate, overridable string rather than being fused into the call protocol —
#: see :meth:`GatewayModel.final_answer_instruction`.
_PLAIN_TEXT_FINAL_ANSWER = "Otherwise, respond with your final answer as plain text (no JSON)."

#: Byte-identical to the pre-split constant (the two halves concatenate exactly).
#: Retained as the name the plain-text loops and their tests refer to.
_TOOL_INSTRUCTION = _TOOL_CALL_PROTOCOL + _PLAIN_TEXT_FINAL_ANSWER

# A markdown code fence wrapping the ENTIRE response text, optionally tagged
# (```json ... ```). Observed live 2026-08-04 on the GPU demo lane: Qwen3.6-35B
# follows _TOOL_INSTRUCTION's protocol but emits the tool_call JSON inside a
# ```json fence — a bare json.loads then fails, the call is silently demoted to
# prose, and a grounded turn that WANTED to fetch never fetches (the gate then
# correctly withholds the draft — honest, but the fetch was lost). The fence is
# unwrapped ONLY when it spans the whole stripped text (one block, nothing
# outside it); prose containing a fence, or a fence followed by prose, is left
# untouched and stays a final answer. The unwrapped candidate still has to pass
# the exact same strict {"tool_call": {...}} shape checks below — this widens
# accepted TRANSPORT, never the accepted call shape.
_FENCED_BLOCK_RE = re.compile(r"\A```[A-Za-z0-9_-]*[ \t]*\r?\n(.*?)\r?\n?```\Z", re.DOTALL)

# A per-run hook invoked with (tool_name, args) whenever ``request`` parses a
# well-formed tool call whose name is not in the request's tool set. It routes the
# call through the governed executor (audit + guardrails + PDP) BEFORE pydantic-ai
# sees it; see :class:`~ne.runtime.agent.DepartmentAgentSession`.
UnknownToolHook = Callable[[str, Mapping[str, Any]], Awaitable[None]]


class GatewayModel(Model):
    """A pydantic-ai ``Model`` whose every completion routes through the ModelGateway."""

    def __init__(
        self,
        gateway: ModelGateway,
        settings: RuntimeSettings,
        *,
        on_unknown_tool: UnknownToolHook,
        budget: ContextBudget | None = None,
        sink: AuditSink | None = None,
        source_guard: ProtectedSourceResultGuard | None = None,
        preserve_history: bool = False,
    ) -> None:
        # ``on_unknown_tool`` is REQUIRED and keyword-only (no default, no None): a
        # hook-less GatewayModel would let a model-emitted unregistered tool call
        # slip past governance, so it is structurally unconstructable (C6).
        super().__init__()
        # C10 (mandatory audit): a trim guard that cannot audit is structurally
        # unconstructable — a budget WITHOUT a sink would drop turns / refuse
        # unaudited, exactly the silent-downgrade this unit exists to prevent.
        # Mirrors the ``on_unknown_tool`` REQUIRED-hook pattern above.
        if budget is not None and sink is None:
            raise ValueError(
                "GatewayModel(budget=...) requires a sink: a trimming model that "
                "cannot emit the mandatory context.budget audit event is forbidden (C10)"
            )
        self._gateway = gateway
        self._rt = settings
        self._on_unknown_tool = on_unknown_tool
        self._budget = budget
        self._sink = sink
        # PRE-015A0-ENFORCEMENT: None => byte-identical legacy request() (no
        # protected-reference resolution, no second assembly). The platform passes
        # the guard wherever a session may carry source-bearing results.
        self._source_guard = source_guard
        self._preserve_history = preserve_history
        # budget=None ⇒ byte-identical legacy request(): no estimator, no trim.
        self._estimator: ByteTokenEstimator | None = (
            ByteTokenEstimator(settings.bytes_per_token, settings.per_call_overhead)
            if budget is not None
            else None
        )

    @property
    def model_name(self) -> str:
        return f"ne-gateway:{self._rt.task}"

    @property
    def system(self) -> str:
        return "ne-gateway"

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        # model_settings.max_tokens is honored UNCONDITIONALLY (budget or not): it
        # goes into the outbound params AND (below) into the effective-reserve
        # arithmetic. Every OTHER ModelSettings key is explicitly ignored
        # (SUPPORTED_MODEL_SETTINGS_KEYS). No caller supplies one today, so the
        # legacy path stays byte-identical (params == FrozenDict()); a future one
        # is the R5-P1-7 silent-drop fix, not a surprise.
        params = _params_from_settings(model_settings)
        # C8 KPI ATTRIBUTION — stamp the DECLARED business outcome so the gateway
        # lifts it onto this call's CostEvent (and its sibling AuditEvent). The
        # value comes from RuntimeSettings, which for a dispatched sub-agent is
        # the per-child model_copy carrying its AgentDefinition's tag: declared
        # configuration, never anything the model asked for. Unset ⇒ the key is
        # ABSENT (not an empty string, not a placeholder), so the call is emitted
        # honestly untagged and reports as unattributed. Absent also keeps every
        # untagged caller's params byte-identical to the legacy value.
        params = _with_kpi_tag(params, self._rt.kpi_tag)

        # PRE-015A0-ENFORCEMENT (SPEC §13): materialize every protected source
        # reference BEFORE trimming/assembly, so the trim estimate is taken over the
        # bytes actually sent, and AGAIN immediately before ``_gateway.complete`` so
        # authority drift between the two passes withholds instead of delivering.
        # Substitution is EPHEMERAL: it lands only in the outbound
        # ``CompletionRequest``; the caller's ``messages`` (durable Pydantic history)
        # keeps the opaque reference, so every later request reauthorizes it.
        # ``source_guard=None`` ⇒ both passes are skipped (legacy, byte-identical).
        delivery: HistoryDelivery | None = None
        if self._source_guard is not None:
            delivery = await self._source_guard.materialize_history(messages)
            messages = list(delivery.messages)

        # Context-budget trim (design §3.5): reduce ``messages`` to a candidate that
        # fits the primary model's input budget, or raise the typed/audited refusal.
        # budget=None ⇒ untouched (legacy).
        if self._budget is not None:
            messages = await self._trim(
                messages,
                model_settings,
                model_request_parameters,
                await self._turn_budget(),
            )

        chat = self._assemble(messages, model_request_parameters)

        if self._source_guard is not None and delivery is not None:
            delivery = await self._source_guard.reauthorize_history(messages, delivery)
            chat = self._assemble(list(delivery.messages), model_request_parameters)

        lane = self._effective_lane()
        # NE-COST-PRINCIPAL: current_run_principal_id() is None outside any
        # run_principal_scope (no run root entered one), so a caller predating
        # this unit sees a byte-identical CompletionRequest — pinned by
        # test_correlation.py's ambient-None GatewayModel test.
        completion = await self._gateway.complete(
            CompletionRequest(
                messages=chat,
                task=self._rt.task,
                lane=lane,
                params=params,
                trace_id=current_run_trace_id(),
                principal_id=current_run_principal_id(),
                requested_model=current_run_requested_model(),
            )
        )

        part = self._parse(completion.text, tuple(model_request_parameters.function_tools))
        # NE-OUTPUT-TRANSPORT. A run whose Agent declared a structured
        # ``output_object`` transports its FINAL ANSWER as one JSON object; a
        # ``TextPart`` here is that answer (a tool call already left as a
        # ``ToolCallPart``). If those bytes are not JSON but contain exactly one
        # parseable top-level object, the model produced the object and wrapped
        # it — in a prose preamble, an indented fence, a trailing sign-off — and
        # the wrapper alone is what pydantic-ai's ``validate_json`` will choke
        # on, reporting ``json_invalid`` against the WHOLE reply. Unwrapping it
        # here widens accepted TRANSPORT only: the extracted bytes still face the
        # same schema, the same ``extra="forbid"``, and the same claim binding,
        # so a malformed, truncated, or contract-violating reply still fails and
        # still names what was wrong. See ``sole_json_object`` for why ambiguity
        # (more than one candidate object) deliberately fails closed instead, and
        # ``sole_answer_object`` for why a lone tool-call object is not an answer.
        #
        # ``output_object is None`` — every ``output_type=str`` loop — leaves this
        # byte-identical to the legacy path.
        if model_request_parameters.output_object is not None and isinstance(part, TextPart):
            # NE-CALL-SELECT (593ef6d5) briefly promoted a reply here whose sole
            # object was a well-formed tool call into a governed invocation, on
            # positional gates (object ends the reply; one emission). REVERTED
            # by arbiter ruling 2026-08-29: the attacker authors the retrieved
            # text the model quotes, so the legitimate wrapped call and the
            # refusal-quoting-an-injected-payload occupy the SAME set of texts
            # (prose + one well-formed call object) — every text-only rule
            # (position at either end, emission count, fencing) was defeated by
            # construction. The only separating rule is "the object IS the
            # whole reply", which is _parse's existing transport, so a
            # prose-wrapped call stays TEXT: refused as an answer just below
            # (a lone tool_call object never satisfies ``extra="forbid"``) and
            # sent back as a recoverable answer-shape retry whose correction
            # (NE-RETRY-LEGIBLE, ``_render_retry_prompt`` +
            # ``final_answer_retry_instruction``) restates the one-object
            # transport — not the dead turn the pre-NE-RETRY-LEGIBLE budget
            # notes described. Answer NORMALISATION below is kept: its worst
            # case is a rejected answer, never an invocation.
            normalized = sole_answer_object(part.content)
            if normalized is not None:
                part = TextPart(content=normalized)
        if isinstance(part, ToolCallPart):
            known = {t.name for t in model_request_parameters.function_tools} | {
                t.name for t in model_request_parameters.output_tools
            }
            if part.tool_name not in known:
                # Governance fires HERE, before pydantic-ai's ToolManager can reject
                # the unregistered name. Hook exceptions (e.g. RuntimeAuditError from
                # an un-recordable call) propagate and kill the request (fail closed).
                await self._on_unknown_tool(
                    part.tool_name, part.args if isinstance(part.args, Mapping) else {}
                )
        usage = RequestUsage(
            input_tokens=completion.usage.prompt_tokens,
            output_tokens=completion.usage.completion_tokens,
        )
        # NE-FRONTIER-OUTPUT: a completion the model stopped early, at the
        # output budget, is a PARTIAL answer, and until this it reached the run
        # indistinguishable from a whole one. pydantic-ai already owns a slot
        # for the fact — ``ModelResponse.finish_reason`` — which lands in the
        # run's own message history and on its span, so the run record itself
        # states that this response was cut off.
        #
        # ONLY ``"length"`` is forwarded, and deliberately: that slot is a
        # pydantic-ai ``FinishReason`` Literal on ITS spellings, not the
        # provider's (litellm hands back OpenAI's ``"tool_calls"``, the Literal
        # wants ``"tool_call"``), so passing raw provider tokens through would
        # be inventing a mapping. ``Completion.truncated`` is the one reading
        # this codebase makes of that field, and it is the one that changes what
        # the answer MEANS. A non-truncated response is constructed exactly as
        # before — ``finish_reason`` stays ``None``.
        if completion.truncated:
            return ModelResponse(
                parts=[part], usage=usage, model_name=self.model_name, finish_reason="length"
            )
        return ModelResponse(parts=[part], usage=usage, model_name=self.model_name)

    # -- context-budget trim (design §3.5) ----------------------------------

    def _effective_lane(self) -> Lane | None:
        """The lane pin this request goes out under — ONE rule, two readers.

        Factored out so :meth:`_turn_budget` asks the gateway to budget the
        model under exactly the pin the ``CompletionRequest`` below will carry.
        Round 3 of the resolver-vs-attempt divergence: with the lane pinned and
        no per-run selection, the gateway's candidate rule can filter the route
        PRIMARY out entirely, so a budget resolved without the pin is for a
        model that will never be called.
        """
        requested_lane = current_run_requested_model_lane()
        return (
            requested_lane
            if requested_lane is not None
            else Lane(self._rt.lane)
            if self._rt.lane
            else None
        )

    async def _turn_budget(self) -> ContextBudget:
        """The budget of the model IN FORCE for THIS request (NE-CTX-IN-FORCE).

        Asked of the gateway on every request, because both inputs are live: a
        governed per-run selection sets ``current_run_requested_model()`` for the
        duration of one run, and a governed repoint changes the task's primary
        for every run after it. Neither is visible to ne-runtime, and neither
        moves the composed ``RuntimeSettings``.

        Falls back to the composed boot budget — never to "no budget" — when the
        gateway does not implement the capability or cannot name the model. The
        guard therefore stays ARMED in every configuration it was armed in
        before; the only thing that varies is which window it is armed at.

        ⚠️ THAT FALLBACK IS NOT A SAFE DEFAULT ANY MORE, and the isinstance test
        below is why every wrapping gateway must forward the capability. On a
        deployment whose composition-root task routes to the widest lane (the
        demo emitter routes ``agent`` at ``frontier-default``), the composed boot
        budget is the deployment's WIDEST, not its narrowest — so a stack that
        fails this test measures every turn against a window the serving model
        may not have. ``TurnContextBudget`` is method-presence, so ONE wrapper
        without the method silences the whole mechanism.
        """
        assert self._budget is not None
        if not isinstance(self._gateway, TurnContextBudget):
            return self._budget
        resolved = await self._gateway.context_budget_for_turn(
            self._rt.task,
            requested_model=current_run_requested_model(),
            lane=self._effective_lane(),
        )
        return resolved.budget if resolved is not None else self._budget

    async def _trim(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        budget: ContextBudget,
    ) -> list[ModelMessage]:
        """Drop OLDEST whole turns until the assembled request fits the input budget.

        Returns the trimmed ``messages`` (possibly unchanged); raises a typed,
        audited :class:`ContextBudgetExceededError` if head+suffix alone overflow.
        Only reached when ``self._budget`` is set, so the estimator + sink exist.

        ``budget`` is the IN-FORCE model's (see :meth:`_turn_budget`), not
        necessarily the composed default: every window/reserve/margin below comes
        from it, so the refusal a caller sees names the window that actually
        constrained the turn.
        """
        assert self._budget is not None  # constructor invariant when trimming
        assert self._estimator is not None
        assert self._sink is not None

        # Effective output reserve — the SAME arithmetic as the gateway pre-flight
        # (gateway.py:390-395): an explicit ``model_settings.max_tokens`` (also put
        # into params by _params_from_settings, so the gateway's setdefault won't
        # override it) else the budget's (== primary model's) max_output_tokens.
        # overhead_margin == overhead_margin_for(window) by construction of the
        # budget, so both layers subtract the identical margin.
        effective = self._effective_max_tokens(model_settings, budget)
        input_budget = max(0, budget.context_window - effective - budget.overhead_margin)

        head, groups, suffix = _partition(messages)

        estimated = 0
        for k in range(len(groups) + 1):
            candidate: list[ModelMessage] = [
                *head,
                *[m for g in groups[k:] for m in g],
                *suffix,
            ]
            chat = self._assemble(candidate, model_request_parameters)
            # Lockstep serialization (byte-identical to gateway.py:368 /
            # _estimate_prompt_tokens): the agent path never places prompt-bearing
            # params, so the gateway estimates these same messages only.
            estimated = self._estimator.estimate(
                json.dumps([m.model_dump() for m in chat], ensure_ascii=False, default=str)
            )
            if estimated <= input_budget:
                if k > 0:
                    await self._emit_budget(
                        estimated=estimated,
                        input_budget=input_budget,
                        dropped_turns=k,
                        outcome=AuditOutcome.success,
                        context_window=budget.context_window,
                    )
                return candidate
            if k == 0 and self._preserve_history:
                await self._emit_budget(
                    estimated=estimated,
                    input_budget=input_budget,
                    dropped_turns=0,
                    outcome=AuditOutcome.error,
                    context_window=budget.context_window,
                )
                raise ContextBudgetExceededError(
                    stage="runtime-trim",
                    estimated_tokens=estimated,
                    input_budget=input_budget,
                    context_window=budget.context_window,
                    dropped_turns=0,
                )

        # No drop level fits: head + suffix ALONE still overflow. The current turn's
        # tool returns are structurally un-trimmable (design §3.5-2) — a hard
        # governed refusal is honest (§3.5-5: NO fallback render in v1).
        await self._emit_budget(
            estimated=estimated,
            input_budget=input_budget,
            dropped_turns=len(groups),
            outcome=AuditOutcome.error,
            context_window=budget.context_window,
        )
        raise ContextBudgetExceededError(
            stage="runtime-trim",
            estimated_tokens=estimated,
            input_budget=input_budget,
            context_window=budget.context_window,
            dropped_turns=len(groups),
        )

    def _effective_max_tokens(
        self, model_settings: ModelSettings | None, budget: ContextBudget
    ) -> int:
        if model_settings is not None:
            explicit = model_settings.get("max_tokens")
            if explicit is not None:
                return int(explicit)
        return budget.max_output_tokens

    async def _emit_budget(
        self,
        *,
        estimated: int,
        input_budget: int,
        dropped_turns: int,
        outcome: AuditOutcome,
        context_window: int,
    ) -> None:
        """Emit the MANDATORY counts-only ``context.budget`` audit event (C10/C12).

        COUNTS ONLY — the detail carries the five numeric/enum fields and NEVER any
        message or tool text (pinned by the sentinel scan in
        ``tests/test_context_budget_audit.py``). Fails CLOSED: a sink-append failure
        raises :class:`RuntimeAuditError` (chained) so an un-recordable trim/refusal
        stops the run rather than proceeding unaudited — mirrors
        ``agent._emit_session_run`` and ``middleware._emit``.
        """
        assert self._sink is not None
        event = new_audit_event(
            action="context.budget",
            resource=self.model_name,
            outcome=outcome,
            actor="ne-runtime",
            detail=json.dumps(
                {
                    "estimated_tokens": estimated,
                    "input_budget": input_budget,
                    "context_window": context_window,
                    "dropped_turns": dropped_turns,
                    "stage": "runtime-trim",
                }
            ),
            trace_id=current_run_trace_id(),
        )
        try:
            await self._sink.append(event)
        except Exception as exc:
            raise RuntimeAuditError(
                "audit append failed for context.budget; result withheld (C10 fail closed)"
            ) from exc

    # -- assembly (C3 message-shape contract) --------------------------------

    def _assemble(
        self,
        messages: Sequence[ModelMessage],
        model_request_parameters: ModelRequestParameters,
    ) -> tuple[ChatMessage, ...]:
        """The ONE assembly path shared by :meth:`request` and :meth:`_trim`.

        Maps history (instructions become a leading system message, per
        :meth:`_map_history`), appends the in-band D3 tool catalog as a system
        message when ``function_tools`` is non-empty, then folds every
        system-role message into ONE leading system message via
        :func:`_merge_system_head` — the C3 message-shape contract (at most one
        system message, always at index 0), enforced provider-agnostically for
        every caller. Using this SAME helper in ``_trim`` keeps the context-
        budget estimate byte-identical to what ``request`` actually sends
        (module docstring's lockstep invariant).
        """
        chat = self._map_history(messages, model_request_parameters)
        tools = tuple(model_request_parameters.function_tools)
        if tools:
            chat = (*chat, ChatMessage(role="system", content=self._render_catalog(tools)))
        return _merge_system_head(chat)

    # -- history mapping ----------------------------------------------------

    def _map_history(
        self,
        messages: Sequence[ModelMessage],
        model_request_parameters: ModelRequestParameters,
    ) -> tuple[ChatMessage, ...]:
        chat: list[ChatMessage] = []
        parts = self._get_instruction_parts(messages, model_request_parameters)
        instructions = InstructionPart.join(parts) if parts else None
        if instructions is not None:
            chat.append(ChatMessage(role="system", content=instructions))
        for message in messages:
            if isinstance(message, ModelRequest):
                chat.extend(self._map_request_parts(message))
            elif isinstance(message, ModelResponse):
                chat.extend(self._map_response_parts(message))
        return tuple(chat)

    def _map_request_parts(self, message: ModelRequest) -> list[ChatMessage]:
        out: list[ChatMessage] = []
        for part in message.parts:
            if isinstance(part, SystemPromptPart):
                out.append(ChatMessage(role="system", content=part.content))
            elif isinstance(part, UserPromptPart):
                out.append(ChatMessage(role="user", content=_as_text(part.content)))
            elif isinstance(part, ToolReturnPart):
                out.append(
                    ChatMessage(
                        role="tool",
                        content=f"{part.tool_name} returned: {_as_text(part.content)}",
                    )
                )
            elif isinstance(part, RetryPromptPart):
                out.append(ChatMessage(role="user", content=self._render_retry_prompt(part)))
            else:  # unknown/native part: map conservatively rather than raise
                out.append(ChatMessage(role="user", content=_as_text(part)))
        return out

    def _map_response_parts(self, message: ModelResponse) -> list[ChatMessage]:
        out: list[ChatMessage] = []
        for part in message.parts:
            if isinstance(part, TextPart):
                out.append(ChatMessage(role="assistant", content=part.content))
            elif isinstance(part, ToolCallPart):
                # Re-render a prior tool call as its canonical JSON line (faithful history).
                out.append(
                    ChatMessage(
                        role="assistant", content=_render_tool_call(part.tool_name, part.args)
                    )
                )
            else:
                out.append(ChatMessage(role="assistant", content=_as_text(part)))
        return out

    # -- catalog + parsing --------------------------------------------------

    @property
    def final_answer_instruction(self) -> str:
        """The clause telling the model how a FINAL ANSWER is transported.

        Overridable because the answer transport belongs to the Agent's declared
        ``output_type``, not to this adapter. The base value is correct for every
        ``output_type=str`` loop; a structured-output subclass MUST override it,
        or the catalog it emits will instruct the model to answer in plain text
        while its own output schema requires a JSON object — a contradiction the
        model resolves by guessing, and whose losing guess is an unvalidatable
        final reply (see ``runner._StructuredAgenticGatewayModel``).
        """
        return _PLAIN_TEXT_FINAL_ANSWER

    @property
    def final_answer_retry_instruction(self) -> str:
        """What a rejected FINAL ANSWER'S retry should tell the model to DO.

        Empty here, deliberately: an ``output_type=str`` loop never rejects a
        final answer for shape, so the base adapter has no correction to add. A
        structured-output subclass overrides this with its transport's own
        restatement (see ``runner._StructuredAgenticGatewayModel``), and
        :meth:`_render_retry_prompt` appends it to every schema-rejection retry.
        """
        return ""

    @property
    def wrapped_tool_call_retry_instruction(self) -> str:
        """What a rejected PROSE-WRAPPED TOOL CALL's retry should tell the model.

        NE-WRAPPED-CALL. Defaults to :attr:`final_answer_retry_instruction`, so a
        loop that never distinguishes the two is unaffected. A loop that rejects
        final answers for shape MUST override it: for this reply the answer
        restatement is advice for the wrong mistake — it tells a model that tried
        to call a tool to re-send its "answer", i.e. to abandon the call (see
        :data:`PROSE_WRAPPED_TOOL_CALL_CORRECTION`).
        """
        return self.final_answer_retry_instruction

    def _render_retry_prompt(self, part: RetryPromptPart) -> str:
        """The model-facing text of one ``RetryPromptPart``.

        NE-RETRY-LEGIBLE — the S1 live failure's second half. This branch used to
        emit ``_as_text(part.content)``, and for a schema rejection ``content`` is
        pydantic's raw error-details list: the model received a bare user message
        reading ``[{"input":"<its own 2,655-char reply>","loc":[],"msg":"Invalid
        JSON...","type":"json_invalid"}]`` — its whole malformed answer echoed
        back inside an error object, with NO sentence saying what to do about it.
        Measured live (train-49, worker log): the model answered THAT user
        message with prose four consecutive times and the run died with the
        bounded retry exhausted. The "bounded retry" existed, but what it
        delivered was not a correction.

        ``part.model_response()`` is pydantic-ai's OWN rendering of the same
        part — "N validation errors: <list> Fix the errors and try again." — and
        for a final-answer rejection (``tool_name is None``) it deliberately
        EXCLUDES the ``input`` echo from every error whose ``loc`` is empty or one
        deep, so the retry request gets smaller as well as legible. It does NOT
        exclude a DEEPER error's ``input``, which is a verbatim slice of the same
        reply; :func:`_without_nested_rejection_inputs` drops those before the
        render. A string ``content`` (this loop's own server-authored
        ``ModelRetry`` corrections — residue, claims) is delivered verbatim as
        before: those sentences were written to be sent, and re-wrapping them
        would drift every budget note that measured them.

        WHAT THIS RENDER ACTUALLY PUBLISHES, field by field. ``model_response()``
        dumps each error entry through pydantic-core's ``ErrorDetails``, whose
        complete key set is ``type``, ``loc``, ``msg``, ``input``, ``ctx`` and
        ``url``. After pydantic-ai's own exclusions and this method's:

        ``type``   pydantic's error-code vocabulary. SERVER-OWNED.
        ``url``    ``https://errors.pydantic.dev/<ver>/v/<type>`` — a function of
                   ``type`` alone. SERVER-OWNED.
        ``msg``    pydantic's message for that code. SERVER-OWNED for every code
                   these two schemas can raise (``extra_forbidden`` says "Extra
                   inputs are not permitted" and names no key; ``json_invalid``
                   gives a line/column and no content). The codes whose ``msg``
                   CAN embed model bytes — ``value_error``/``assertion_error``
                   (a custom validator's exception text) and ``union_tag_invalid``
                   (the model's discriminator value) — need a custom validator or
                   a discriminated union, and neither
                   ``_StructuredAgenticCompletion`` nor its nested models has one.
                   Pinned by the marker sweep in ``test_output_transport.py``, so
                   adding one fails a test rather than shipping an echo.
        ``ctx``    constraint context, and for ``json_invalid`` the parse error.
                   MAY carry model bytes in general — and is NEVER RENDERED:
                   pydantic-ai excludes it on both branches. Pinned, because it
                   is an upstream behaviour C12 leans on.
        ``loc``    server-owned EXCEPT under ``extra_forbidden``, whose final
                   segment is the key the MODEL invented. MODEL-AUTHORED there,
                   and masked by :func:`_with_safe_error_locations` on BOTH
                   branches before the render.
        ``input``  the rejected bytes. MODEL-AUTHORED always. On the final-answer
                   branch it reaches the model from NO entry: pydantic-ai drops it
                   at depth <= 1 and :func:`_without_nested_rejection_inputs` drops
                   it deeper. On the TOOL-CALL branch pydantic-ai keeps every
                   ``input`` and this method does NOT strip it — the model's own
                   arguments are echoed back, deliberately, because they are what
                   the correction is about. That is a known, stated echo, not a
                   claim that nothing is echoed.

        For a structured loop, :attr:`final_answer_retry_instruction` is appended
        so the retry restates the ONE action that fixes the reply — emit the
        transport object, nothing outside it, escape line breaks — instead of
        leaving the model to infer it from a parser error naming a line number.
        """
        if isinstance(part.content, str):
            return part.content
        if part.tool_name is not None:
            # A TOOL ARGUMENT rejection. ``loc`` is masked here too: an undeclared
            # argument name lands in ``loc`` under ``extra_forbidden`` exactly as an
            # undeclared answer key does. ``input`` is NOT stripped on this branch —
            # see the field table above; the arguments are the subject of the
            # correction and are already in history as the assistant turn that sent
            # them (:meth:`_map_response_parts` re-renders the call).
            safe = _with_safe_error_locations(part.content)
            return dataclasses.replace(part, content=safe).model_response()
        # C12, STATED RATHER THAN ASSERTED — the field table in the docstring above
        # is the contract, and it is not "the rejected reply is never echoed".
        #
        # ``input``: ``model_response()`` excludes it only from the entries whose
        # ``loc`` is empty or one deep; a DEEPER ``loc`` keeps it, and that value is
        # a verbatim slice of the reply, so ``_without_nested_rejection_inputs``
        # drops those. Across this branch's whole list no ``input`` is rendered.
        #
        # ``loc``: server-owned EXCEPT under ``extra_forbidden``, where the final
        # segment is the key the model invented — which this render republished
        # VERBATIM until now, under ``role="user"``. ``_with_safe_error_locations``
        # masks it. It runs AFTER the two helpers below read the original entries:
        # both read ``len(loc)`` only, masking preserves depth exactly, and running
        # it last keeps that independence obvious rather than merely true.
        #
        # ``type``/``url``/``msg`` are rendered as pydantic wrote them, and ``ctx``
        # is never rendered at all. Why each of those is safe is in the table above.
        details = _without_nested_rejection_inputs(part.content)
        # NE-WRAPPED-CALL: which mistake was made decides which correction is sent,
        # read ONLY off the root entries (``loc == ()``), whose ``input`` the
        # stripper above deliberately leaves in place. A depth-1 entry is a
        # top-level FIELD's value and a deeper one a sub-value; neither is a reply
        # to classify. Both corrections are static server-authored text.
        correction = (
            self.wrapped_tool_call_retry_instruction
            if rejects_a_prose_wrapped_tool_call(details)
            else self.final_answer_retry_instruction
        )
        text = dataclasses.replace(
            part, content=_with_safe_error_locations(details)
        ).model_response()
        if correction:
            text = f"{text}\n\n{correction}"
        return text

    def _render_catalog(self, tools: Sequence[ToolDefinition]) -> str:
        return _catalog_text(
            [(t.name, t.description or "", t.parameters_json_schema) for t in tools],
            final_answer_instruction=self.final_answer_instruction,
        )

    def _parse(self, text: str, tools: Sequence[ToolDefinition]) -> TextPart | ToolCallPart:
        stripped = text.strip()
        # Unwrap a single all-spanning markdown fence (see _FENCED_BLOCK_RE):
        # models trained to fence JSON otherwise lose their tool call to prose.
        fenced = _FENCED_BLOCK_RE.match(stripped)
        if fenced is not None:
            stripped = fenced.group(1).strip()
        part = _tool_call_from_json_text(stripped)
        if part is None and "\n" in stripped:
            # First-line fallback (observed live 2026-08-04, GPU demo lane):
            # Qwen3.6-35B sometimes emits a well-formed tool_call line and then
            # KEEPS GENERATING — duplicating the call and fabricating the tool's
            # "response" JSON in the same completion. Under a whole-text-only
            # parse that entire blob (fabricated result included) was demoted to
            # prose and SHOWN TO THE USER as the final answer — strictly worse
            # than executing the call: the governed executor never ran, and the
            # model's invented data was emitted as if it were an answer. When
            # the response BEGINS with a well-formed tool_call line, honor that
            # line and discard the tail — the real tool runs through the full
            # governed path (PDP + audit + guardrails) and its REAL return is
            # what enters history. Prose that merely mentions tool_call JSON
            # later in the text still parses as a final text answer (the
            # first line is not a lone tool_call JSON object).
            part = _tool_call_from_json_text(stripped.splitlines()[0].strip())
        if part is not None:
            return part
        return TextPart(content=text)


#: The protocol shape's WRAPPER nesting depth —
#: ``{"tool_call": {"name": ..., "arguments": {...}}}`` opens exactly three
#: containers (root, the ``tool_call`` value, the ``arguments`` object).
#:
#: Be precise about what the THIRD brace closes, because it is not a wrapper:
#: it closes the ``arguments`` PAYLOAD object. Named residual, accepted: an
#: emission cut at a pair BOUNDARY completes to the pairs emitted so far, so a
#: later argument the model had not yet reached is silently absent
#: (``{"to": "x"`` -> ``{"to": "x"}``, losing an intended ``"dry_run": false``).
#: A cut MID-pair cannot be completed at all (``{"to": `` stays unparseable on
#: every variant), and a missing REQUIRED argument fails pydantic-ai schema
#: validation into a retry; only a dropped OPTIONAL argument survives, and the
#: call still passes the full PDP/tier/audit path. That is the accepted cost of
#: not losing a real fetch to prose. The ceiling is what stops it growing: a
#: candidate needing a FOURTH brace was truncated inside a NESTED argument
#: structure, where the omission is an unbounded subtree rather than one
#: trailing pair.
#:
#: Pinned by ``test_completion_stops_at_the_wrapper_depth_so_a_truncated_
#: nested_argument_stays_text``.
_MAX_WRAPPER_BRACE_COMPLETION = 3


def _brace_completion_variants(candidate: str) -> list[str]:
    """``candidate`` plus its bounded trailing-brace completions, longest last.

    THE single source of the accepted transport: both the tool-call decision
    (:func:`_tool_call_from_json_text`) and the residue predicate
    (:func:`is_malformed_tool_call_residue`) derive their variants here rather
    than restating the rule, so the two can never drift apart and the bound has
    exactly one place to be read, tested, or changed.

    Only ``}`` is ever appended, and only to a candidate that already starts
    with ``{``: completion can close containers the model opened, and can NEVER
    introduce a key, a name, or a value. Synthesizing an action or an argument
    out of a truncated emission is therefore impossible by construction, not by
    convention.
    """
    variants = [candidate]
    if candidate.startswith("{"):
        variants.extend(candidate + "}" * k for k in range(1, _MAX_WRAPPER_BRACE_COMPLETION + 1))
    return variants


def _tool_call_from_json_text(candidate: str) -> ToolCallPart | None:
    """``candidate`` as a lone in-band tool call, or ``None`` if it is not one.

    The single strict shape check behind every accepted tool-call transport
    (whole text, fence-unwrapped, first-line fallback): exactly
    ``{"tool_call": {"name": <nonempty str>, "arguments": <object>}}`` — the
    accepted CALL shape never widens, only the transport around it.

    Bounded trailing-brace completion (observed live 2026-08-04, GPU demo
    lane): Qwen3.6-35B reproducibly emits the tool_call line with the FINAL
    closing brace missing (2 of 3 probes on identical input — an EOS landing
    on a ``}}`` token where ``}}}`` was intended; the inner ``arguments``
    object is always complete). When ``candidate`` starts with ``{`` and does
    not parse, up to 3 closing braces (the wrapper nesting depth of the
    protocol shape) are appended one at a time; the first variant that parses
    must STILL pass the exact strict shape check below. This completes only
    unclosed WRAPPERS at end-of-text — a candidate whose content is actually
    truncated or malformed still fails ``json.loads`` (or the shape check)
    on every variant and stays ``None``. Final text answers are never brace-
    completed: this helper only ever feeds the tool-call decision, and a
    ``None`` verdict leaves the original text untouched.
    """
    for variant in _brace_completion_variants(candidate):
        try:
            obj = json.loads(variant)
        except (json.JSONDecodeError, ValueError):
            continue
        break
    else:
        return None
    if not isinstance(obj, dict) or set(obj) != {"tool_call"}:
        return None
    call = obj["tool_call"]
    if not isinstance(call, dict):
        return None
    name = call.get("name")
    arguments = call.get("arguments")
    if not isinstance(name, str) or not name or not isinstance(arguments, dict):
        return None
    # A well-formed tool_call — even an unknown tool name — becomes a ToolCallPart:
    # the governed executor is the authority and audits unknown tools.
    return ToolCallPart(tool_name=name, args=arguments)


#: The CALL-SHAPE half of the corrective feedback malformed residue earns, shared
#: by every loop because the accepted call shape is identical on every loop. The
#: predicate above and this correction are deliberately adjacent: one decision
#: boundary, one description of how to get back inside it.
#:
#: What each caller appends is NOT shared, and must not be. A session loop
#: (``output_type=str``) tells the model to answer in plain text; the structured
#: agentic loop REJECTS a plain-text final answer and must point at the JSON
#: transport instead. Appending the session's sentence there would instruct the
#: model to do the one thing that loop refuses.
PROTOCOL_RESIDUE_CALL_CORRECTION = (
    "Your reply was a tool_call JSON object but it was MALFORMED and has been "
    "rejected. To call a tool respond with EXACTLY one line of JSON: "
    '{"tool_call": {"name": "<tool-name>", "arguments": {<json-args>}}} — BOTH '
    'the "name" key (one of the listed tool names, spelled exactly) and the '
    '"arguments" key (a JSON object) are REQUIRED.'
)


#: NE-WRAPPED-CALL. The correction for a WELL-FORMED lone tool call that the
#: model wrapped in prose — the one transport mistake that fell through to the
#: wrong advice entirely.
#:
#: What the model did, measured live on the S5 governed-send run ``f9826d5f``
#: (2026-09-10, both owner-authorised attempts): it wrote a prose preamble and
#: THEN the tool_call object. None of :meth:`GatewayModel._parse`'s three
#: transports accept that — the object is not the whole reply, the reply is not
#: one spanning fence, and the first line is prose — so the call was demoted to
#: text, taken for the final reply, and rejected
#: (``opens=prose objects=1 sole=tool-call``).
#:
#: Why it needed its OWN correction. A rejected final answer earns the answer
#: transport's restatement, and for THIS reply that advice is aimed at the wrong
#: mistake: it tells a model that tried to CALL A TOOL to "send the same answer
#: again" as a final-answer object, i.e. to ABANDON the governed call the skill
#: requires. Four consecutive wrapped calls each earned that advice and the run
#: died with the answer-shape allowance spent (10 model requests, 4 of them
#: rejected, no cap hit). :data:`PROTOCOL_RESIDUE_CALL_CORRECTION` already does
#: exactly this job for a MALFORMED lone call; this is its well-formed twin.
#:
#: It is STATIC SERVER-AUTHORED TEXT. It is selected by INSPECTING the reply and
#: it publishes no byte of it — same C12 guarantee as
#: :func:`rejected_reply_shape` beside it. And it changes no transport: the
#: separating rule stays "the object IS the whole reply" (see
#: :func:`sole_answer_object` on the NE-CALL-SELECT revert). The wrapped
#: emission still executes NOTHING; the model is told how to re-send it.
PROSE_WRAPPED_TOOL_CALL_CORRECTION = (
    "Your reply contained a tool_call JSON object WRAPPED IN PROSE, so NO tool was "
    "called and the reply was rejected. A tool call must be the ENTIRE reply. Send "
    "the call again as EXACTLY one line of JSON: "
    '{"tool_call": {"name": "<tool-name>", "arguments": {<json-args>}}} — the first '
    "character of your reply must be { and the last must be }, with NOTHING before "
    "it and NOTHING after it: no preamble, no explanation, no markdown fence. "
    "Inside every JSON string write a line break as the two characters \\n. Do not "
    "rewrite the call as a final answer — if you still need that tool, call it in "
    "that form now."
)


def is_malformed_tool_call_residue(text: str) -> bool:
    """``True`` iff ``text`` is protocol residue: a LONE ``{"tool_call": ...}``
    JSON object that FAILS the strict call shape.

    Live-isolated 2026-08-04 on the GPU demo lane (hybrid thinking ON): after a
    tool return, Qwen3.6-35B intermittently emits the tool_call line WITHOUT its
    ``"name"`` key (observed: ``{"tool_call": {"arguments": {"query": "SLA"}}}``).
    Under parse-never-raises that residue was demoted to prose, SHOWN to the user
    as the final answer, and PERSISTED into session history — where it
    self-reinforced (every later turn in the poisoned session imitated it,
    observed 4/4 in the live console). This predicate is the session-level
    output validator's trigger (see ``agent._install_protocol_residue_validator``):
    residue becomes a corrective ``ModelRetry``, never a user-visible answer.

    Decision boundary (deliberately narrow — the accepted CALL shape never
    widens, and legitimate prose is never flagged): after the SAME transports
    ``_parse`` accepts (whole text, all-spanning fence unwrap, first-line
    fallback, bounded trailing-brace completion), a candidate is residue iff it
    json-parses to a dict whose key set is EXACTLY ``{"tool_call"}`` and the
    strict shape check REJECTS it. A well-formed call never reaches the output
    validator (it became a ``ToolCallPart``); prose that merely mentions
    tool_call JSON, a JSON array, or an object with extra keys is NOT residue
    and flows through unchanged.
    """
    stripped = text.strip()
    fenced = _FENCED_BLOCK_RE.match(stripped)
    if fenced is not None:
        stripped = fenced.group(1).strip()
    candidates = [stripped]
    if "\n" in stripped:
        candidates.append(stripped.splitlines()[0].strip())
    for candidate in candidates:
        for variant in _brace_completion_variants(candidate):
            try:
                obj = json.loads(variant)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(obj, dict) and set(obj) == {"tool_call"}:
                # A lone tool_call object: residue iff the strict shape says no.
                return _tool_call_from_json_text(variant) is None
            break  # parsed to something that is NOT a lone tool_call object
    return False


def _merge_system_head(chat: Sequence[ChatMessage]) -> tuple[ChatMessage, ...]:
    """Fold EVERY system-role message in ``chat`` into ONE leading system message.

    Order-stable and lossless (C3 message-shape contract): the contents of
    every ``role == 'system'`` message are collected in ENCOUNTER order and
    joined with :data:`_SYSTEM_JOINER` — nothing is dropped, nothing is
    reordered relative to the other system contents. The non-system messages
    keep their original relative order, unchanged, following the single
    merged system message. ``chat`` with no system messages is returned
    UNCHANGED (as a tuple) — the merge never invents a phantom empty system
    message on a system-free sequence.
    """
    system_contents = [m.content for m in chat if m.role == "system"]
    if not system_contents:
        return tuple(chat)
    merged = ChatMessage(role="system", content=_SYSTEM_JOINER.join(system_contents))
    rest = [m for m in chat if m.role != "system"]
    return (merged, *rest)


def _params_from_settings(model_settings: ModelSettings | None) -> FrozenDict[str, Any]:
    """The outbound ``CompletionRequest.params`` derived from ``model_settings``.

    Only ``max_tokens`` (the sole member of :data:`SUPPORTED_MODEL_SETTINGS_KEYS`)
    is forwarded; every other key is ignored. Absent/None ⇒ ``FrozenDict()`` — the
    BYTE-IDENTICAL legacy value (no caller supplies ``max_tokens`` today). Putting
    it in params is ALSO what keeps the gateway pre-flight in lockstep: the
    gateway's ``setdefault('max_tokens', model.max_output_tokens)`` will NOT
    override a caller value, so both layers use the same effective reserve.
    """
    if model_settings is not None:
        raw = model_settings.get("max_tokens")
        if raw is not None:
            return FrozenDict({"max_tokens": int(raw)})
    return FrozenDict()


def _with_kpi_tag(params: FrozenDict[str, Any], kpi_tag: str | None) -> FrozenDict[str, Any]:
    """Add the declared C8 business-outcome tag to the outbound params, or don't.

    ``kpi_tag`` is a RESERVED gateway request-param key: the gateway pops it off
    the merged params and stamps it onto the ``CostEvent``, never forwarding it to
    litellm (``ne.gateway.gateway.LiteLLMGateway._call_params``). Transporting it
    through ``params`` is therefore the sanctioned channel, not a hijacked one.

    ``None`` returns ``params`` UNCHANGED — no empty string, no placeholder tag.
    An untagged call must stay visibly untagged all the way to the report; a
    default here would fabricate attribution at the source, where it would be
    indistinguishable from a real declaration downstream.
    """
    if kpi_tag is None:
        return params
    return FrozenDict({**dict(params), "kpi_tag": kpi_tag})


def _partition(
    messages: Sequence[ModelMessage],
) -> tuple[list[ModelMessage], list[list[ModelMessage]], list[ModelMessage]]:
    """Split ``messages`` into (protected head, droppable whole-turn groups, protected suffix).

    * suffix_start = index of the LAST ``ModelRequest`` bearing a ``UserPromptPart``.
      If none, or 0, NOTHING is droppable: ``([], [], list(messages))``.
    * head = the first request's ``SystemPromptPart``s ONLY (a ``dataclasses.replace``
      copy that preserves ``.instructions`` for the direct-caller fallback scan),
      when ``messages[0]`` is a ``ModelRequest`` with ≥1 ``SystemPromptPart``.
    * groups = a single-pass scan over ``messages[0:suffix_start]``: a
      ``UserPromptPart``-bearing ``ModelRequest`` starts a new group (for
      ``messages[0]`` its NON-system parts start group 0 via a replace() copy that
      drops the ``SystemPromptPart``s already lifted into head; an all-system first
      request contributes no group). Whole-turn grouping keeps every
      ``ToolCallPart``/``ToolReturnPart`` pair inside ONE group — no dangling halves.
    """
    suffix_start = -1
    for i, m in enumerate(messages):
        if isinstance(m, ModelRequest) and any(isinstance(p, UserPromptPart) for p in m.parts):
            suffix_start = i
    if suffix_start <= 0:
        return [], [], list(messages)

    head: list[ModelMessage] = []
    first = messages[0]
    if isinstance(first, ModelRequest):
        system_parts = [p for p in first.parts if isinstance(p, SystemPromptPart)]
        if system_parts:
            head = [dataclasses.replace(first, parts=system_parts)]

    groups: list[list[ModelMessage]] = []
    current: list[ModelMessage] | None = None
    for i in range(suffix_start):
        m = messages[i]
        if i == 0 and isinstance(first, ModelRequest):
            # The first request's SYSTEM parts are in head; its remaining parts
            # (the turn-0 user prompt et al.) start group 0 — skip entirely if
            # nothing remains (an all-system first request).
            rest = [p for p in first.parts if not isinstance(p, SystemPromptPart)]
            if rest:
                current = [dataclasses.replace(first, parts=rest)]
                groups.append(current)
            continue
        if isinstance(m, ModelRequest) and any(isinstance(p, UserPromptPart) for p in m.parts):
            current = [m]
            groups.append(current)
        else:
            if current is None:
                current = []
                groups.append(current)
            current.append(m)

    return head, groups, list(messages[suffix_start:])


def _catalog_text(
    entries: Sequence[tuple[str, str, Any]],
    *,
    final_answer_instruction: str = _PLAIN_TEXT_FINAL_ANSWER,
) -> str:
    """Render the in-band tool catalog system message from (name, description, schema).

    The SINGLE renderer shared by :meth:`GatewayModel._render_catalog` (over live
    ``ToolDefinition``s) and :func:`catalog_text_for_tool_specs` (over ``ToolSpec``s
    at the ne.api boot check), so the boot-check estimate is byte-identical to what
    the running loop appends (pinned by the platform byte-parity test).
    """
    lines = ["Available tools (JSON):"]
    for name, description, parameters in entries:
        lines.append(
            json.dumps(
                {"name": name, "description": description, "parameters": parameters},
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    lines.append(_TOOL_CALL_PROTOCOL + final_answer_instruction)
    return "\n".join(lines)


def catalog_text_for_tool_specs(specs: Sequence[ToolSpec]) -> str:
    """The in-band tool catalog message text a :class:`GatewayModel` WOULD append for
    ``specs`` — the ne.api boot check (``validate_tool_catalog_budgets``) estimates
    THIS so a small-window primary that cannot hold its own catalog fails LOUD.

    Byte-identity with the live loop's catalog (pinned by the platform test): each
    spec's ``input_schema`` is plain-ified exactly as ``runner._to_plain_schema``
    does — deep-converted (FrozenDict → dict via ``json.dumps(default=dict)``) and
    the SAME permissive ``{'type':'object','properties':{}}`` default for an empty
    schema — because the live loop builds its ``ToolDefinition`` via
    ``Tool.from_schema(json_schema=_to_plain_schema(...))`` and pydantic-ai 2.5.0
    passes that schema through UNCHANGED to ``parameters_json_schema``. Replicated
    (not imported) to avoid the ``ne.runtime.runner`` import cycle.

    Byte-identity holds for the DEFAULT final-answer clause, i.e. every
    ``output_type=str`` loop. A model that overrides
    :meth:`GatewayModel.final_answer_instruction` (today only
    ``runner._StructuredAgenticGatewayModel``) emits a catalog differing from this
    estimate by exactly that one substituted sentence — a handful of tokens, never
    a difference in the tool entries themselves.
    """
    entries: list[tuple[str, str, Any]] = []
    for spec in specs:
        schema = json.loads(json.dumps(spec.input_schema, default=dict))
        if not schema:
            schema = {"type": "object", "properties": {}}
        entries.append((spec.name, spec.description or "", schema))
    return _catalog_text(entries)


def _as_text(value: object) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, default=str, separators=(",", ":"), sort_keys=True)


def _render_tool_call(name: str, args: Any) -> str:
    arguments: Any
    if isinstance(args, str):
        try:
            arguments = json.loads(args)
        except (json.JSONDecodeError, ValueError):
            arguments = {}
    elif isinstance(args, Mapping):
        arguments = dict(args)
    else:
        arguments = args or {}
    # ``name`` FIRST, deliberately NOT ``sort_keys`` (2026-08-04 live lesson):
    # the model imitates HISTORY exemplars more faithfully than the
    # instruction, and the previous sorted render put ``arguments`` before
    # ``name`` — the exact opposite of _TOOL_INSTRUCTION's canonical shape —
    # nudging multi-call turns toward name-last (and eventually name-DROPPED)
    # emissions, the residue the output validator now catches. History
    # exemplars and the instruction now show ONE identical shape.
    return json.dumps(
        {"tool_call": {"name": name, "arguments": arguments}},
        separators=(",", ":"),
    )


# -- prompted-output transport normalisation (NE-OUTPUT-TRANSPORT) ----------
#
# WHAT THIS IS, AND WHAT IT IS EXPLICITLY NOT. When an Agent declares a prompted
# structured ``output_type``, its final answer is transported as ONE JSON object.
# A model that produces the right object but wraps it — a "Here is the answer:"
# preamble, an indented object inside a ``` fence, a trailing "Let me know if..."
# — has satisfied the CONTRACT and failed the TRANSPORT. pydantic-ai already
# concedes this class of thing: ``_utils.strip_markdown_fences`` unwraps a fenced
# object before ``validate_json``. Its regex, though, is
# ``r'```(?:\w+)?\r?\n(\{.*?\})\s*(?:\r?\n?```|\Z)'`` — which requires a fence,
# requires the ``{`` to sit immediately after the fence's newline, and therefore
# misses (a) a BARE object behind a prose preamble, (b) an INDENTED object inside
# a fence, (c) a fence opened and closed on one line. All three were live-plausible
# shapes for the frontier lane's ``json_invalid`` on the WHOLE reply.
#
# This widens accepted TRANSPORT and NOTHING else. The extracted bytes are handed
# to the same ``validate_json`` against the same model with the same
# ``extra="forbid"``; a reply that is genuinely malformed, truncated mid-object,
# or shaped as some other object still fails, and fails naming what was wrong. It
# is the exact same trade the ``_FENCED_BLOCK_RE`` unwrap above already makes for
# the in-band tool_call protocol.
#
# AMBIGUITY FAILS CLOSED. Extraction applies only when the reply contains EXACTLY
# ONE parseable top-level JSON object. Two candidates (e.g. the model narrated an
# example object and then emitted the real one) is not a transport wrapper, it is
# a reply we cannot read without guessing — so it is left untouched and rejected.


#: The longest reply the transport-normalisation scan (and the shape summary,
#: ``rejected_reply_shape``) will read. Measured over the STRIPPED reply in
#: BOTH places — one definition of "pathological", so leading whitespace can
#: neither push a short reply over the cap in one layer while the other still
#: scans it, nor make the summary report ``unscanned`` for bytes normalisation
#: did in fact read. The scan restarts one character after each failed
#: ``raw_decode``, so it is superlinear on pathological input and runs on the
#: hottest rejection path; past the cap a reply is simply not normalised, and
#: earns the rejection it already earns.
_MAX_SHAPE_SCAN_CHARS = 200_000


def sole_json_object(text: str) -> str | None:
    """The one parseable top-level JSON object inside ``text``, or ``None``.

    ``None`` when the reply already IS a JSON value (nothing to normalise), when
    no top-level object parses, when more than one does (ambiguous — see the
    module comment above), or when the stripped reply is longer than
    ``_MAX_SHAPE_SCAN_CHARS`` (see the cap's note). Uses
    ``json.JSONDecoder.raw_decode`` rather than brace counting, so braces inside
    string literals and escapes are handled by the JSON grammar itself and never
    by an approximation of it.

    NE-ANSWER-CTRLCHAR. The decoder runs with ``strict=False``, which widens the
    grammar in EXACTLY one way: a raw control character (a literal newline, a
    tab) is admitted INSIDE a string literal. That is the S1 live failure shape —
    the model wrote ``{"final_text":"line one\\n<literal newline>line two", ...}``
    with a real line break where JSON requires the two characters ``\\n``, and
    the whole delivery failed ``json_invalid`` (``opens=object objects=0``) four
    times over on an answer whose CONTENT was correct. When the sole candidate is
    admitted only by that widening, its bytes are not strict JSON and cannot be
    handed to ``validate_json`` as they are, so the candidate is re-serialised
    with ``json.dumps`` — the identical decoded value, control characters now
    escaped, key order preserved (``dumps`` keeps insertion order). This is still
    transport only: the re-serialised object faces the same schema, the same
    ``extra="forbid"`` and the same claim binding, and a truncated, ambiguous or
    wrong-shaped reply is as rejected as it ever was. ``strict=False`` admits
    nothing else — not single quotes, not trailing commas, not bare words.
    """
    stripped = text.strip()
    if not stripped or len(stripped) > _MAX_SHAPE_SCAN_CHARS:
        return None
    try:
        json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        pass
    else:
        # Already a well-formed JSON value: pydantic-ai's own path handles it and
        # this function must not rewrite bytes that need no rewriting.
        return None

    decoder = json.JSONDecoder(strict=False)
    found: list[str] = []
    index = 0
    while True:
        start = stripped.find("{", index)
        if start < 0:
            break
        try:
            value, end = decoder.raw_decode(stripped, start)
        except (json.JSONDecodeError, ValueError):
            index = start + 1
            continue
        if isinstance(value, dict):
            candidate = stripped[start:end]
            try:
                json.loads(candidate)
            except (json.JSONDecodeError, ValueError):
                # Admitted only by ``strict=False``: same value, strict bytes
                # (NE-ANSWER-CTRLCHAR above). ``ensure_ascii=False`` keeps the
                # model's own non-ASCII text as it wrote it.
                candidate = json.dumps(value, ensure_ascii=False)
            found.append(candidate)
            if len(found) > 1:
                return None
            index = end
        else:  # pragma: no cover - raw_decode from "{" cannot yield a non-dict
            index = start + 1
    return found[0] if len(found) == 1 else None


def is_lone_tool_call_object(text: str) -> bool:
    """``True`` iff ``text`` parses to a dict whose key set is exactly ``tool_call``.

    The SELECTION predicate for :func:`sole_answer_object`, deliberately the same
    decision boundary :func:`is_malformed_tool_call_residue` already uses — a lone
    ``tool_call`` object — so the two cannot drift into disagreeing about what
    protocol residue is. It does not care whether the call is well formed: a
    well-formed one is not an answer either.
    """
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return False
    return isinstance(value, dict) and set(value) == {"tool_call"}


def sole_answer_object(text: str) -> str | None:
    """The one top-level object in ``text``, unless it is a tool call.

    NE-ANSWER-SELECT. :func:`sole_json_object` answers "is there exactly one
    object here"; that is a TRANSPORT question and it has no business knowing what
    an answer is. This wrapper adds the one ANSWER-specific rule: a lone
    ``{"tool_call": ...}`` object is protocol residue, never a final answer, so it
    is not selected.

    Why it matters. In-band tool calling (D3) means a tool call IS a JSON object
    in a text reply. ``GatewayModel._parse`` turns a well-formed one into a
    ``ToolCallPart``, but a MALFORMED one is demoted to text by the
    parse-never-raises design — and normalisation would then hand that
    tool-call-shaped object to the answer schema, which rejects it as an
    undeclared key with no ``final_text``. The model's protocol slip becomes an
    unreadable answer-contract failure.

    Refusing to select it FAILS CLOSED, exactly as two candidate objects already
    do: the reply is left untouched and rejected, and ``rejected_reply_shape``
    names ``sole=tool-call`` so the reason says which mistake was made. Nothing is
    relaxed — an object carrying ``tool_call`` could never satisfy
    ``extra="forbid"`` anyway, so no reply that could have been served stops being
    served. And this holds for a WELL-FORMED lone call too — NE-CALL-SELECT
    (593ef6d5, reverted 2026-08-29 by arbiter ruling) briefly promoted that case
    into a governed invocation, and the revert's rationale is pinned at the
    request-path call site: the attacker authors the retrieved text the model
    quotes, so no text-positional rule can separate the model's own call from an
    injected payload the model is quoting or refusing. A wrapped call stays text,
    is rejected as an answer, and comes back as a recoverable answer-shape retry
    (NE-RETRY-LEGIBLE); the ONLY accepted call transports are ``_parse``'s.
    """
    candidate = sole_json_object(text)
    if candidate is None or is_lone_tool_call_object(candidate):
        return None
    return candidate


def is_prose_wrapped_tool_call(text: str) -> bool:
    """``True`` iff ``text`` is a lone tool call the model WRAPPED in other text.

    NE-WRAPPED-CALL. The selection predicate for
    :data:`PROSE_WRAPPED_TOOL_CALL_CORRECTION`, and deliberately not a fourth
    opinion about what a tool call is: it composes the two functions that
    already decide it. :func:`sole_json_object` returns ``None`` when the whole
    stripped reply is already well-formed JSON, so a reply that IS the object
    can never be selected here — only one embedded in surrounding text can —
    and :func:`is_lone_tool_call_object` is the same predicate that stamps
    ``sole=tool-call`` into :func:`rejected_reply_shape`.

    It therefore reads exactly as the negation of :func:`sole_answer_object`'s
    one refusal: the reply carries exactly one top-level object, that object is
    a lone ``tool_call``, and it is not the whole reply.

    This SELECTS A CORRECTION and nothing else. It never promotes anything: the
    accepted call transports remain ``_parse``'s three, the separating rule
    remains "the object IS the whole reply", and a wrapped call stays text and
    executes nothing (NE-CALL-SELECT, reverted 2026-08-29 — see
    :func:`sole_answer_object`).
    """
    candidate = sole_json_object(text)
    return candidate is not None and is_lone_tool_call_object(candidate)


def _loc_of(detail: Mapping[str, Any]) -> Sequence[Any] | None:
    """``detail``'s ``loc`` as a sequence, or ``None`` if it is not one.

    A missing ``loc`` reads as the empty path, which is pydantic's own default in
    ``model_response``. A ``str``/``bytes`` ``loc`` is not a path and is refused
    rather than measured: ``len("claims")`` would otherwise pass for depth 6.
    """
    loc = detail.get("loc", ())
    if isinstance(loc, str | bytes) or not isinstance(loc, Sequence):
        return None
    return loc


def _rejection_carries_the_whole_reply(detail: Mapping[str, Any]) -> bool:
    """``True`` iff this error entry's ``input`` IS the rejected reply itself.

    Exactly one ``loc`` means that: the EMPTY one. ``loc=()`` addresses the root,
    so its ``input`` is the whole reply as the model sent it. ``loc=("claims",)``
    is already one step in — it addresses a top-level FIELD, and its ``input`` is
    that field's value, not the reply — and every deeper ``loc`` addresses a
    sub-value of one. So depth 0 is the reply and depth >= 1 is a part of it.

    How this relates to pydantic's rule, which is DIFFERENT and deliberately so.
    ``RetryPromptPart.model_response`` excludes ``input`` from a final-answer
    rejection when::

        {'ctx', 'input'} if len(e.get('loc', ())) <= 1 else {'ctx'}

    that is, at depth <= 1 — a STRICTLY WIDER band than this predicate's depth 0.
    The two are not the same question. Pydantic's asks "is echoing this ``input``
    redundant or over-long in the render"; this one asks "is this ``input`` a
    whole reply I may classify". Depth 1 is the band where the answers differ:
    pydantic drops the echo there (so C12 is already satisfied — see
    :func:`_pydantic_excludes_the_rejection_input`), while this predicate refuses
    it, because a top-level field's value is not a reply. Reading depth 1 as a
    reply is precisely the mis-fire this predicate used to have: a valid final
    answer with one bad field earned the tool-call correction.

    What this does NOT decide: whether the entry is rendered, what its ``input``
    contains, or whether the rejection is recoverable. It answers the address
    question only.
    """
    loc = _loc_of(detail)
    return loc is not None and len(loc) == 0


def _pydantic_excludes_the_rejection_input(detail: Mapping[str, Any]) -> bool:
    """``True`` iff pydantic-ai's own render already drops this entry's ``input``.

    Pydantic's rule, restated once here so :func:`_without_nested_rejection_inputs`
    does not guess at it. ``RetryPromptPart.model_response`` builds, for a
    final-answer rejection (``tool_name is None``) only::

        {'ctx', 'input'} if len(e.get('loc', ())) <= 1 else {'ctx'}

    so ``input`` survives into the render exactly when ``len(loc) > 1``.

    This is NOT :func:`_rejection_carries_the_whole_reply` — that one admits only
    ``loc == ()``, and the depth-1 entries this admits are the difference. Nor
    does it describe a TOOL-CALL rejection (``tool_name`` set): that branch
    excludes only ``ctx`` and keeps every ``input``, and this adapter never sends
    such a part through the stripping helper.
    """
    loc = _loc_of(detail)
    return loc is not None and len(loc) <= 1


#: A ``loc`` segment the server may republish verbatim: a name that LOOKS like a
#: field declared in one of this runtime's own output schemas (or a tool's own
#: argument list). A segment that does not match was not ours to publish, so it
#: is masked rather than guessed at.
_SCHEMA_FIELD_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]{0,63}\Z")

#: What a masked ``loc`` segment renders as. ONE spelling, used by both the
#: string renderer and the segment sanitiser, so a reader of a ``safe_reason``
#: and a model reading a retry are told the same thing.
UNDECLARED_KEY_PLACEHOLDER = "<undeclared key>"

#: What an EMPTY ``loc`` renders as in the string form: the rejection addresses
#: the root, i.e. the reply as a whole.
WHOLE_REPLY_LOCATION = "<whole reply>"


def safe_error_location_parts(loc: Any, *, code: str) -> tuple[int | str, ...]:
    """One Pydantic ``loc``, masked to segments this server is entitled to publish.

    THE single implementation of the ``loc`` rule. ``safe_error_location`` renders
    its result as a string for a ``safe_reason``; :func:`_with_safe_error_locations`
    puts it back into an error entry so the RETRY RENDER carries it too. There is
    exactly one such function in the tree, pinned by
    ``test_output_transport.py::test_there_is_exactly_one_loc_sanitiser``.

    ``loc`` is server-owned for every error type EXCEPT ``extra_forbidden``, whose
    final segment is the key the MODEL invented — model-authored text that must
    never be republished (C12). That segment becomes
    :data:`UNDECLARED_KEY_PLACEHOLDER`. Every remaining string segment is
    additionally required to look like a declared field name, so a surprising
    ``loc`` shape degrades to a placeholder instead of leaking whatever it held.

    Integer segments (array indices) are server-owned and survive as integers, so
    a ``loc`` put back into an error entry keeps pydantic's own
    ``tuple[int | str, ...]`` shape and its depth. Depth is load-bearing: both
    :func:`_pydantic_excludes_the_rejection_input` and pydantic-ai's own ``input``
    exclusion read ``len(loc)``, and masking must not move an entry between bands.
    """
    parts = list(loc) if isinstance(loc, tuple | list) else []
    if code == "extra_forbidden" and parts:
        parts[-1] = None
    return tuple(
        part
        if isinstance(part, int)
        else part
        if isinstance(part, str) and _SCHEMA_FIELD_RE.match(part)
        else UNDECLARED_KEY_PLACEHOLDER
        for part in parts
    )


def safe_error_location(loc: Any, *, code: str) -> str:
    """One Pydantic ``loc`` rendered as a dotted path through OUR schema.

    The string form of :func:`safe_error_location_parts` — same masking, joined
    with ``.``; an empty path is the root and renders as
    :data:`WHOLE_REPLY_LOCATION`. Used by ``runner._output_contract_reason`` to
    build a server-authored ``safe_reason``.
    """
    return (
        ".".join(str(part) for part in safe_error_location_parts(loc, code=code))
        or WHOLE_REPLY_LOCATION
    )


def _with_safe_error_locations(details: Sequence[Any]) -> list[Any]:
    """``details`` with every ``loc`` masked by :func:`safe_error_location_parts`.

    The half of C12 the first round missed. ``RetryPromptPart.model_response``
    renders each entry's ``loc`` verbatim, and this adapter re-serves that render
    as a ``role="user"`` message (:meth:`GatewayModel._map_request_parts`) — so for
    an ``extra_forbidden`` rejection the KEY THE MODEL INVENTED came back wearing
    the user's role. A model that had quoted an injected key out of a retrieved
    document therefore read its own quotation as an instruction from the user.
    ``input`` was fixed for that path; ``loc`` was not, and the sanitiser that
    already existed for the ``safe_reason`` path was never called here.

    Applied to BOTH retry branches, because ``extra_forbidden`` reaches both: a
    final-answer rejection against ``_StructuredAgenticCompletion`` and a TOOL
    ARGUMENT rejection against a tool's own argument model (measured: an
    undeclared argument name lands in ``loc`` identically).

    Depth is preserved exactly (see :func:`safe_error_location_parts`), so this
    can neither move an entry across pydantic-ai's ``input``-exclusion band nor
    change which correction :func:`rejects_a_prose_wrapped_tool_call` selects.
    An entry that is not a mapping is passed through untouched.
    """
    out: list[Any] = []
    for detail in details:
        if not isinstance(detail, Mapping):
            out.append(detail)
            continue
        code = str(detail.get("type", "unknown"))
        out.append({**detail, "loc": safe_error_location_parts(detail.get("loc", ()), code=code)})
    return out


def _without_nested_rejection_inputs(details: Sequence[Any]) -> list[Any]:
    """``details`` with the ``input`` dropped from every NESTED rejection (C12).

    "Nested" here means the band pydantic-ai does NOT strip for itself:
    ``len(loc) > 1``. The entries :func:`_pydantic_excludes_the_rejection_input`
    admits — depth 0 AND depth 1 — are left untouched, because pydantic excludes
    their ``input`` from the render itself and stripping them here would change
    nothing a model ever sees. The rest lose the key entirely, which is the SAME
    shape pydantic's own exclusion produces (it renders ``type``/``loc``/``msg``
    and no ``input`` field), so nothing downstream sees a new spelling.

    Note which predicate this uses. It is deliberately NOT
    :func:`_rejection_carries_the_whole_reply`, which admits only ``loc == ()``:
    this helper's question is "will the render echo this ``input``", not "is this
    ``input`` a whole reply", and the depth-1 entries are exactly where the two
    answers part. Using the narrower one here would strip a depth-1 ``input``
    pydantic was already dropping — harmless for the render, but it would state
    a relationship between the two rules that does not hold.

    What this does NOT do: it does not redact the ``input`` it leaves in place
    (there is none left to redact in the render), it does not touch ``loc``,
    ``type`` or ``msg``, and it makes no claim about a TOOL-CALL rejection's
    entries, which take pydantic's other branch and never reach here. ``loc`` is
    the NEXT caller's job — :func:`_with_safe_error_locations` masks it, on both
    branches — because it is the one remaining field that is sometimes
    model-authored, and this helper deliberately does not spread into it.

    Why drop it rather than keep the information. A nested ``input`` is a verbatim
    slice of the model's reply, and this adapter re-serves the rendered retry as a
    ``role="user"`` message (:meth:`GatewayModel._map_request_parts`). The reply
    itself is ALREADY in the history as the ``role="assistant"`` turn it was, so
    the echo adds no fact the model cannot see — it only re-dresses text the model
    may have quoted out of a retrieved document as something the USER said. That
    is the prompt-injection surface the NE-CALL-SELECT revert was about (the
    attacker authors the retrieved text), and the loss is small and bounded:
    ``loc`` still names the DECLARED part of the path (masked to
    ``<undeclared key>`` only where the model invented the key itself), and
    ``type``/``msg`` still name the constraint.
    """
    out: list[Any] = []
    for detail in details:
        if isinstance(detail, Mapping) and not _pydantic_excludes_the_rejection_input(detail):
            out.append({key: value for key, value in detail.items() if key != "input"})
        else:
            out.append(detail)
    return out


def rejects_a_prose_wrapped_tool_call(details: object) -> bool:
    """``True`` iff a WHOLE-REPLY rejection in ``details`` rejected a wrapped call.

    ``details`` is a ``RetryPromptPart.content`` schema-rejection list — pydantic's
    own error entries. Only the entries :func:`_rejection_carries_the_whole_reply`
    admits — ``loc == ()``, the root — carry the rejected reply under ``input``.
    Every other entry, at depth 1 as much as at depth 3, carries a VALUE FROM
    INSIDE a reply that parsed, and reading one as a reply MIS-FIRES: a valid
    final-response object whose ``claims`` field merely holds prose around a
    ``tool_call`` object would otherwise select the CALL correction — telling a
    model that produced an answer to re-send a tool call — for what is an
    answer-shape rejection. (It did: the predicate admitted ``len(loc) <= 1``
    until this was fixed, so a top-level field's value was read as a reply.)
    Anything else (a server-authored string correction, an unexpected shape) is
    ``False``.

    What a ``True`` here does NOT mean. It does not mean a call was made, may be
    made, or was recognised as well-formed by ``_parse``: it SELECTS WHICH
    CORRECTION SENTENCE the retry carries and nothing else. The accepted call
    transports remain ``_parse``'s, and a wrapped call stays text (NE-CALL-SELECT,
    reverted 2026-08-29). It also makes no claim about a rejection whose entries
    carry no ``input`` at all, or whose ``input`` is not a string: those are
    simply not selected.
    """
    if not isinstance(details, Sequence) or isinstance(details, str | bytes):
        return False
    for detail in details:
        if not isinstance(detail, Mapping) or not _rejection_carries_the_whole_reply(detail):
            continue
        value = detail.get("input")
        if isinstance(value, str) and is_prose_wrapped_tool_call(value):
            return True
    return False


def rejected_reply_shape(text: str) -> str:
    """A SERVER-AUTHORED description of a rejected reply's SHAPE — never its bytes.

    Why this exists. When a prompted-output reply fails to parse at all, the run
    can today say only ``<whole reply> [json_invalid]``: it names the slot and the
    error code, and neither of those distinguishes "the model wrote prose" from
    "the model fenced the object" from "the answer was cut off mid-object". Four
    consecutive live diagnoses stalled on exactly that, because every other
    surface on the box records COUNTS and never CONTENT — the audit rows, the
    model-step rows, the worker logs, and OTel at ``verbosity: basic``.

    What it may and may not say. Every token below is drawn from a CLOSED,
    server-owned vocabulary plus integer counts. It reads the reply and publishes
    NO byte of it — no substring, no first line, no redacted excerpt — so it
    carries the same C12 guarantee as the ``loc``/``type`` pair beside it, while
    answering the question that pair cannot.
    """
    stripped = text.strip()
    if len(stripped) > _MAX_SHAPE_SCAN_CHARS:
        # Same cap, same bytes as ``sole_json_object``: measured over the
        # STRIPPED reply, so "unscanned" is never claimed for a reply the
        # normalisation scan did read (whitespace padding cannot split the two).
        return f"chars={len(stripped)} unscanned"
    if not stripped:
        return "empty"

    if stripped.startswith("{"):
        opens = "object"
    elif stripped.startswith("```"):
        opens = "fence"
    elif stripped.startswith("["):
        opens = "array"
    else:
        opens = "prose"

    decoder = json.JSONDecoder()
    objects = 0
    index = 0
    while True:
        start = stripped.find("{", index)
        if start < 0:
            break
        try:
            value, end = decoder.raw_decode(stripped, start)
        except (json.JSONDecodeError, ValueError):
            index = start + 1
            continue
        if isinstance(value, dict):
            objects += 1
            index = end
        else:  # pragma: no cover - raw_decode from "{" cannot yield a non-dict
            index = start + 1

    # NE-ANSWER-SELECT. When the reply carries exactly one object and that object
    # is a lone tool call, say so: the difference between "the model wrote prose"
    # and "the model called a tool where its answer belonged" is the difference
    # between a transport problem and a protocol slip, and they want different
    # fixes. A closed token, never a byte of the reply.
    sole = ""
    if objects == 1:
        candidate = sole_json_object(stripped) or (stripped if opens == "object" else None)
        if candidate is not None and is_lone_tool_call_object(candidate):
            sole = " sole=tool-call"
    return (
        f"opens={opens} fences={stripped.count('```')} "
        f"objects={objects} chars={len(stripped)}{sole}"
    )
