# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Gateway error taxonomy — every message is ACTIONABLE and cites the exact
config key or model name at fault. There is never a silent any-provider
fallback: an unroutable request fails loud with the configured alternatives
named.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence


class NeGatewayError(Exception):
    """Base class for every ne-gateway error."""


class GatewayConfigError(NeGatewayError):
    """Boot-time rejection of an invalid :class:`GatewaySettings` tree."""


class UnknownTaskError(NeGatewayError):
    """A request named a task with no ``tasks.<task>`` route configured."""

    def __init__(self, task: str, configured: Sequence[str]) -> None:
        self.task = task
        self.configured = list(configured)
        super().__init__(
            f"task {task!r} is not configured under tasks.*; "
            f"configured tasks: {sorted(self.configured)!r}"
        )


class LaneMismatchError(NeGatewayError):
    """No configured candidate for the task serves the requested lane."""

    def __init__(
        self, task: str, requested_lane: str, candidates: Sequence[tuple[str, str]]
    ) -> None:
        self.task = task
        self.requested_lane = requested_lane
        self.candidates = list(candidates)
        rendered = ", ".join(f"{name} (lane={lane})" for name, lane in self.candidates)
        super().__init__(
            f"no configured candidate for task {task!r} serves the requested lane "
            f"{requested_lane!r}; candidates: [{rendered}]"
        )


#: Publication ceiling for :attr:`ModelCallFailedError.safe_reason`. The run
#: projection drops an ``error_reason`` longer than 256 characters outright, so a
#: long chain must be TRIMMED here rather than composed and silently discarded
#: there: candidates a reader receives beat a complete list they never see.
_MAX_SAFE_REASON_CHARS = 256


def _safe_attempts_reason(
    attempts: Sequence[tuple[str, Exception]],
    *,
    task: str | None = None,
    lanes: Mapping[str, str] | None = None,
) -> str:
    """A publishable one-sentence summary of why no candidate served a request.

    Built from SERVER-OWNED identifiers only: the routed TASK name and the
    candidate's configured model name and LANE (all three are keys/enums out of
    the gateway registry, never caller- or provider-controlled text) plus each
    failure's exception CLASS name. The rendered exception messages that
    :class:`ModelCallFailedError` carries for an operator are deliberately NOT
    read — a provider's 4xx text can echo prompt substrings (design §10.3 rule 5),
    and this string is published to the run record. ``safe_reason`` is the
    duck-typed opt-in ``run_workflow`` looks for, which is why it exists at all:
    without it a failed run publishes the bare exception class and no sentence.

    NE-DEMO-NO-CPU-FALLBACK: the sentence names the LANE of every candidate it
    lists, because lane exhaustion stopped being a corner case when the demo's
    routes dropped their 0.6B CPU tier. A reader now has to tell three
    situations apart from this one line — the private rig is unreachable, the
    frontier API refused, or AIR-GAP POSTURE blocked the frontier egress ON
    PURPOSE — and "no configured model produced an answer: frontier-default
    (EgressBlockedError)" does not distinguish a governed refusal from an
    outage, while ``frontier-default [frontier-api] (EgressBlockedError)``
    does. Under air-gap that exhaustion is an EXPECTED, demonstrable state, so
    it has to read as a deliberate refusal rather than a crash.
    """
    scope = f" for task {task!r}" if task else ""
    head = f"no authorized model lane could serve this request{scope}: "
    named: list[str] = []
    for name, exc in attempts:
        lane = (lanes or {}).get(name)
        cls = type(exc).__name__
        entry = f"{name} [{lane}] ({cls})" if lane else f"{name} ({cls})"
        if len(head + ", ".join([*named, entry])) > _MAX_SAFE_REASON_CHARS:
            break
        named.append(entry)
    if not named:
        # Either there were no attempts, or the first entry alone overran the
        # ceiling. Both must still yield a publishable sentence: the projection
        # drops an over-long reason outright, so a trimmed-to-nothing list has
        # to degrade to the bare claim rather than to no reason at all.
        return f"no authorized model lane could serve this request{scope}."
    return head + ", ".join(named)


