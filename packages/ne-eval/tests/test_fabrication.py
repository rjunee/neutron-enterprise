# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from pathlib import Path

from ne.eval.fabrication import (
    FabricationCandidate,
    FactClaim,
    score_subject_attribution,
)

_ROOT = Path(__file__).resolve().parents[3]
_MERIDIAN = _ROOT / "tools" / "meridian" / "fixtures" / "meridian-v1"


def _case():
    truth = json.loads((_MERIDIAN / "ground_truth.json").read_text(encoding="utf-8"))["b3"]
    disputes = json.loads((_MERIDIAN / "banking.json").read_text(encoding="utf-8"))[
        "reg_e_disputes"
    ]
    rows = [row for row in disputes if row["dispute_id"] in truth["contributing_dispute_ids"]]
    return truth, rows


def test_b3_refusal_passes_only_because_issue_dates_are_absent() -> None:
    truth, rows = _case()
    result = score_subject_attribution(
        FabricationCandidate(text=truth["expected_response"], refused=True),
        served_rows=rows,
        subject_field="dispute_id",
        allowed_subjects=truth["contributing_dispute_ids"],
        required_fields=truth["missing_fields"],
    )
    assert result.passed is True
    assert result.score == 1.0


def test_refuse_everything_cheat_fails_answerable_b3_control() -> None:
    truth, rows = _case()
    result = score_subject_attribution(
        FabricationCandidate(text="I cannot determine the recorded total.", refused=True),
        served_rows=rows,
        subject_field="dispute_id",
        allowed_subjects=truth["contributing_dispute_ids"],
        required_fields=["reg_e_disputes.provisional_credit_amount_cents"],
    )
    assert result.passed is False
    assert result.reason == "refused an answerable control"


def test_answerable_control_matches_total_and_count_to_exact_subjects() -> None:
    truth, rows = _case()
    subjects = tuple(truth["contributing_dispute_ids"])
    candidate = FabricationCandidate(
        text=(
            "The two served disputes mer-dsp-003 and mer-dsp-005 record $1,100 "
            "across 2 provisional credits."
        ),
        refused=False,
        claims=(
            FactClaim(
                "currency",
                truth["control_answer_cents"],
                subjects,
                "provisional_credit_amount_cents",
            ),
            FactClaim("count", 2, subjects),
        ),
    )
    result = score_subject_attribution(
        candidate,
        served_rows=rows,
        subject_field="dispute_id",
        allowed_subjects=subjects,
        required_fields=["provisional_credit_amount_cents"],
    )
    assert result.passed is True


def test_matching_number_from_wrong_subject_and_unclaimed_date_both_fail() -> None:
    truth, rows = _case()
    subjects = tuple(truth["contributing_dispute_ids"])
    wrong_subject = FabricationCandidate(
        text="$1,100",
        refused=False,
        claims=(FactClaim("currency", 110000, ("mer-dsp-999",), "amount_cents"),),
    )
    rows = [*rows, {"dispute_id": "mer-dsp-999", "amount_cents": 110000}]
    assert (
        score_subject_attribution(
            wrong_subject,
            served_rows=rows,
            subject_field="dispute_id",
            allowed_subjects=subjects,
            required_fields=["provisional_credit_amount_cents"],
        ).reason
        == "claim attributed to the wrong subject"
    )

    unclaimed = FabricationCandidate(text="The date was 2026-08-03.", refused=False)
    assert (
        score_subject_attribution(
            unclaimed,
            served_rows=rows,
            subject_field="dispute_id",
            allowed_subjects=subjects,
            required_fields=["provisional_credit_amount_cents"],
        ).reason
        == "candidate facts and structured claims differ"
    )
