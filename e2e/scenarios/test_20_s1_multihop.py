# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""S1 — the multi-hop (frontier-default, the original P0).

Ground truth (ASK_SCENARIOS S1): eligible loans are EXACTLY mer-loan-001,
mer-loan-002, mer-loan-003.

* mer-loan-005 must be EXCLUDED — its servicing note records a hardship offer
  already made and accepted. This is the criterion a wrong run misses.
* The 14-day window is INCLUSIVE of 2026-08-18..2026-09-01. Prose date claims
  are too fragile to assert directly, so the window is asserted through its
  measurable consequences: mer-loan-008 (one day outside the window) and
  mer-loan-010 (payment already paid) must not be claimed eligible.
* ~6 tool.execute calls; the servicing-notes hop MUST be present — without it
  the exclusion criterion was never even read.

Lane: frontier-default ONLY (2/2 correct incl. the prior-offer criterion;
private-default was measured returning VERIFIED-and-wrong ~1 in 3 — the badge
cannot catch reasoning-over-correct-rows, which is why this scenario asserts
content). Known flake: ~1 in 3 warm runs fails with a reply-schema rejection
and NO answer — retried with attribution, never silently.
"""

from __future__ import annotations

import pytest
from nelib.answers import positively_claimed_ids
from nelib.client import MERIDIAN_ANALYST, NeutronClient, RunResult
from nelib.flakes import FlakeLedger, run_with_attribution

pytestmark = [pytest.mark.frontier]

_QUESTION = (
    "For DR-4830, which affected Meridian auto loans have an unpaid scheduled payment due "
    "within 14 days and remain eligible for a new hardship offer?"
)
_TRUTH = ["mer-loan-001", "mer-loan-002", "mer-loan-003"]


@pytest.fixture(scope="module")
def s1(ops: NeutronClient, warm_box: None, flake_ledger: FlakeLedger) -> RunResult:
    del warm_box
    return run_with_attribution(
        "s1-multihop",
        lambda attempt: ops.run_turn(
            MERIDIAN_ANALYST,
            _QUESTION,
            requested_model="frontier-default",
            label=f"s1-a{attempt}",
            deadline_s=420.0,
        ),
        ledger=flake_ledger,
        max_attempts=3,
        settle_ok=lambda r: r.status == "completed",
    )


def test_s1_completes_verified(s1: RunResult) -> None:
    assert s1.verdict == "verified", f"S1 verdict {s1.verdict!r}; answer: {s1.answer!r}"


def test_s1_names_exactly_the_three_eligible_loans(s1: RunResult) -> None:
    claimed = positively_claimed_ids(s1.answer, r"mer-loan-\d{3}")
    missing = [loan for loan in _TRUTH if loan not in claimed]
    assert not missing, (
        f"S1 answer omitted eligible loans {missing}. Claimed set: {claimed}. Answer: {s1.answer!r}"
    )


def test_s1_excludes_the_prior_offer_loan(s1: RunResult) -> None:
    """THE criterion: mer-loan-005 already accepted a hardship extension and
    must not be claimed eligible. A claim of 005 on a warm box is a real
    regression; on a cold box it is the connector-blackout signature (the SOP
    never retrieved) — the warmth gate ran before this scenario, so blackout
    has already been ruled out for this run."""
    claimed = positively_claimed_ids(s1.answer, r"mer-loan-\d{3}")
    assert "mer-loan-005" not in claimed, (
        "S1 claimed mer-loan-005 eligible despite its accepted prior hardship offer "
        "(servicing note mer-note-005). The box was warm (gate passed), so this is a "
        f"reasoning/retrieval regression, not a blackout. Answer: {s1.answer!r}"
    )


def test_s1_respects_the_inclusive_window_via_its_boundary_cases(s1: RunResult) -> None:
    claimed = positively_claimed_ids(s1.answer, r"mer-loan-\d{3}")
    over_claimed = [loan for loan in ("mer-loan-008", "mer-loan-010") if loan in claimed]
    assert not over_claimed, (
        f"S1 claimed window/payment boundary cases as eligible: {over_claimed} "
        f"(008 is one day outside the inclusive window; 010 is already paid). "
        f"Answer: {s1.answer!r}"
    )
    unexpected = [loan for loan in claimed if loan not in _TRUTH]
    assert not unexpected, f"S1 claimed loans outside the truth set: {unexpected}"


def test_s1_walked_the_chain_including_the_servicing_notes_hop(s1: RunResult) -> None:
    calls = s1.tool_calls
    assert any("servicing_note" in call for call in calls), (
        f"S1 never read servicing notes — the exclusion criterion was not even consulted. "
        f"Tool chain: {calls}"
    )
    assert any("payments_due" in call or "payment" in call for call in calls), (
        f"S1 never read the payment window. Tool chain: {calls}"
    )
    # ~6 calls measured; a wide bound catches a silently amputated plan without
    # flaking on harmless plan variation.
    assert 3 <= len(calls) <= 12, f"S1 tool-call count {len(calls)} far from the ~6 measured"


def test_s1_ran_on_the_frontier_lane(s1: RunResult) -> None:
    assert s1.detail.get("requested_model") == "frontier-default"
    assert 30.0 <= s1.elapsed_s <= 420.0, (
        f"S1 elapsed {s1.elapsed_s:.0f}s is outside any measured band (67-165s warm; "
        "sub-30s is the blackout signature: fast AND wrong)"
    )