class ModelCallFailedError(NeGatewayError):
    """Every candidate model for a request failed; enumerates each attempt.

    ``task`` and ``lanes`` are OPTIONAL and default to ``None`` so that every
    existing construction site (and every test that builds this error directly
    from an attempts tuple) keeps working unchanged; they only enrich the
    published ``safe_reason``. The gateway's two exhaustion sites supply both.
    """

    def __init__(
        self,
        attempts: tuple[tuple[str, Exception], ...],
        *,
        task: str | None = None,
        lanes: Mapping[str, str] | None = None,
    ) -> None:
        self.attempts = attempts
        self.task = task
        # Deferred import: ssrf.py imports this module at module level, so a
        # top-level import here would be circular. redact_url_for_logs strips any
        # credentialed URL a provider exception may have embedded so a cleartext
        # secret can never reach the rendered error message (self.attempts keeps
        # the raw exceptions for programmatic callers).
        from ne.gateway.ssrf import redact_url_for_logs

        rendered = "; ".join(
            f"{name}: {type(exc).__name__}: {redact_url_for_logs(str(exc))}"
            for name, exc in attempts
        )
        self.safe_reason = _safe_attempts_reason(attempts, task=task, lanes=lanes)
        super().__init__(f"all model candidates failed: [{rendered}]")


class ReasoningExhaustedError(NeGatewayError):
    """A candidate spent its whole output budget reasoning and delivered EMPTY
    content.

    The definitive signature of a reasoning model (hybrid-thinking open-weight
    via a reasoning parser, or a frontier extended-thinking model) whose
    ``max_tokens`` truncated generation inside the think channel: the provider
    reply carries a non-empty ``reasoning_content`` but no ``content`` at all.
    Returning the empty string here (the pre-fix behavior) presented as a
    silent empty answer the runtime could only retry into
    ``UnexpectedModelBehavior``; this typed error makes the condition loud,
    names the fix, and — appended to the candidate ``attempts`` chain — still
    lets a DECLARED fallback model serve the request (a configured fallback is
    a visible routing decision, never a silent downgrade).

    C12: the message carries COUNTS ONLY (token/char/config numbers and the
    provider's finish_reason enum) — never any reasoning or prompt text.
    """

    def __init__(
        self,
        model_name: str,
        *,
        completion_tokens: int | None,
        reasoning_chars: int,
        max_tokens: int,
        finish_reason: str | None,
    ) -> None:
        self.model_name = model_name
        self.completion_tokens = completion_tokens
        self.reasoning_chars = reasoning_chars
        self.max_tokens = max_tokens
        self.finish_reason = finish_reason
        super().__init__(
            f"model {model_name!r} returned empty content with non-empty reasoning "
            f"(completion_tokens={completion_tokens}, reasoning_chars={reasoning_chars}, "
            f"max_tokens={max_tokens}, finish_reason={finish_reason!r}): the output "
            "budget was exhausted inside the reasoning channel — raise this model's "
            "max_output_tokens (or the caller's max_tokens) so reasoning + content fit"
        )


