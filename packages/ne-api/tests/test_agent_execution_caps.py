# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""THE DISPLAYED CAP IS THE ENFORCED CAP.

The defect these tests close: the Conversation composer printed
``Caps · 12 steps · 10 tool calls`` straight off the manifest, while the run it
governs was built with a tool-call ceiling of ``min(12, 10)``, a model-request
ceiling of eleven, and no counter anywhere compared against twelve. A governed
number on the screen that no counter is measured against is worse than no number:
it teaches a reader that the governed numbers are decorative.

The pair:
  * the number the catalog SERVES is the number the executing adapter was BUILT
    with - read off both, never retyped; and
  * a run that genuinely exceeds that number still fails, loudly and terminally,
    rather than being quietly rounded up to whatever the loop felt like doing.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from ne.api.agent_execution_caps import enforced_agentic_caps
from ne.api.agent_manifest import load_agent_manifest
from ne.api.agents_wire import _enforced_caps_item
from ne.api.platform import _compose_workflow_execution_bindings
from ne.contracts.models import Completion, CompletionRequest, Usage
from ne.contracts.runtime import ToolSpec
from ne.contracts.types import ApprovalTier, Principal
from ne.runtime.config import RuntimeSettings
from ne.runtime.runner import output_retries, output_retry_allowances

_ROOT = Path(__file__).parents[3]
_REGISTRY = _ROOT / "deploy/agents/registry.toml"
_PRINCIPAL = Principal(id="user:meridian-ops", groups=("ops",))


class _NullSink:
    """The audit sink is exercised elsewhere; these tests are about ceilings."""

    async def append(self, event: Any) -> None:
        return None

    async def verify(self) -> bool:
        return True


class _ScriptedGateway:
    def __init__(self, completions: list[Completion]) -> None:
        self._queue = list(completions)

    async def complete(self, request: CompletionRequest) -> Completion:
        if self._queue:
            return self._queue.pop(0)
        return Completion(
            text=json.dumps({"final_text": "unreached", "claims": []}),
            model_id="stub",
            usage=Usage(prompt_tokens=3, completion_tokens=5),
        )

    def stream(self, request: CompletionRequest) -> Any:
        raise NotImplementedError


def _tool_call(intent: str) -> Completion:
    return Completion(
        text=json.dumps(
            {"tool_call": {"name": "read.kg.directory", "arguments": {"step_intent": intent}}}
        ),
        model_id="stub",
        usage=Usage(prompt_tokens=3, completion_tokens=5),
    )


