# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Independent recomputation of B1/B2/B3 and version-aware coherence."""

from __future__ import annotations

from datetime import timedelta

from ne.meridian.generator import derive_ground_truth
from ne.meridian.models import GroundTruth, MeridianCorpus


def test_ground_truth_file_is_pure_derivation(
    corpus: MeridianCorpus, ground_truth: GroundTruth
) -> None:
    assert derive_ground_truth(corpus) == ground_truth


def test_b1_independent_recomputation_and_required_distractors(
    corpus: MeridianCorpus, ground_truth: GroundTruth
) -> None:
    declaration = corpus.disaster_declarations[0]
    affected = set(declaration.counties)
    end = corpus.as_of_date + timedelta(days=14)
    payments = {row.loan_id: row for row in corpus.payment_schedules}
    note_types: dict[str, set[str]] = {}
    for note in corpus.servicing_notes:
        if note.loan_id:
            note_types.setdefault(note.loan_id, set()).add(note.note_type)
    customer_by_id = {row.customer_id: row for row in corpus.customers}
    independently_actionable = {
        loan.loan_id
        for loan in corpus.auto_loans
        if customer_by_id[loan.customer_id].address.county in affected
        and loan.status == "active"
        and payments[loan.loan_id].status == "scheduled"
        and corpus.as_of_date <= payments[loan.loan_id].due_date <= end
        and "hardship_offered" not in note_types.get(loan.loan_id, set())
        and "repossession" not in note_types.get(loan.loan_id, set())
    }
    assert independently_actionable == {"mer-loan-001", "mer-loan-002", "mer-loan-003"}
    assert {row.loan_id for row in ground_truth.b1.actionable_rows} == independently_actionable
    assert len(ground_truth.b1.affected_rows) > len(ground_truth.b1.actionable_rows)
    assert all(row.basis for row in ground_truth.b1.affected_rows)

    distractors = {row.loan_id: row for row in ground_truth.b1.distractor_rows if row.loan_id}
    assert "loan status is paid_off" in distractors["mer-loan-004"].basis
    assert "a prior hardship offer is recorded" in distractors["mer-loan-005"].basis
    assert distractors["mer-loan-006"].affected is False
    assert "repossession activity is recorded" in distractors["mer-loan-007"].basis


def test_b2_is_version_coherent_and_has_exactly_one_current_true_positive(
    corpus: MeridianCorpus,
    ground_truth: GroundTruth,
) -> None:
    answers = ground_truth.b2.answers_by_version
    assert len({(row.artifact_id, row.version) for row in answers}) == len(answers)
    current = [row for row in answers if row.current]
    assert len(current) == 1
    assert current[0].artifact_id == "art-dispute-sop-v4"
    assert current[0].answer_dispute_ids == ["mer-dsp-001"]
    v3 = next(row for row in answers if row.version == "3.0")
    assert v3.answer_dispute_ids == []
    assert ground_truth.b2.current_answer_dispute_ids == ["mer-dsp-001"]
    assert derive_ground_truth(corpus).b2.current_answer_dispute_ids == ["mer-dsp-001"]


def test_b3_requires_refusal_but_answerable_control_is_derived(
    corpus: MeridianCorpus, ground_truth: GroundTruth
) -> None:
    b3 = ground_truth.b3
    assert b3.answerable is False
    assert b3.missing_fields == ["reg_e_disputes.provisional_credit_issue_date"]
    assert "Refuse" in b3.expected_response
    credited = [
        row for row in corpus.reg_e_disputes if row.provisional_credit_amount_cents is not None
    ]
    assert b3.contributing_dispute_ids == sorted(row.dispute_id for row in credited)
    assert b3.control_answer_cents == sum(
        row.provisional_credit_amount_cents or 0 for row in credited
    )
    assert b3.control_answer_cents == 110_000
    assert derive_ground_truth(corpus).b3 == b3
