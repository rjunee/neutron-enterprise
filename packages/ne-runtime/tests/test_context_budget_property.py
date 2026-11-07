# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-CTXB-3 DoD (a): the OVERFLOW-INVARIANT property test (design §3.5, §7.1).

Seeded (``random.Random``, NO hypothesis dependency) fuzz over ~200 randomized
histories — random turn counts, random per-part byte sizes incl. multi-KB tool
returns, random tool-call/return/retry placement — plus dedicated oversized
current-turn cases (a giant ``ToolReturnPart`` in the suffix). For EVERY
``request()`` call: the assembled request the gateway receives satisfies
``estimate ≤ input_budget`` OR the call raised the typed
``ContextBudgetExceededError``. The current turn's tool-return content is NEVER
absent from a SENT request — an oversized single turn always RAISES, it is never
silently truncated. ``dropped_turns`` in the audit is monotone-consistent with
the kept content.
"""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Mapping
from typing import Any

from pydantic_ai.messages import InstructionPart, ModelMessage
from pydantic_ai.models import ModelRequestParameters

from ne.contracts.errors import ContextBudgetExceededError
from ne.runtime.config import RuntimeSettings
from ne.runtime.model import GatewayModel

from .conftest import (
    RecordingAuditSink,
    RecordingGateway,
    estimate_request,
    make_budget,
    req_first,
    req_retry,
    req_tool_return,
    req_user,
    resp_text,
    resp_tool_call,
    text_completion,
)

_INPUT_BUDGET = 3000
_BUDGET = make_budget(
    context_window=_INPUT_BUDGET + 100 + 1024, max_output_tokens=100, overhead_margin=1024
)


async def _noop_hook(name: str, args: Mapping[str, Any]) -> None:  # pragma: no cover - unused
    return None


def _blob(rng: random.Random, lo: int, hi: int) -> str:
    return "z" * rng.randint(lo, hi)


def _random_prior_turns(rng: random.Random) -> list[ModelMessage]:
    msgs: list[ModelMessage] = []
    n = rng.randint(0, 5)
    for i in range(n):
        user = f"PRIOR{i} " + _blob(rng, 10, 2000)
        if rng.random() < 0.5:
            # tool turn: user → tool_call → tool return → answer (pairing must hold)
            msgs.append(req_user(user))
            msgs.append(resp_tool_call("read.docs.search", {"q": f"q{i}"}))
            msgs.append(req_tool_return("read.docs.search", "R" + _blob(rng, 10, 2000)))
            if rng.random() < 0.7:
                msgs.append(resp_text(f"ans{i}"))
        else:
            msgs.append(req_user(user))
            msgs.append(resp_text(f"ans{i} " + _blob(rng, 0, 500)))
        # occasional stray retry prompt inside the turn stream
        if rng.random() < 0.2:
            msgs.append(req_retry("please retry " + _blob(rng, 0, 300)))
    return msgs


def _run_once(rng: random.Random, *, force_oversized_current: bool) -> bool:
    """Run one fuzzed request; return True iff it raised the typed refusal."""
    prior = _random_prior_turns(rng)
    # The current turn (protected SUFFIX): a user prompt + optionally a tool
    # call/return whose content carries a UNIQUE sentinel that must NEVER be
    # dropped. When oversized, the whole assembly cannot fit and MUST raise.
    cur_user = f"CURRENT-{rng.randint(0, 1_000_000)}"
    current: list[ModelMessage] = [req_user(cur_user)]
    ret_sentinel: str | None = None
    if force_oversized_current or rng.random() < 0.6:
        ret_sentinel = f"CURRET-{rng.randint(0, 1_000_000)}"
        # Oversized: > _INPUT_BUDGET tokens (>~4500 bytes / 1.5) even with an empty
        # head, so the assembly can never fit and MUST raise.
        size = rng.randint(9000, 14000) if force_oversized_current else rng.randint(10, 2500)
        current.append(resp_tool_call("read.docs.search", {"q": "z"}))
        current.append(req_tool_return("read.docs.search", ret_sentinel + " " + "z" * size))

    system = "SYS " + _blob(rng, 0, 400)
    if prior:
        # Fold the system prompt into the first prior request so head-splitting
        # (system → head, user → group 0) is exercised on every non-empty history.
        messages: list[ModelMessage] = [
            req_first(system, prior_user_of(prior)),
            *prior_tail(prior),
            *current,
        ]
    else:
        from .conftest import req_system

        messages = [req_system(system), *current]

    gw = RecordingGateway([text_completion("done")])
    sink = RecordingAuditSink()
    model = GatewayModel(
        gw, RuntimeSettings(), on_unknown_tool=_noop_hook, budget=_BUDGET, sink=sink
    )
    params = ModelRequestParameters(
        function_tools=[], instruction_parts=[InstructionPart(content="INSTR")]
    )

    raised = False
    try:
        asyncio.run(model.request(list(messages), None, params))
    except ContextBudgetExceededError as exc:
        raised = True
        assert exc.stage == "runtime-trim"
        # A raise emits exactly one error-outcome context.budget event; nothing sent.
        assert gw.requests == []
        assert len(sink.with_action("context.budget")) == 1

    if raised:
        return True

    # Did NOT raise ⇒ the SENT request fits the input budget (lockstep re-estimate).
    sent = gw.requests[-1]
    assert estimate_request(sent) <= _INPUT_BUDGET
    joined = "\n".join(m.content for m in sent.messages)
    # The current turn's prompt AND tool return are structurally un-trimmable.
    assert cur_user in joined
    if ret_sentinel is not None:
        assert ret_sentinel in joined
    # dropped_turns is monotone-consistent: it never exceeds the prior-turn count,
    # and any surviving PRIORk implies no LATER prior turn was dropped (oldest-first).
    events = sink.with_action("context.budget")
    prior_turns = sum(1 for m in prior if _is_user_request(m))
    if events:
        dropped = json.loads(events[0].detail)["dropped_turns"]
        assert 0 <= dropped <= prior_turns
    _assert_oldest_first(joined, prior_turns)
    return False


def _is_user_request(m: ModelMessage) -> bool:
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    return isinstance(m, ModelRequest) and any(isinstance(p, UserPromptPart) for p in m.parts)


def prior_user_of(prior: list[ModelMessage]) -> str:
    # The first prior message is always a user request (built that way).
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    first = prior[0]
    assert isinstance(first, ModelRequest)
    for p in first.parts:
        if isinstance(p, UserPromptPart):
            return str(p.content)
    return "T0"


def prior_tail(prior: list[ModelMessage]) -> list[ModelMessage]:
    return list(prior[1:])


def _assert_oldest_first(joined: str, prior_turns: int) -> None:
    # Surviving PRIOR sentinels must be a CONTIGUOUS NEWEST suffix: if PRIORk is
    # present then every PRIORj (j > k) is present too (oldest dropped first).
    present = [k for k in range(prior_turns) if f"PRIOR{k}" in joined]
    if present:
        lo = min(present)
        assert present == list(range(lo, prior_turns))


def test_overflow_invariant_property() -> None:
    rng = random.Random(20260707)
    for _ in range(200):
        _run_once(rng, force_oversized_current=False)


def test_oversized_current_turn_always_raises_never_truncates() -> None:
    # Dedicated oversized-current-turn cases: a giant current tool return alone
    # exceeds the budget, so the call MUST raise — the tool output is NEVER
    # silently truncated into a sent request (design §3.5-2 / §0-law-5).
    rng = random.Random(999)
    for _ in range(40):
        assert _run_once(rng, force_oversized_current=True) is True