class EmptyCompletionError(NeGatewayError):
    """A candidate returned a completion carrying NO content at all.

    The sibling of :class:`ReasoningExhaustedError`, and deliberately a separate
    class because the two are different facts about the provider: that one
    delivered reasoning and no answer, this one delivered NOTHING — neither
    ``content`` nor ``reasoning_content``. Both used to be reported by returning
    the empty string, and the empty string is what made this failure
    undiagnosable: it reached the runtime as a text answer of length zero, which
    pydantic-ai reports as a NON-ACTIONABLE response ("Please return text or call
    a tool"), which the runtime in turn reports as the model's reply failing the
    declared ANSWER FORMAT. Three hops, and the sentence the reader finally sees
    describes a schema rejection that never happened. Failing typed HERE — at the
    only boundary that saw the provider's reply — is what lets the record state
    what actually occurred, and (appended to the candidate ``attempts`` chain)
    lets a DECLARED fallback model serve the request instead.

    ``completion_tokens`` is load-bearing evidence, not decoration: ZERO means
    the provider generated nothing, while a NON-ZERO count on an empty reply
    means it generated tokens that arrived in no field this gateway reads — a
    provider/adapter mapping gap rather than a silent model. The record
    distinguishes them without anyone having to re-run the query.

    C12: COUNTS ONLY (token/config numbers and the provider's finish_reason
    enum) — the message never carries model or prompt text, and there is no
    response text to carry.
    """

    def __init__(
        self,
        model_name: str,
        *,
        completion_tokens: int | None,
        max_tokens: int,
        finish_reason: str | None,
    ) -> None:
        self.model_name = model_name
        self.completion_tokens = completion_tokens
        self.max_tokens = max_tokens
        self.finish_reason = finish_reason
        super().__init__(
            f"model {model_name!r} returned an empty completion: no content and no "
            f"reasoning (completion_tokens={completion_tokens}, "
            f"max_tokens={max_tokens}, finish_reason={finish_reason!r}) — the "
            "provider produced no answer to serve"
        )


class RegistryValidationError(NeGatewayError):
    """A model registry entry failed validation; the message names the exact
    entry and field at fault.
    """


class UnknownModelIdError(NeGatewayError):
    """A registry lookup named a model_id with no registered entry."""

    def __init__(self, model_id: str, registered: Sequence[str]) -> None:
        self.model_id = model_id
        self.registered = list(registered)
        super().__init__(
            f"model_id {model_id!r} is not registered; "
            f"registered models: {sorted(list(registered))!r}"
        )


class UnknownRoutedModelError(NeGatewayError):
    """A RUNTIME route override named a model the boot config never registered.

    NE-MODEL-ROUTING §2.1/§5. Raised at CALL time by
    ``LiteLLMGateway._candidates`` so the property "a routing change can only
    ever re-select among boot-composed, SSRF-validated, lane-classified
    ``ModelDef``s" holds even if a row reached the override table by some path
    other than the governed one.

    It is deliberately a REFUSAL, not a fallback to the boot primary. A silent
    revert is indistinguishable from a successful swap at the call site, which
    is exactly the silent-downgrade this platform refuses to ship.
    """

    def __init__(self, task: str, model: str, registered: Sequence[str]) -> None:
        self.task = task
        self.model = model
        self.registered = list(registered)
        super().__init__(
            f"runtime route override for task {task!r} names model {model!r}, "
            "which is not in the boot-composed model set; refusing to serve "
            "(a routing change may only re-select a registered model, never "
            f"introduce one); registered models: {sorted(list(registered))!r}"
        )


class UnsafeEndpointError(NeGatewayError):
    """An admin-registered endpoint URL was rejected by the SSRF validator
    (NE-012). The message names exactly why (metadata IP, private range,
    credentials-in-URL, query/fragment, bad scheme, …).
    """


