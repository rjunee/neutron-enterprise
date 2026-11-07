# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""S2 — the Meridian Analyst on the overdue-disputes question (private-default).

⛔⛔ THIS SCENARIO IS EXPECTED TO GET THE ANSWER WRONG, AND ITS WRONGNESS IS A
DEMO ASSET. DO NOT "FIX" IT. (Owner decision 2026-08-28, ASK_SCENARIOS S2/S9.)
The Analyst reasons about "5 business days" unaided and was measured 8-of-8
wrong on the private lane — that failure is the S9 contrast beat. Any change
that teaches the Analyst business-day arithmetic (a helper tool, a served
deadline column, prompt coaching) DESTROYS the beat.

So this module encodes S2 as:

1. A TRIPWIRE against someone fixing it: the Analyst's capability surface must
   NOT expose a materialised-deadline/overdue tool. That is deterministic and
   catches the "fix" at its only possible mechanism.
2. A liveness + form check: the run completes VERIFIED and claims at least one
   dispute — the beat needs a confident wrong answer, not a refusal.
3. NO correctness assertion in either direction. A single run being right is
   the measured ~1-in-8 rare draw, not a failure; a single run being wrong is
   the norm. The claimed set is recorded in the transcript + flake ledger so
   drift shows up across scheduled runs.
"""

from __future__ import annotations

import pytest
from nelib.answers import positively_claimed_ids
from nelib.client import MERIDIAN_ANALYST, NeutronClient, RunResult
from nelib.flakes import FlakeLedger, run_with_attribution

pytestmark = [pytest.mark.private_lane]

_QUESTION = "Which open debit-card disputes are overdue for provisional credit under the SOP?"
_TRUTH = {"mer-dsp-001"}  # for the informational note only — see the module docstring


def test_nobody_fixed_the_analysts_arithmetic(ops: NeutronClient) -> None:
    """The do-not-fix guard. The deterministic lane owns the materialised
    ``provisional_credit_deadline_date``; the Analyst must keep reasoning
    unaided. If this fails, someone granted the Analyst a deadline tool —
    revert it (see ASK_SCENARIOS S2 ⛔ before believing it is a bug)."""
    capabilities = ops.preflight(MERIDIAN_ANALYST)["capabilities"]
    names = [str(tool.get("name", "")) for tool in capabilities.get("tools", [])]
    offending = [
        name
        for name in names
        if "overdue_provisional_credit" in name or "deadline" in name or "business_day" in name
    ]
    assert not offending, (
        f"The Meridian Analyst has been granted deadline/overdue tooling {offending} — this "
        "destroys the S9 contrast beat (owner decision 2026-08-28). Revert the grant."
    )


@pytest.fixture(scope="module")
def s2(ops: NeutronClient, warm_box: None, flake_ledger: FlakeLedger) -> RunResult:
    del warm_box
    return run_with_attribution(
        "s2-expected-wrong",
        lambda attempt: ops.run_turn(
            MERIDIAN_ANALYST,
            _QUESTION,
            requested_model="private-default",
            label=f"s2-a{attempt}",
        ),
        ledger=flake_ledger,
        max_attempts=3,
        settle_ok=lambda r: r.status == "completed",
    )


def test_s2_completes_verified_with_a_confident_claim(
    s2: RunResult, flake_ledger: FlakeLedger
) -> None:
    assert s2.verdict == "verified", (
        f"S2 must complete VERIFIED (every cited value real — the wrongness is judgement, "
        f"which the badge cannot see); got {s2.verdict!r}"
    )
    claimed = positively_claimed_ids(s2.answer, r"mer-dsp-\d{3}")
    assert claimed, (
        "S2 named no disputes at all — the beat needs a confident (usually wrong) claim, "
        f"not silence. Answer: {s2.answer!r}"
    )
    # Record the draw; NEVER assert on correctness here (see module docstring).
    note = (
        "rare correct draw (~1 in 8 measured) — fine for one run; if scheduled runs are "
        "repeatedly exactly correct, suspect an unauthorised 'fix' (ASK_SCENARIOS S2 ⛔)"
        if set(claimed) == _TRUTH
        else "expected-wrong draw (the demo asset): claimed set differs from truth {mer-dsp-001}"
    )
    flake_ledger.record(
        scenario="s2-claimed-set",
        attempt=1,
        outcome=f"claimed={sorted(claimed)}",
        signature=None,
        run_id=s2.run_id,
        detail=note,
    )
