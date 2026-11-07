# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Strict sidecar models for the fictional Meridian Bank corpus.

The banking domain intentionally stays outside the frozen generated ``ne-schema``
contracts.  These models are fixture/tool-local, reject unknown fields, and use
integer cents for every monetary value so serialization and derived answers are
exact on every supported Python runtime.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

PersonaId = Literal["ops", "approver", "frontline"]
PERSONA_IDS: tuple[PersonaId, ...] = ("ops", "approver", "frontline")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Persona(_Strict):
    persona_id: PersonaId
    display_name: str
    entitlements: list[str]


class ArtifactAcl(_Strict):
    allowed_personas: list[PersonaId]
    denied_personas: list[PersonaId]
    classification: Literal["internal", "restricted", "customer-facing"]

    @model_validator(mode="after")
    def _complete_matrix(self) -> ArtifactAcl:
        allowed = set(self.allowed_personas)
        denied = set(self.denied_personas)
        if allowed & denied:
            raise ValueError("artifact ACL allow/deny sets overlap")
        if allowed | denied != set(PERSONA_IDS):
            raise ValueError("artifact ACL must decide every Meridian persona")
        if not allowed:
            raise ValueError("artifact ACL must allow at least one persona")
        return self


class Address(_Strict):
    line1: str
    city: str
    county: str
    state: Literal["GA"]
    postal_code: str


class Household(_Strict):
    household_id: str
    household_name: str


class Customer(_Strict):
    customer_id: str
    household_id: str
    first_name: str
    last_name: str
    email: str
    address: Address
    status: Literal["active", "inactive"] = "active"


class DepositAccount(_Strict):
    account_id: str
    customer_id: str
    account_type: Literal["checking", "savings"]
    balance_cents: int
    status: Literal["open", "closed"]


class AutoLoan(_Strict):
    loan_id: str
    customer_id: str
    status: Literal["active", "paid_off", "repossession"]
    principal_balance_cents: int
    next_due_date: date


class DebitCard(_Strict):
    card_id: str
    customer_id: str
    deposit_account_id: str
    last_four: str
    status: Literal["active", "blocked", "closed"]


class PaymentSchedule(_Strict):
    payment_id: str
    loan_id: str
    due_date: date
    amount_cents: int
    status: Literal["scheduled", "paid", "cancelled"]


class ServicingTicket(_Strict):
    ticket_id: str
    customer_id: str
    loan_id: str | None
    category: Literal["hardship", "payment", "card-dispute", "repossession"]
    status: Literal["open", "pending", "closed"]
    opened_date: date
    subject: str


class ServicingNote(_Strict):
    note_id: str
    ticket_id: str
    customer_id: str
    loan_id: str | None
    note_type: Literal["contact", "hardship_offered", "repossession", "resolution"]
    created_date: date
    text: str


class Complaint(_Strict):
    complaint_id: str
    customer_id: str
    dispute_id: str | None
    status: Literal["open", "investigating", "resolved"]
    received_date: date
    summary: str


class RegEDispute(_Strict):
    dispute_id: str
    customer_id: str
    card_id: str
    status: Literal["open", "closed", "withdrawn"]
    notice_date: date
    amount_cents: int
    provisional_credit_amount_cents: int | None
    # Deliberately no provisional-credit issue date. B3 must refuse to infer it.


class FeePosting(_Strict):
    fee_posting_id: str
    customer_id: str
    deposit_account_id: str
    fee_code: str
    amount_cents: int
    posted_date: date
    reversed: bool = False


class DisasterDeclaration(_Strict):
    declaration_id: str
    state: Literal["GA"]
    declaration_date: date
    incident: str
    counties: list[str]
    scope_description: str
    source_url: str


class FeeScheduleRow(_Strict):
    fee_code: str
    fee_name: str
    amount_cents: int
    effective_date: date


class Artifact(_Strict):
    artifact_id: str
    title: str
    family: str
    version: str
    effective_date: date
    current: bool
    filename: str
    sharepoint_path: str
    media_type: str
    seed_stage: Literal["initial", "supersession"]
    acl: ArtifactAcl


class MeridianCorpus(_Strict):
    corpus_version: Literal["meridian-v1"]
    as_of_date: date
    personas: list[Persona]
    households: list[Household]
    customers: list[Customer]
    deposit_accounts: list[DepositAccount]
    auto_loans: list[AutoLoan]
    debit_cards: list[DebitCard]
    payment_schedules: list[PaymentSchedule]
    servicing_tickets: list[ServicingTicket]
    servicing_notes: list[ServicingNote]
    complaints: list[Complaint]
    reg_e_disputes: list[RegEDispute]
    fee_postings: list[FeePosting]
    disaster_declarations: list[DisasterDeclaration]
    fee_schedule_rows: list[FeeScheduleRow]
    artifacts: list[Artifact]


class B1Row(_Strict):
    customer_id: str
    county: str
    loan_id: str | None
    affected: bool
    actionable: bool
    basis: list[str]


class B1GroundTruth(_Strict):
    question: str
    declaration_id: str
    window_start: date
    window_end: date
    affected_rows: list[B1Row]
    actionable_rows: list[B1Row]
    distractor_rows: list[B1Row]


class B2VersionAnswer(_Strict):
    artifact_id: str
    version: str
    current: bool
    threshold_cents: int | None
    deadline_business_days: int
    answer_dispute_ids: list[str]
    basis: list[str]


class B2GroundTruth(_Strict):
    question: str
    answers_by_version: list[B2VersionAnswer]
    current_artifact_id: str
    current_answer_dispute_ids: list[str]


class B3GroundTruth(_Strict):
    question: str
    answerable: Literal[False]
    expected_response: str
    missing_fields: list[str]
    control_question: str
    control_answer_cents: int
    contributing_dispute_ids: list[str]


class GroundTruth(_Strict):
    corpus_version: Literal["meridian-v1"]
    generated_from: list[str]
    b1: B1GroundTruth
    b2: B2GroundTruth
    b3: B3GroundTruth


class CorpusManifest(_Strict):
    corpus_version: Literal["meridian-v1"]
    seed: int
    generator: str
    as_of_date: date
    counts: dict[str, int]
    sha256: dict[str, str]
