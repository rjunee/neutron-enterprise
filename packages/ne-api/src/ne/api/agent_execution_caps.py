# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The caps an agentic Agent's Runs ACTUALLY enforce, derived in exactly one place.

WHY THIS MODULE EXISTS. A manifest declares four budget numbers. None of the four
is enforced as written: the tool-call ceiling is the MIN of two of them, the
model-request ceiling is that min plus one, the wall clock is ``max_steps`` times
ten seconds, and the token ceiling is the two token budgets added together.
``max_steps`` in particular is enforced as NOTHING on its own — it narrows the
tool-call ceiling and it sets the clock, and no counter anywhere is ever compared
against 12.

The composition root derived those numbers inline while the console printed the
raw manifest numbers beside the word "Caps", so a reader was shown a governed
ceiling that no counter was ever measured against. This module is the one
derivation; :mod:`ne.api.platform` builds the executing adapter from it and
:mod:`ne.api.agents_wire` serves the SAME values to the console, so the number on
the screen is the number the run is held to by construction rather than by two
implementations agreeing.

Nothing here is a policy decision. Every rule below already existed at its
enforcement site; this only stops it existing twice.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ne.api.agent_manifest import AgentBudgets
from ne.runtime.runner import model_request_limit

#: Seconds of wall clock each declared step buys an agentic Run.
_SECONDS_PER_STEP = 10


def enforced_wall_clock_seconds(budgets: AgentBudgets) -> float:
    """The wall clock ``ne.runtime.runner`` hard-cancels one attempt at.

    Split out of :func:`enforced_agentic_caps` so a caller that needs only the
    clock -- :mod:`ne.api.run_credential_floor`, which must know how long an
    admitted Run is PERMITTED to take -- reads the same derivation rather than
    re-multiplying ``max_steps`` by ten somewhere else. Still not a policy
    decision: this is the number the runner already enforces.
    """

    return float(budgets.max_steps * _SECONDS_PER_STEP)


@dataclass(frozen=True, slots=True)
class EnforcedAgenticCaps:
    """What a bounded agentic Run is actually measured against, per attempt.

    Every field is a ceiling some counter is compared to inside
    :func:`ne.runtime.runner.run_model_planned_agent`. A manifest number that
    only narrows another ceiling does not appear here, because a reader shown a
    number expects a counter behind it.
    """

    #: Governed Tool calls. Hard-refused at the recording executor and, one step
    #: earlier, by pydantic-ai's own ``tool_calls_limit``.
    max_tool_calls: int
    #: Model requests, including the final-answer request after the last call.
    max_model_requests: int
    #: The turn's wall clock, in seconds.
    wall_clock_seconds: float
    #: Input plus output tokens, measured as one budget.
    max_total_tokens: int
    #: Unsent writes the run may park.
    max_parked_writes: int


def enforced_agentic_caps(budgets: AgentBudgets, tool_grants: Sequence[str]) -> EnforcedAgenticCaps:
    """Derive the enforced ceilings from a manifest's declared budgets and grants."""
    max_tool_calls = min(budgets.max_steps, budgets.max_tool_calls)
    return EnforcedAgenticCaps(
        max_tool_calls=max_tool_calls,
        max_model_requests=model_request_limit(max_tool_calls),
        wall_clock_seconds=enforced_wall_clock_seconds(budgets),
        max_total_tokens=budgets.max_input_tokens + budgets.max_output_tokens,
        max_parked_writes=min(
            budgets.max_tool_calls,
            sum(not grant.startswith("read.") for grant in tool_grants),
        ),
    )
