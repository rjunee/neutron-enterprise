# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""S9 — the kill shot: Meridian Overdue Credit Review (private-default).

The governed agent answers the SAME question S2 gets wrong — through ONE
argument-free verified read whose deadline is a load-time-materialised fact,
not per-query arithmetic. The acceptance bar (ASK_SCENARIOS S9, rehearsed 4/4
on train-60, re-confirmed train-62):

* anchors **mer-dsp-001 ALONE** — any other dispute id claimed overdue is a
  regression of the deterministic lane and fails immediately (never retried);
* exactly ONE tool call (``read.kg.overdue_provisional_credit_disputes``);
* completes VERIFIED with a server-verified evidence record.

Known flake: the claim-binding no-span failure (~1 in 11, POST_DEMO #18). It
writes nothing and clears on re-run. The suite retries it WITH ATTRIBUTION up
to 3 attempts; three consecutive draws of a 1-in-11 event is p < 0.1%, so
exhausting the budget is reported as a rate regression, not bad luck.
"""

from __future__ import annotations

import pytest
from nelib.answers import mentioned_ids, positively_claimed_ids
from nelib.client import OVERDUE_CREDIT_REVIEW, NeutronClient, RunResult
from nelib.flakes import FlakeLedger, run_with_attribution

pytestmark = [pytest.mark.private_lane]

_QUESTION = "Which open debit-card disputes are overdue for provisional credit under the SOP?"


def test_capability_surface_is_one_governed_read(ops: NeutronClient) -> None:
    """The whole architecture in one row: exactly one tool, the verified read
    with the materialised deadline."""
    capabilities = ops.preflight(OVERDUE_CREDIT_REVIEW)["capabilities"]
    names = [str(tool.get("name", "")) for tool in capabilities.get("tools", [])]
    assert names == ["read.kg.overdue_provisional_credit_disputes"], (
        f"S9 agent capability surface drifted: {names} — the beat depends on this agent "
        "not being allowed to wander."
    )


@pytest.fixture(scope="module")
def s9(ops: NeutronClient, warm_box: None, flake_ledger: FlakeLedger) -> RunResult:
    del warm_box
    return run_with_attribution(
        "s9-kill-shot",
        lambda attempt: ops.run_turn(
            OVERDUE_CREDIT_REVIEW,
            _QUESTION,
            requested_model="private-default",
            label=f"s9-a{attempt}",
        ),
        ledger=flake_ledger,
        max_attempts=3,
        settle_ok=lambda r: r.status == "completed",
    )


def test_s9_completes_verified(s9: RunResult) -> None:
    assert s9.verdict == "verified", f"S9 verdict {s9.verdict!r}; answer: {s9.answer!r}"


def test_s9_anchors_mer_dsp_001_alone(s9: RunResult) -> None:
    claimed = positively_claimed_ids(s9.answer, r"mer-dsp-\d{3}")
    assert claimed == ["mer-dsp-001"], (
        f"S9 must anchor mer-dsp-001 ALONE; it claimed {claimed}. This is the deterministic "
        f"lane moving — a regression, never a flake. Answer: {s9.answer!r}"
    )
    # The served row contains only 001; other dispute ids should not even be
    # narrated (the agent may only narrate served cells).
    mentioned = mentioned_ids(s9.answer, r"mer-dsp-\d{3}")
    assert mentioned == ["mer-dsp-001"], (
        f"S9 narrated dispute ids beyond its served row: {mentioned} — the agent "
        f"editorialised beyond served evidence. Answer: {s9.answer!r}"
    )


def test_s9_used_exactly_one_tool_call(s9: RunResult) -> None:
    assert s9.tool_calls == ["read.kg.overdue_provisional_credit_disputes"], (
        f"S9 must make exactly one governed read; observed: {s9.tool_calls}"
    )


def test_s9_ran_on_the_private_lane(s9: RunResult) -> None:
    assert s9.detail.get("requested_model") == "private-default"
