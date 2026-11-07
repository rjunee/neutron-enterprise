# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Offline self-test of the suite's own assertion machinery.

Runs with NO env config and NO live stack — it is the one module of this suite
a developer can (and should) run while changing the helpers:

    uv run pytest e2e/scenarios/test_00_selftest.py -q

It also runs first in every live invocation, so a broken helper fails the
suite before a single model dollar is spent.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from nelib.answers import (
    fabricated_total_hits,
    mentioned_ids,
    positively_claimed_ids,
    standard_tier_business_days,
)
from nelib.client import RunResult
from nelib.flakes import (
    CLAIM_BINDING_NO_SPAN,
    FRONTIER_REPLY_SCHEMA_REJECTION,
    FlakeLedger,
    classify_failure,
    run_with_attribution,
)

_S1_STYLE_ANSWER = (
    "Eligible loans are mer-loan-001, mer-loan-002 and mer-loan-003. "
    "mer-loan-005 is excluded because a fourteen-day extension was already offered and "
    "accepted. mer-loan-010's payment was already paid, and mer-loan-008 falls outside the "
    "inclusive window."
)


def test_positive_claims_respect_exclusion_context() -> None:
    claimed = positively_claimed_ids(_S1_STYLE_ANSWER, r"mer-loan-\d{3}")
    assert claimed == ["mer-loan-001", "mer-loan-002", "mer-loan-003"]
    assert mentioned_ids(_S1_STYLE_ANSWER, r"mer-loan-\d{3}") == [
        "mer-loan-001",
        "mer-loan-002",
        "mer-loan-003",
        "mer-loan-005",
        "mer-loan-010",
        "mer-loan-008",
    ]


def test_positive_claim_detected_when_no_exclusion_marker() -> None:
    wrong = "The overdue disputes are mer-dsp-001 and mer-dsp-003."
    assert positively_claimed_ids(wrong, r"mer-dsp-\d{3}") == ["mer-dsp-001", "mer-dsp-003"]


def test_fabricated_total_forms_are_caught() -> None:
    assert fabricated_total_hits("The total issued late is $1,100.00.") == ["$1,100", "1,100.00"]
    assert (
        fabricated_total_hits(
            "Neither row carries a populated credit_issue_date, so I cannot determine a total."
        )
        == []
    )


def test_standard_tier_number_parses_both_directions() -> None:
    a = (
        'The SOP states: "Standard tier: provide provisional credit within 10 business days." '
        'It also states: "Expedited tier: within 5 business days for disputes >= $500."'
    )
    b = a.replace("within 10 business days", "within 7 business days")
    assert standard_tier_business_days(a) == 10
    assert standard_tier_business_days(b) == 7


def _failed_run(reason: str) -> RunResult:
    return RunResult(
        run_id="run-x",
        conversation_id="conv-x",
        elapsed_s=1.0,
        detail={"status": "failed", "output_verdict": None, "failure_reason": reason},
        events=[],
        evidence={},
    )


def test_classifier_matches_known_signatures_only() -> None:
    assert (
        classify_failure(_failed_run("Recorded failure class: AgentOutputContractError."))
        is CLAIM_BINDING_NO_SPAN
    )
    assert (
        classify_failure(_failed_run("Recorded failure class: json_invalid."))
        is FRONTIER_REPLY_SCHEMA_REJECTION
    )
    assert classify_failure(_failed_run("Recorded failure class: ValueError.")) is None


def test_unknown_failure_is_never_retried(tmp_path: Path) -> None:
    ledger = FlakeLedger(path=tmp_path / "ledger.json")
    calls: list[int] = []

    def attempt(n: int) -> RunResult:
        calls.append(n)
        return _failed_run("Recorded failure class: ValueError.")

    with pytest.raises(AssertionError, match="regression, not a flake"):
        run_with_attribution(
            "selftest",
            attempt,
            ledger=ledger,
            max_attempts=3,
            settle_ok=lambda r: r.status == "completed",
        )
    assert calls == [1]  # exactly one attempt: no bare retry hid the break


def test_exhausted_known_flake_budget_reports_rate_regression(tmp_path: Path) -> None:
    ledger = FlakeLedger(path=tmp_path / "ledger.json")

    def attempt(_: int) -> RunResult:
        return _failed_run("Recorded failure class: AgentOutputContractError.")

    with pytest.raises(AssertionError, match="RATE REGRESSION"):
        run_with_attribution(
            "selftest",
            attempt,
            ledger=ledger,
            max_attempts=3,
            settle_ok=lambda r: r.status == "completed",
        )
    assert len(ledger.entries) == 3  # every attempt attributed, none hidden