class ReasoningWithoutAnswerError(NeGatewayError):
    """A candidate ENDED ITS TURN NORMALLY having reasoned and produced no answer.

    The third member of the empty-reply family, and separate from both siblings
    because it is a different fact about the provider:

    * :class:`ReasoningExhaustedError` — the reply was CUT OFF inside the think
      channel (``finish_reason='length'``). The remediation it names is real:
      raise ``max_output_tokens`` so reasoning and content both fit.
    * :class:`ReasoningWithoutAnswerError` — this. The model reasoned, then
      stopped of its own accord (``finish_reason='stop'``/``'end_turn'``, or an
      unstated one) with no content block at all. Raising ``max_tokens`` would
      change NOTHING, so saying so would be wrong operator guidance.
    * ``EmptyCompletionError`` — the reply carried nothing at all: no content,
      no reasoning, no thinking block. A silent provider, not a reasoning one.

    The class name is load-bearing, not taxonomy: an attempt chain renders
    each failure as its exception CLASS NAME, so the name IS the terminal a
    reader is shown. "Reasoning without answer" is what happened; "exhausted"
    and "empty" would both be false.

    Live shape that made this necessary (2026-08-26, ``anthropic/claude-sonnet-5``
    on the frontier lane): ``finish_reason='stop'``, ``completion_tokens=144``,
    ``content`` empty, ``reasoning_content`` empty, and ONE ``thinking_blocks``
    entry. Anthropic's current models run adaptive thinking BY DEFAULT when the
    request omits the ``thinking`` parameter, and default its ``display`` to
    ``"omitted"`` — which returns a thinking block whose ``thinking`` field is
    the empty string. litellm derives ``reasoning_content`` by concatenating
    exactly those fields, so it too came back empty. The model billed 144
    tokens of reasoning and returned no answer; ``reasoning_chars`` alone could
    not see it, and the record therefore said "no content and no reasoning" of
    a reply that demonstrably contained reasoning.

    ``thinking_blocks`` is a COUNT and nothing else. A thinking block's only
    text field is ``thinking`` (already summed into ``reasoning_chars``); its
    other field is an opaque ``signature`` / ``data`` replay token. There is no
    answer hiding in that channel, and this error exists precisely so the
    absence of an answer is REPORTED rather than manufactured from reasoning.

    C12: COUNTS ONLY — token/char/block/config numbers and the provider's
    finish_reason enum. Never reasoning text, never prompt text.
    """

    def __init__(
        self,
        model_name: str,
        *,
        completion_tokens: int | None,
        reasoning_chars: int,
        thinking_blocks: int,
        max_tokens: int,
        finish_reason: str | None,
    ) -> None:
        self.model_name = model_name
        self.completion_tokens = completion_tokens
        self.reasoning_chars = reasoning_chars
        self.thinking_blocks = thinking_blocks
        self.max_tokens = max_tokens
        self.finish_reason = finish_reason
        super().__init__(
            f"model {model_name!r} reasoned and returned NO answer "
            f"(completion_tokens={completion_tokens}, reasoning_chars={reasoning_chars}, "
            f"thinking_blocks={thinking_blocks}, max_tokens={max_tokens}, "
            f"finish_reason={finish_reason!r}): the turn ended normally with a "
            "reasoning/thinking channel and no content block — the output budget was "
            "NOT the limit, so raising it changes nothing. If this model is not meant "
            "to reason on this lane, turn its thinking off in the route/model params "
            "(the provider's default is ON for current Anthropic models); otherwise "
            "the declared fallback chain serves the request"
        )


class EgressBlockedError(NeGatewayError):
    """The egress policy denied a call (NE-012). The message names the model,
    the resolved egress host, and the configured allowlist plus the config key
    to fix (``NE_EGRESS_ALLOWED_FRONTIER_HOSTS`` env / ``allowed_frontier_hosts``
    in ``ne.toml``).
    """


class AirGapViolationError(EgressBlockedError):
    """A frontier-lane egress was attempted while ``AIR_GAP`` is engaged
    (NE-012, SPEC C2). The message names the model and states that only the
    private-vllm lane is available under air-gap.
    """


class RedactionRequiredError(GatewayConfigError):
    """A FRONTIER_API model is configured/attempted but no redactor was
    injected. Pre-egress PII redaction is mandatory on the frontier lane (SPEC
    C2/C12, design doc §10.3): ``None`` is a REFUSAL, not a no-op — the
    deliberate asymmetry with ``egress_guard``, whose ``None`` IS a documented
    no-op. Fix: wire ``redactor=create_redactor(...)`` at the composition root
    (the ne-api ``_default_gateway`` recipe).
    """


class FrontierProviderError(NeGatewayError):
    """A provider failure on the FRONTIER_API lane, reduced to the original
    exception CLASS NAME only. Provider 4xx text can echo prompt substrings; on
    the frontier lane that text must never reach ``_redacted_attempts`` / audit
    detail / ``ModelCallFailedError`` rendering (design §10.3 rule 5).
    Constructed as ``FrontierProviderError(type(exc).__name__)``; deliberately
    NOT chained via ``__cause__`` so traceback/cause formatting cannot
    resurrect the original text.
    """