class _ReadOnlyExecutor:
    """One read tool that always serves a row the run has not seen before."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    @property
    def tool_specs(self) -> tuple[ToolSpec, ...]:
        return (
            ToolSpec(
                name="read.kg.directory",
                description="Read the evidence directory.",
                tier=ApprovalTier.TIER_0,
                input_schema={"type": "object", "properties": {}},
            ),
        )

    async def execute(self, tool: str, args: Mapping[str, Any], *, principal: Principal) -> str:
        self.calls.append(tool)
        return f"row | status\nurn:ne:meridian:dispute:mer-dsp-{len(self.calls):03d} | served"


def _agentic_definitions() -> list[Any]:
    loaded = load_agent_manifest(_REGISTRY)
    return [
        definition
        for definition in loaded.manifest.agents
        if definition.workflow is None and definition.default_strategy == "agentic"
    ]


def test_every_served_cap_is_the_cap_its_adapter_was_built_with() -> None:
    """Displayed == enforced, for every agentic Agent this deployment ships.

    Both sides are READ, not written: the left from the catalog projection the
    console renders, the right from the BoundedAgenticAdapter the composition
    root hands the worker. There is no literal in this assertion that a drifting
    derivation could keep satisfying.
    """
    loaded = load_agent_manifest(_REGISTRY)
    _profiles, bindings = _compose_workflow_execution_bindings(loaded)
    definitions = _agentic_definitions()
    assert definitions, "the shipped registry has no agentic Agent to check"

    for definition in definitions:
        served = _enforced_caps_item(definition)
        adapter = bindings[definition.agent_id].agentic_adapter
        assert served is not None
        assert adapter is not None
        assert served.max_tool_calls == adapter.max_tool_calls
        assert served.max_total_tokens == adapter.total_tokens_limit
        assert served.wall_clock_seconds == adapter.wall_clock_seconds
        assert served.max_parked_writes == adapter.max_parked_writes


def test_the_served_caps_are_the_derived_ones_not_the_declared_ones() -> None:
    """The distinction the old footer erased, stated on the shipped numbers.

    The Meridian Analyst declares 40 steps and 34 tool calls. Thirty-four is
    enforced; forty is not enforced as anything - it narrows that thirty-four
    (without binding it, which is the point: `max_tool_calls` is the field whose
    name says "tool ceiling") and it buys wall clock. The wire therefore serves a
    tool-call ceiling and a model-request
    ceiling, and never a "steps" figure, because "steps" is the word the
    transcript band already uses for its own row count and the two are not the
    same quantity.
    """
    analyst = next(
        definition
        for definition in _agentic_definitions()
        if definition.label == "Meridian Analyst"
    )
    budgets = analyst.budgets
    caps = enforced_agentic_caps(budgets, analyst.tool_grants)

    assert caps.max_tool_calls == min(budgets.max_steps, budgets.max_tool_calls)
    # One final-answer request after the last tool call, plus the output-retry
    # pool: the SUM of THIS run's per-class allowances from
    # `ne.runtime.runner.output_retry_allowances` (protocol residue 1,
    # wrapped-call `max_tool_calls` since NE-WRAPPED-CALL-FUNDED, answer shape 4
    # since NE-SHAPE-ALLOWANCE-4, claim contract 1), not a number chosen here.
    # Reserving them is what keeps a broken answer contract from being reported
    # as a request budget the operator never actually hit — and it is read
    # through the RESOLVER, because one class is sized to the run's own ladder.
    assert caps.max_model_requests == caps.max_tool_calls + 1 + output_retries(caps.max_tool_calls)
    assert caps.wall_clock_seconds == float(budgets.max_steps * 10)
    assert caps.max_total_tokens == budgets.max_input_tokens + budgets.max_output_tokens
    # The declared step count is not a served ceiling: nothing counts to it.
    # Asserted as "each served cap IS its derived quantity" rather than as
    # "the number 12 appears nowhere" — a value test that passes only while no
    # derived cap happens to equal max_steps, which stopped being true the
    # moment the reserved output retry made the request ceiling 10 + 2.
    served = _enforced_caps_item(analyst)
    assert served is not None
    assert served.max_tool_calls == min(budgets.max_steps, budgets.max_tool_calls)
    assert served.max_model_requests == served.max_tool_calls + 1 + output_retries(
        served.max_tool_calls
    )
    assert served.max_parked_writes == caps.max_parked_writes
    # The one thing max_steps buys is wall clock, and it is served as seconds.
    assert served.wall_clock_seconds == float(budgets.max_steps * 10)


def test_a_deterministic_agent_serves_no_agentic_caps_at_all() -> None:
    """No bounded loop, no ceilings - not a row of zeroes standing in for them."""
    loaded = load_agent_manifest(_REGISTRY)
    deterministic = [
        definition
        for definition in loaded.manifest.agents
        if definition.default_strategy == "deterministic"
    ]
    assert deterministic, "the shipped registry has no deterministic Agent to check"
    assert all(_enforced_caps_item(definition) is None for definition in deterministic)


def test_a_run_that_exceeds_the_served_cap_still_fails_loudly() -> None:
    """The other half of the pair.

    Making the display honest is worthless if the number it now prints is not
    actually held. This drives the shipped Analyst's OWN adapter - the object the
    worker executes, taken from the composition root, not a set of numbers typed
    into this test - past the tool-call ceiling the console shows, and requires
    the run to end on the typed budget terminal with its draft WITHHELD. Never an
    answer, and never a quietly longer run.
    """
    loaded = load_agent_manifest(_REGISTRY)
    _profiles, bindings = _compose_workflow_execution_bindings(loaded)
    analyst = next(
        definition
        for definition in _agentic_definitions()
        if definition.label == "Meridian Analyst"
    )
    adapter = bindings[analyst.agent_id].agentic_adapter
    assert adapter is not None
    served = _enforced_caps_item(analyst)
    assert served is not None

    executor = _ReadOnlyExecutor()
    # One more planned call than the served ceiling allows, each serving a row the
    # run has not seen so the no-progress rule never fires and the CAP is what the
    # run runs into.
    gateway = _ScriptedGateway(
        [_tool_call(f"Hop {index}.") for index in range(served.max_tool_calls + 1)]
    )

    async def scenario() -> Any:
        return await adapter.run(
            task="exceed the served ceiling",
            principal=_PRINCIPAL,
            executor=executor,
            gateway=cast(Any, gateway),
            settings=RuntimeSettings(),
            sink=cast(Any, _NullSink()),
        )

    result = asyncio.run(scenario())

    assert (result.outcome, result.exhausted_cap) == ("budget-exhausted", "tool-calls")
    assert result.output == ""
    # The ceiling held at exactly the number the console prints.
    assert result.tool_calls_used == served.max_tool_calls
    assert len(executor.calls) == served.max_tool_calls


# --- NE-CLAIM-RETRY-RESERVE: the budget covers the ceiling it funds -----------


def test_the_token_budget_covers_exactly_the_request_ceiling_it_funds() -> None:
    """The manifest's token budget, re-derived here from the same constants.

    `max_total_tokens` is charged CUMULATIVELY across every request, so it has to
    be derived from the request ceiling — and the request ceiling moves whenever
    `output_retries` does. Deployed 2026-08-27, it did: a budget funding 11
    requests met a ceiling of 14 and the run died `budget_exhausted` before it
    ever reached the verifier. It moved again when the retry pool stopped being
    one fungible number and became the SUM of per-class allowances, and again
    when one of those classes stopped being a constant at all.

    Asserting EQUALITY rather than "fits" is deliberate. `<=` would pass just as
    happily on a budget raised to a round number that covers nothing in
    particular; equality means the number on the manifest is the number this
    workload costs, and any future change to the retry count fails here until it
    is re-derived rather than rounded.

    The constants are the registry's own (`deploy/agents/registry.toml`); the
    sequence is the classes in the order `_OUTPUT_RETRY_ALLOWANCES` enumerates
    them — residue, the whole ladder-sized `wrapped-call` allowance, the whole
    `answer-shape` allowance, then rejected claims, then the served answer. Each
    class costs what its REPLY costs in re-paid history: residue is a lone
    tool_call object (t), a wrapped call is prose around one (W = 439, measured
    on the two live S5 rejections, `chars=1134` and `chars=1284`, at 2.93
    chars/token), and only the ANSWER rejections cost a full `F`.

    ⚠ THE ENUMERATED ORDER IS NOT THE MOST EXPENSIVE INTERLEAVING, and the
    registry's Analyst block records the gap rather than this pin hiding it:
    because every increment is re-paid by every LATER retry request, the
    costliest permutation carries the largest increments longest (descending,
    claim first). That convention understated by 20755 at the 1/4/1 composition
    and by 631905 once `wrapped-call` is in the pool. It is the shared
    convention of all four derivations in this file and is left alone here on
    purpose; re-cutting it belongs in one pass over all four.

    S = 3090 since NE-ANALYST-INSTRUCTIONS: the zero-pin Analyst's own manifest
    instructions (508 chars = 174 tokens) joined the fixed prompt re-sent on
    every request when the Skill index stopped standing in for a system prompt.

    NE-SHAPE-ALLOWANCE-4 (2026-09-11) moved it again, and that is the whole
    reason this pin is asserted as an EQUALITY: `answer-shape` went 3 -> 4 on a
    measured live run, the request ceiling went 40 -> 41, and the manifest
    budget had to be re-derived 1256283 -> 1331813. Left alone it would have
    been 75530 SHORT of its own funded ceiling, and the run would have died
    `budget_exhausted`/`tokens` before ever reaching the 4th correction —
    replacing a legible output-contract failure with one that names the wrong
    cause. The two numbers are coupled in BOTH directions here: this assertion
    fails if the allowance moves without the budget, and equally if the budget
    moves without the allowance.

    NE-WRAPPED-CALL-FUNDED (2026-09-11) then moved it much further, in TWO
    separately attributable steps the registry block itemises:
      (a) R_shape 200 -> 242, a MEASUREMENT rather than a re-estimate. 200
          described pydantic-ai's error list alone, which stopped being the
          whole retry message once NE-RETRY-LEGIBLE appended
          `_STRUCTURED_ANSWER_RETRY_CORRECTION` to it. Rendered through
          `GatewayModel._render_retry_prompt` on this tree the message is 709
          chars = 242 tokens. +588 on every derivation in this file, and it is
          what makes R_wrap comparable to the constant beside it.
      (b) the ladder-sized `wrapped-call` class: 34 more retries at W + R_wrap
          = 786 each, so the request ceiling went 41 -> 75 and the budget
          1332401 -> 3666637.
    """
    S, C, t, F = 3358, 1300, 80, 4000
    R_res, R_shape, R_claim = 172, 242, 403
    W, R_wrap = 439, 347

    analyst = next(
        definition
        for definition in _agentic_definitions()
        if definition.label == "Meridian Analyst"
    )
    caps = enforced_agentic_caps(analyst.budgets, analyst.tool_grants)
    tools = caps.max_tool_calls
    retries = caps.max_model_requests - tools - 1
    assert retries == output_retries(tools)

    allowances = dict(output_retry_allowances(tools))
    step = t + C
    # The sequence the ledger actually permits, class by class: one residue, the
    # LADDER-SIZED wrapped-call allowance, the whole shape allowance, one
    # claim-contract correction. Reading it off the allowances rather than
    # hard-coding it means a re-partitioned pool that keeps the same TOTAL still
    # has to be re-derived here — and since NE-WRAPPED-CALL-FUNDED the
    # allowances are a FUNCTION of this run's own `max_tool_calls`, so the
    # resolver is what is read, never the raw module table (whose `wrapped-call`
    # entry is a negative sentinel).
    increments = [
        *[t + R_res] * allowances["protocol-residue"],
        *[W + R_wrap] * allowances["wrapped-call"],
        *[F + R_shape] * allowances["answer-shape"],
        *[F + R_claim] * allowances["claim-contract"],
    ]
    assert len(increments) == retries

    tool_input = sum(S + k * step for k in range(tools))
    first_final = S + tools * step
    carried, retry_input = 0, 0
    for increment in increments:
        carried += increment
        retry_input += S + tools * step + carried
    # Charged by CLASS, because the classes no longer cost the same. A residue
    # reply is a lone tool_call object (t); a wrapped call is prose around one
    # (W, measured); a rejected or served answer is a full F. The old
    # `F * retries` happened to equal this only while `wrapped-call` did not
    # exist and residue was exactly one.
    output = (
        tools * t
        + t * allowances["protocol-residue"]
        + W * allowances["wrapped-call"]
        + F * (allowances["answer-shape"] + allowances["claim-contract"] + 1)
    )

    assert tool_input + first_final + retry_input + output == caps.max_total_tokens == 3_666_637


def test_the_customer_support_token_budget_is_the_number_its_workload_costs() -> None:
    """NE-CS-SPLIT: the agent's budget is DERIVED, not copied from the Analyst.

    Same method, same constants, its OWN S: the NE-CS-SPLIT chain measured the
    7-tool catalog to 1932 (1665 at the baseline swap epoch, +152 for
    NE-GRAPH-ANCHOR/NE-CS-LOOKUP, +115 for NE-ANSWER-THE-QUESTION), and
    NE-CS-WHOLE-LADDER (owner decision 2026-08-28) adds exactly the five
    hardship verified-answer entries it grants, measured over the rendered
    `_catalog_text` lines as `build_verified_answer_tools` composes them today
    (443 + 935 + 1383 + 1925 + 994 = 5680 chars = 1939 tokens at 2.93):
    1932 + 1939 = 3871. Its OWN counted ceiling too (17 calls: the 15-call
    worst whole-ladder turn — eligibility for ONE loan, then
    draft/approve-send/record — plus the same +2 refinement headroom the
    Analyst's derivation uses). Equality, not `<=`, for the same reason as the
    Analyst's pin above: the manifest number must be what this workload costs.

    ⚠ `max_steps` IS NOT TOUCHED AND CANNOT BE. 23 is the subtrahend of this
    deployment's published 180 s approval window (420 - 230 - 10). Since
    NE-WRAPPED-CALL-FUNDED the request ceiling is 41, whose 340-390 s at the
    measured rate the 230 s clock does not fund — so a run that wrapped every
    rung is stopped by the CLOCK, named, rather than by an unfunded token
    budget that would name the wrong cause. The registry block says so.
    """
    S, C, t, F = 3871, 1300, 80, 4000
    R_res, R_shape, R_claim = 172, 242, 403
    W, R_wrap = 439, 347

    support = next(
        definition
        for definition in _agentic_definitions()
        if definition.label == "Meridian Customer Support"
    )
    caps = enforced_agentic_caps(support.budgets, support.tool_grants)
    tools = caps.max_tool_calls
    assert tools == 17
    # Three non-read grants (draft + the two inert Tier-2 outreach actions), so
    # a single turn may park at most three unsent writes — never more than the
    # write surface it was granted.
    assert caps.max_parked_writes == 3
    retries = caps.max_model_requests - tools - 1
    assert retries == output_retries(tools)

    allowances = dict(output_retry_allowances(tools))
    step = t + C
    increments = [
        *[t + R_res] * allowances["protocol-residue"],
        *[W + R_wrap] * allowances["wrapped-call"],
        *[F + R_shape] * allowances["answer-shape"],
        *[F + R_claim] * allowances["claim-contract"],
    ]
    tool_input = sum(S + k * step for k in range(tools))
    first_final = S + tools * step
    carried, retry_input = 0, 0
    for increment in increments:
        carried += increment
        retry_input += S + tools * step + carried
    # Charged by CLASS, because the classes no longer cost the same. A residue
    # reply is a lone tool_call object (t); a wrapped call is prose around one
    # (W, measured); a rejected or served answer is a full F. The old
    # `F * retries` happened to equal this only while `wrapped-call` did not
    # exist and residue was exactly one.
    output = (
        tools * t
        + t * allowances["protocol-residue"]
        + W * allowances["wrapped-call"]
        + F * (allowances["answer-shape"] + allowances["claim-contract"] + 1)
    )

    assert tool_input + first_final + retry_input + output == caps.max_total_tokens == 1_198_989
    # A 32768-window lane still serves every ORDINARY tool-reading request of
    # this run (the deepest costs S + 16*(t+C) = 25951 < 27033) — unlike the
    # Analyst, whose traversal outgrows that lane mid-run — but since
    # NE-CS-WHOLE-LADDER the full 17-call turn's FIRST-ANSWER request (27331)
    # no longer fits it; a turn spending 16 or fewer calls completes there.
    # NE-WRAPPED-CALL-FUNDED moved the retry worst case 48786 -> 62316; the
    # 262144-window primary lanes still serve it whole.
    largest = S + tools * step + carried
    assert largest == 62_316
    assert S + (tools - 1) * step == 25_951 < 27_033 < S + tools * step == 27_331
    assert largest < 224_460


def test_the_overdue_credit_review_token_budget_is_the_number_its_workload_costs() -> None:
    """S9: the agentic Meridian Overdue Credit Review's budget is DERIVED, not copied.

    Same method and constants as the Analyst's and Customer Support's pins
    above; its OWN S and its OWN counted ceiling. S = 2883 is measured
    ABSOLUTELY on this tree (registry comment, 2026-08-28) with the repo's own
    renderers — the one composed verified-read ToolSpec's catalog message with
    the structured runner's final-answer clause (1744 chars), pydantic-ai's
    prompted-output schema instructions for _StructuredAgenticCompletion (3549),
    the transport tail (2505 with its joining newlines), the 500-char manifest
    instructions and the 150-char question allowance: 8448 chars / 2.93 = 2883.
    The ceiling is counted, not estimated: ONE argument-free verified read
    returns the complete served answer, plus the same +2 refinement headroom the
    other two derivations use = 3 tool calls, 9 model requests. Equality, not
    `<=`, for the same reason as the pins above: the manifest number must be
    what this workload costs.
    """
    S, C, t, F = 2883, 1300, 80, 4000
    R_res, R_shape, R_claim = 172, 242, 403
    W, R_wrap = 439, 347

    overdue = next(
        definition
        for definition in _agentic_definitions()
        if definition.label == "Meridian Overdue Credit Review"
    )
    caps = enforced_agentic_caps(overdue.budgets, overdue.tool_grants)
    tools = caps.max_tool_calls
    assert tools == 3
    # One read grant and no write grants: nothing to park.
    assert caps.max_parked_writes == 0
    retries = caps.max_model_requests - tools - 1
    assert retries == output_retries(tools)
    assert caps.max_model_requests == 13
    # `max_steps` (9) is NOT the request ceiling and never was — it narrows the
    # tool ceiling and sets the clock. Since NE-WRAPPED-CALL-FUNDED the request
    # ceiling is `2*max_tool_calls + 7` = 13, so the clock funds 9 of the 13
    # permitted requests; the registry block states that residual — and says
    # plainly that the CLOCK is what stops a wrap-every-rung run — rather than
    # moving a governed number.
    assert caps.wall_clock_seconds == float(overdue.budgets.max_steps * 10) == 90.0

    allowances = dict(output_retry_allowances(tools))
    step = t + C
    increments = [
        *[t + R_res] * allowances["protocol-residue"],
        *[W + R_wrap] * allowances["wrapped-call"],
        *[F + R_shape] * allowances["answer-shape"],
        *[F + R_claim] * allowances["claim-contract"],
    ]
    tool_input = sum(S + k * step for k in range(tools))
    first_final = S + tools * step
    carried, retry_input = 0, 0
    for increment in increments:
        carried += increment
        retry_input += S + tools * step + carried
    # Charged by CLASS, because the classes no longer cost the same. A residue
    # reply is a lone tool_call object (t); a wrapped call is prose around one
    # (W, measured); a rejected or served answer is a full F. The old
    # `F * retries` happened to equal this only while `wrapped-call` did not
    # exist and residue was exactly one.
    output = (
        tools * t
        + t * allowances["protocol-residue"]
        + W * allowances["wrapped-call"]
        + F * (allowances["answer-shape"] + allowances["claim-contract"] + 1)
    )

    assert tool_input + first_final + retry_input + output == caps.max_total_tokens == 191_221
    # ⚠ NE-SHAPE-ALLOWANCE-4 FALSIFIED THIS AGENT'S LANE CLAIM, and the pin
    # records the new truth rather than dropping the check. It used to hold that
    # every request of this run — the retry worst case included — fitted EVERY
    # lane the gateway routes. The extra `answer-shape` retry pushed the largest
    # single request (the final retry) past a 32768-window lane's 27033 input
    # allowance, and NE-WRAPPED-CALL-FUNDED pushed it further (28478 -> 31004).
    # Everything short of it still fits there: the deepest ordinary tool-reading
    # request, the first answer, and the retry stage through its eighth request.
    # The 262144-window primary lanes still serve the worst case.
    largest = S + tools * step + carried
    assert largest == 31_004
    assert S + (tools - 1) * step == 5_643
    assert first_final == 7_023
    # The last request that still fits a 32768-window lane is the EIGHTH retry —
    # the last `answer-shape` one, before the claim-contract increment lands.
    eighth_retry = S + tools * step + carried - (F + R_claim)
    assert eighth_retry == 26_601 < 27_033 < largest < 224_460


def test_the_grounded_analyst_token_budget_is_the_number_its_workload_costs() -> None:
    """NE-SHAPE-ALLOWANCE-4: the fourth derived budget, which had NO pin at all.

    The Grounded Analyst's `max_input_tokens` was derived by exactly the method
    the three pins above use (its registry block says so and itemises S = 3026
    bottom-up on this tree), but nothing asserted it. That gap is why the
    `answer-shape` raise was first costed against three agents instead of four:
    reading the declared numbers alone, 131523 looks like headroom over the
    Overdue Credit Review's 114710, when in fact it is this agent's OWN 5-retry
    derivation to the token and was equally unfunded.

    Pinned as an EQUALITY for the same reason as the others: the manifest number
    must be what this workload costs, so a future allowance change fails here
    until it is re-derived rather than rounded.
    """
    S, C, t, F = 3026, 1300, 80, 4000
    R_res, R_shape, R_claim = 172, 242, 403
    W, R_wrap = 439, 347

    grounded = next(
        definition
        for definition in _agentic_definitions()
        if definition.label == "Meridian Grounded Analyst"
    )
    caps = enforced_agentic_caps(grounded.budgets, grounded.tool_grants)
    tools = caps.max_tool_calls
    assert tools == 4
    # Two read grants and no write grants: nothing to park.
    assert caps.max_parked_writes == 0
    retries = caps.max_model_requests - tools - 1
    assert retries == output_retries(tools)

    allowances = dict(output_retry_allowances(tools))
    step = t + C
    increments = [
        *[t + R_res] * allowances["protocol-residue"],
        *[W + R_wrap] * allowances["wrapped-call"],
        *[F + R_shape] * allowances["answer-shape"],
        *[F + R_claim] * allowances["claim-contract"],
    ]
    assert len(increments) == retries
    tool_input = sum(S + k * step for k in range(tools))
    first_final = S + tools * step
    carried, retry_input = 0, 0
    for increment in increments:
        carried += increment
        retry_input += S + tools * step + carried
    # Charged by CLASS, because the classes no longer cost the same. A residue
    # reply is a lone tool_call object (t); a wrapped call is prose around one
    # (W, measured); a rejected or served answer is a full F. The old
    # `F * retries` happened to equal this only while `wrapped-call` did not
    # exist and residue was exactly one.
    output = (
        tools * t
        + t * allowances["protocol-residue"]
        + W * allowances["wrapped-call"]
        + F * (allowances["answer-shape"] + allowances["claim-contract"] + 1)
    )

    assert tool_input + first_final + retry_input + output == caps.max_total_tokens == 230_437
    # ⚠ The same lane claim NE-SHAPE-ALLOWANCE-4 falsified for the Overdue Credit
    # Review: this block used to be able to say every request fitted every lane.
    # The retry worst case no longer fits a 32768-window lane; everything short
    # of it — including the first answer — still does.
    largest = S + tools * step + carried
    assert largest == 33_313
    assert first_final == 8_546 < 27_033 < largest < 224_460


def test_the_worst_single_request_is_the_number_the_registry_states() -> None:
    """The per-request figure the fallback-lane paragraph turns on, pinned.

    `max_total_tokens` is cumulative; a single request is bounded by the LANE's
    input budget instead, and the registry's derivation now records that the
    worst one no longer fits a 32768-window lane. That claim is only worth
    anything if the number behind it is checked, so it is checked here — and a
    change that moves it fails until the paragraph is re-read.
    """
    S, C, t, F = 3358, 1300, 80, 4000
    R_res, R_shape, R_claim = 172, 242, 403
    W, R_wrap = 439, 347

    analyst = next(
        definition
        for definition in _agentic_definitions()
        if definition.label == "Meridian Analyst"
    )
    caps = enforced_agentic_caps(analyst.budgets, analyst.tool_grants)
    allowances = dict(output_retry_allowances(caps.max_tool_calls))
    increments = (
        allowances["protocol-residue"] * (t + R_res)
        + allowances["wrapped-call"] * (W + R_wrap)
        + allowances["answer-shape"] * (F + R_shape)
        + allowances["claim-contract"] * (F + R_claim)
    )
    largest = S + caps.max_tool_calls * (t + C) + increments

    assert largest == 98_625
    # Served whole on the lane the Analyst is primaried on for the live demo
    # (262144 window, 24576 reserve, 13108 margin).
    assert largest < 262_144 - 24_576 - 13_108 == 224_460
    # And it is the FORTIETH retry that sets it since NE-WRAPPED-CALL-FUNDED:
    # the `wrapped-call` allowance is the LADDER, so it contributes 34
    # (W + R_wrap) increments and the figure the registry's lane paragraph
    # quotes moved 71733 -> 98625. Read off the RESOLVED allowances, so both a
    # re-partitioned pool and a re-sized ladder re-derive here.
    assert allowances["answer-shape"] == 4
    assert allowances["wrapped-call"] == caps.max_tool_calls == 34
    assert increments == 48_347
    # NOT served whole on a 32768-window lane -- the registry says so plainly
    # rather than implying headroom that is gone.
    assert largest > 32_768 - 4_096 - 1_639 == 27_033
    # NE-MULTIHOP-FANOUT made that a change in KIND, not a shrinking margin, and
    # the registry's lane paragraph turns on saying so: at 34 tool calls an
    # ORDINARY tool-reading request passes 27033 too, so such a run stops being
    # servable on a 32768-window lane MID-TRAVERSAL rather than only in the retry
    # worst case. Request k costs S + (k-1)(t+C); the first one over is the 19th.
    over = next(k for k in range(1, caps.max_tool_calls + 1) if S + (k - 1) * (t + C) > 27_033)
    assert over == 19
    assert S + 18 * (t + C) == 28_198


def test_a_runaway_run_is_still_stopped_by_the_ceilings_it_was_re_sized_to() -> None:
    """Re-sizing the ceilings did not make the Agent unbounded.

    NE-MULTIHOP-FANOUT raised the tool-call ceiling from 10 to 34 and the clock
    from 120s to 400s because the DR-4830 question is 32 single-key reads deep.
    Raised is not removed: all three ceilings are still hard, still derived from
    the manifest in exactly one place, and a runaway run still stops — named —
    against them. The token budget was never the intended stop; it is re-derived
    to stay slack so the CALL ceilings are what a run actually meets.
    """
    analyst = next(
        definition
        for definition in _agentic_definitions()
        if definition.label == "Meridian Analyst"
    )
    caps = enforced_agentic_caps(analyst.budgets, analyst.tool_grants)

    assert caps.max_tool_calls == 34
    assert caps.wall_clock_seconds == 400.0
    assert caps.max_model_requests == caps.max_tool_calls + 1 + output_retries(caps.max_tool_calls)
    # `max_steps` narrows the tool ceiling and buys the clock; it is NOT the
    # request ceiling, and since NE-WRAPPED-CALL-FUNDED it is far below it
    # (2*34 + 7 = 75). The TOOL ceiling is still the binding one for a run that
    # is actually making progress, which is the property that matters; the wall
    # clock is what stops a run that spends its whole ladder on corrections.
    assert analyst.budgets.max_steps == 40 < caps.max_model_requests == 75
    assert caps.max_tool_calls == min(analyst.budgets.max_steps, analyst.budgets.max_tool_calls)
