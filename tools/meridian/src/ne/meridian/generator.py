# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Deterministic Meridian corpus, binary artifacts, and derived ground truth."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Literal

from ne.acme.rand import DetRandom
from ne.meridian.assets import build_docx, build_pdf, canonical_json, csv_bytes
from ne.meridian.constants import DeclarationSpec
from ne.meridian.models import (
    PERSONA_IDS,
    Address,
    Artifact,
    ArtifactAcl,
    AutoLoan,
    B1GroundTruth,
    B1Row,
    B2GroundTruth,
    B2VersionAnswer,
    B3GroundTruth,
    Complaint,
    CorpusManifest,
    Customer,
    DebitCard,
    DepositAccount,
    DisasterDeclaration,
    FeePosting,
    FeeScheduleRow,
    GroundTruth,
    Household,
    MeridianCorpus,
    PaymentSchedule,
    Persona,
    PersonaId,
    RegEDispute,
    ServicingNote,
    ServicingTicket,
)
from ne.schema import (
    Account,
    AccountStatus,
    Document,
    DocumentType,
    OperationalRecordSet,
    Person,
    Ticket,
    TicketPriority,
    TicketStatus,
)

CORPUS_VERSION = "meridian-v1"
DEFAULT_SEED = 4830
GENERATOR_NAME = "ne-meridian 0.1.0"
AS_OF_DATE = date(2026, 8, 18)
ASSET_PREFIX = "assets/"
_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@dataclass(frozen=True)
class _PolicyVersion:
    artifact_id: str
    version: str
    current: bool
    expedited_threshold_cents: int | None
    expedited_deadline_business_days: int
    standard_deadline_business_days: int


_POLICIES = (
    _PolicyVersion("art-dispute-sop-v3", "3.0", False, None, 10, 10),
    _PolicyVersion("art-dispute-sop-v4", "4.0", True, 50_000, 5, 10),
)


def _acl(
    *allowed: PersonaId,
    classification: Literal["internal", "restricted", "customer-facing"] = "internal",
) -> ArtifactAcl:
    allowed_set = set(allowed)
    return ArtifactAcl(
        allowed_personas=[p for p in PERSONA_IDS if p in allowed_set],
        denied_personas=[p for p in PERSONA_IDS if p not in allowed_set],
        classification=classification,
    )


def _personas() -> list[Persona]:
    return [
        Persona(
            persona_id="ops",
            display_name="Deposit Operations Analyst",
            entitlements=[
                "bank.customer.read",
                "bank.servicing.read",
                "bank.dispute.read",
                "sharepoint.operations.read",
            ],
        ),
        Persona(
            persona_id="approver",
            display_name="Regulatory Approval Officer",
            entitlements=[
                "bank.customer.read",
                "bank.servicing.read",
                "bank.dispute.read",
                "bank.approval.read",
                "sharepoint.restricted.read",
            ],
        ),
        Persona(
            persona_id="frontline",
            display_name="Branch Service Representative",
            entitlements=[
                "bank.customer.read",
                "bank.account-summary.read",
                "sharepoint.frontline.read",
            ],
        ),
    ]


def _households() -> list[Household]:
    return [
        Household(household_id=f"mer-hh-{i:03d}", household_name=f"Meridian Household {i:03d}")
        for i in range(1, 11)
    ]


#: NE-CS-SEND (owner decision, 2026-08-27): mer-cus-001 (Avery Arbor) carries the
#: owner's REAL demo inbox instead of a synthetic address. The S5 outreach demo
#: resolves the recipient from governed source data (Salesforce Contact / the
#: fixture), and the deployment's exact send allowlist contains only this
#: address — a synthetic customer001@meridian.example recipient would force a
#: send-time recipient override, which is exactly the fakery the governed send
#: refuses. Every OTHER customer stays obviously synthetic
#: (customerNNN@meridian.example, pinned by test_schema_and_safety).
DEMO_STANDIN_EMAIL = "owner@example.com"


def _customer_email(i: int) -> str:
    return DEMO_STANDIN_EMAIL if i == 1 else f"customer{i:03d}@meridian.example"


def _customers(spec: DeclarationSpec) -> list[Customer]:
    first_names = (
        "Avery",
        "Jordan",
        "Morgan",
        "Taylor",
        "Casey",
        "Riley",
        "Cameron",
        "Quinn",
        "Parker",
        "Skyler",
        "Drew",
        "Reese",
        "Rowan",
        "Emerson",
        "Finley",
    )
    last_names = (
        "Arbor",
        "Beacon",
        "Cedar",
        "Dover",
        "Elm",
        "Fielding",
        "Grove",
        "Harbor",
        "Ivory",
        "Juniper",
        "Keystone",
        "Linden",
        "Meadow",
        "North",
        "Orchard",
    )
    c1, c2, c3 = spec.counties
    counties = (
        c1,
        c2,
        c3,
        c1,
        c2,
        spec.adjacent_county,
        c3,
        c1,
        spec.adjacent_county,
        c2,
        c3,
        spec.adjacent_county,
        c1,
        spec.adjacent_county,
        c2,
    )
    return [
        Customer(
            customer_id=f"mer-cus-{i:03d}",
            household_id=f"mer-hh-{((i - 1) // 2) + 1:03d}",
            first_name=first_names[i - 1],
            last_name=last_names[i - 1],
            email=_customer_email(i),
            address=Address(
                line1=f"{100 + i} Meridian Demo Way",
                city=f"Demo City {((i - 1) % 5) + 1}",
                county=counties[i - 1],
                state="GA",
                postal_code=f"30{i:03d}",
            ),
        )
        for i in range(1, 16)
    ]


def _deposit_accounts(rng: DetRandom) -> list[DepositAccount]:
    return [
        DepositAccount(
            account_id=f"mer-dep-{i:03d}",
            customer_id=f"mer-cus-{i:03d}",
            account_type="checking" if i % 3 else "savings",
            balance_cents=125_000 + i * 17_300 + rng.rand_int(0, 9_999),
            status="closed" if i == 15 else "open",
        )
        for i in range(1, 16)
    ]


def _auto_loans() -> list[AutoLoan]:
    definitions: tuple[
        tuple[int, Literal["active", "paid_off", "repossession"], int, date], ...
    ] = (
        (1, "active", 2_240_000, date(2026, 8, 20)),
        (2, "active", 1_875_000, date(2026, 8, 25)),
        (3, "active", 3_190_000, date(2026, 9, 1)),
        (4, "paid_off", 0, date(2026, 8, 22)),
        (5, "active", 980_000, date(2026, 8, 23)),
        (6, "active", 1_455_000, date(2026, 8, 24)),
        (7, "repossession", 2_710_000, date(2026, 8, 26)),
        (8, "active", 1_205_000, date(2026, 9, 2)),
        (9, "active", 875_000, date(2026, 8, 15)),
        (10, "active", 2_030_000, date(2026, 8, 29)),
    )
    return [
        AutoLoan(
            loan_id=f"mer-loan-{i:03d}",
            customer_id=f"mer-cus-{i:03d}",
            status=status,
            principal_balance_cents=balance,
            next_due_date=due,
        )
        for i, status, balance, due in definitions
    ]


def _cards(rng: DetRandom) -> list[DebitCard]:
    return [
        DebitCard(
            card_id=f"mer-card-{i:03d}",
            customer_id=f"mer-cus-{i:03d}",
            deposit_account_id=f"mer-dep-{i:03d}",
            last_four=f"{rng.rand_int(0, 9999):04d}",
            status="blocked" if i == 5 else ("closed" if i == 15 else "active"),
        )
        for i in range(1, 16)
    ]


def _payment_schedules(loans: list[AutoLoan]) -> list[PaymentSchedule]:
    statuses: dict[int, Literal["scheduled", "paid", "cancelled"]] = {
        4: "cancelled",
        10: "paid",
    }
    return [
        PaymentSchedule(
            payment_id=f"mer-pay-{i:03d}",
            loan_id=loan.loan_id,
            due_date=loan.next_due_date,
            amount_cents=31_500 + i * 1_250,
            status=statuses.get(i, "scheduled"),
        )
        for i, loan in enumerate(loans, start=1)
    ]


def _servicing() -> tuple[list[ServicingTicket], list[ServicingNote]]:
    categories = (
        "hardship",
        "hardship",
        "hardship",
        "payment",
        "hardship",
        "hardship",
        "repossession",
        "payment",
        "payment",
        "payment",
    )
    note_types = (
        "contact",
        "contact",
        "contact",
        "resolution",
        "hardship_offered",
        "contact",
        "repossession",
        "contact",
        "contact",
        "resolution",
    )
    notes_text = (
        "Customer reported storm disruption; no hardship option offered yet.",
        "Customer requested disaster assistance information; review pending.",
        "Outbound disaster-assistance contact requested; no offer recorded.",
        "Loan paid in full before the disaster review window.",
        "Fourteen-day payment extension already offered and accepted.",
        "Customer resides outside the declaration counties.",
        "Repossession workflow began before the declaration date.",
        "Next scheduled payment falls outside the fourteen-day window.",
        "Scheduled due date is before the review window.",
        "Payment due in window was already satisfied.",
    )
    tickets = [
        ServicingTicket(
            ticket_id=f"mer-tkt-{i:03d}",
            customer_id=f"mer-cus-{i:03d}",
            loan_id=f"mer-loan-{i:03d}",
            category=categories[i - 1],
            status="closed" if i in (4, 10) else "open",
            opened_date=date(2026, 8, min(5 + i, 17)),
            subject=f"Auto-loan servicing review {i:03d}",
        )
        for i in range(1, 11)
    ]
    notes = [
        ServicingNote(
            note_id=f"mer-note-{i:03d}",
            ticket_id=f"mer-tkt-{i:03d}",
            customer_id=f"mer-cus-{i:03d}",
            loan_id=f"mer-loan-{i:03d}",
            note_type=note_types[i - 1],
            created_date=date(2026, 8, min(6 + i, 18)),
            text=notes_text[i - 1],
        )
        for i in range(1, 11)
    ]
    return tickets, notes


def _disputes() -> list[RegEDispute]:
    definitions: tuple[
        tuple[int, Literal["open", "closed", "withdrawn"], date, int, int | None], ...
    ] = (
        (1, "open", date(2026, 8, 10), 65_000, None),
        (2, "open", date(2026, 8, 12), 80_000, None),
        (3, "open", date(2026, 8, 3), 20_000, 20_000),
        (4, "open", date(2026, 8, 4), 42_500, None),
        (5, "closed", date(2026, 7, 20), 90_000, 90_000),
        (6, "withdrawn", date(2026, 8, 1), 10_000, None),
    )
    return [
        RegEDispute(
            dispute_id=f"mer-dsp-{i:03d}",
            customer_id=f"mer-cus-{i:03d}",
            card_id=f"mer-card-{i:03d}",
            status=status,
            notice_date=notice,
            amount_cents=amount,
            provisional_credit_amount_cents=credit,
        )
        for i, status, notice, amount, credit in definitions
    ]


def _complaints() -> list[Complaint]:
    return [
        Complaint(
            complaint_id="mer-cmp-001",
            customer_id="mer-cus-001",
            dispute_id="mer-dsp-001",
            status="investigating",
            received_date=date(2026, 8, 17),
            summary="Customer asked when expedited provisional credit will post.",
        ),
        Complaint(
            complaint_id="mer-cmp-002",
            customer_id="mer-cus-003",
            dispute_id="mer-dsp-003",
            status="resolved",
            received_date=date(2026, 8, 8),
            summary="Customer requested confirmation of provisional-credit amount.",
        ),
        Complaint(
            complaint_id="mer-cmp-003",
            customer_id="mer-cus-011",
            dispute_id=None,
            status="open",
            received_date=date(2026, 8, 16),
            summary="Customer reported difficulty reaching disaster-assistance queue.",
        ),
    ]


def _fees() -> tuple[list[FeeScheduleRow], list[FeePosting]]:
    schedule = [
        FeeScheduleRow(
            fee_code="OD-PAID",
            fee_name="Paid overdraft item",
            amount_cents=3_500,
            effective_date=date(2026, 1, 1),
        ),
        FeeScheduleRow(
            fee_code="NSF-RETURN",
            fee_name="Returned item",
            amount_cents=0,
            effective_date=date(2026, 1, 1),
        ),
        FeeScheduleRow(
            fee_code="WIRE-DOM",
            fee_name="Domestic outgoing wire",
            amount_cents=2_500,
            effective_date=date(2026, 1, 1),
        ),
        FeeScheduleRow(
            fee_code="STOP-PAY",
            fee_name="Stop payment",
            amount_cents=3_000,
            effective_date=date(2026, 1, 1),
        ),
    ]
    postings = [
        FeePosting(
            fee_posting_id=f"mer-fee-{i:03d}",
            customer_id=f"mer-cus-{customer:03d}",
            deposit_account_id=f"mer-dep-{customer:03d}",
            fee_code=code,
            amount_cents=amount,
            posted_date=posted,
            reversed=reversed_,
        )
        for i, (customer, code, amount, posted, reversed_) in enumerate(
            (
                (1, "OD-PAID", 3_500, date(2026, 8, 2), False),
                (2, "WIRE-DOM", 2_500, date(2026, 8, 5), False),
                (3, "STOP-PAY", 3_000, date(2026, 8, 7), True),
                (4, "NSF-RETURN", 0, date(2026, 8, 9), False),
                (5, "OD-PAID", 3_500, date(2026, 8, 11), False),
            ),
            start=1,
        )
    ]
    return schedule, postings


def _artifacts() -> list[Artifact]:
    return [
        Artifact(
            artifact_id="art-dispute-sop-v3",
            title="Debit Card Dispute Handling SOP",
            family="debit-card-dispute-sop",
            version="3.0",
            effective_date=date(2025, 8, 1),
            current=False,
            filename="debit-card-dispute-handling-sop-v3.docx",
            sharepoint_path="Debit Card Dispute Handling SOP.docx",
            media_type=_DOCX_MIME,
            seed_stage="initial",
            acl=_acl("ops", "approver", classification="restricted"),
        ),
        Artifact(
            artifact_id="art-dispute-sop-v4",
            title="Debit Card Dispute Handling SOP",
            family="debit-card-dispute-sop",
            version="4.0",
            effective_date=AS_OF_DATE,
            current=True,
            filename="debit-card-dispute-handling-sop-v4.docx",
            sharepoint_path="Debit Card Dispute Handling SOP.docx",
            media_type=_DOCX_MIME,
            seed_stage="supersession",
            acl=_acl("ops", "approver", "frontline", classification="internal"),
        ),
        Artifact(
            artifact_id="art-reg-dd-2026",
            title="Regulation DD Deposit Account Disclosure",
            family="reg-dd-disclosure",
            version="2026",
            effective_date=date(2026, 1, 1),
            current=True,
            filename="reg-dd-disclosure-2026.pdf",
            sharepoint_path="Regulation DD Deposit Account Disclosure.pdf",
            media_type="application/pdf",
            seed_stage="initial",
            acl=_acl("approver", "frontline", classification="customer-facing"),
        ),
        Artifact(
            artifact_id="art-fees-2026",
            title="Consumer Deposit Fee Schedule",
            family="fee-schedule",
            version="2026",
            effective_date=date(2026, 1, 1),
            current=True,
            filename="fee-schedule-2026.json",
            sharepoint_path="Consumer Deposit Fee Schedule.json",
            media_type="application/json",
            seed_stage="initial",
            acl=_acl("ops", "approver", "frontline", classification="customer-facing"),
        ),
        Artifact(
            artifact_id="art-hardship-2026",
            title="Disaster Hardship Response SOP",
            family="hardship-sop",
            version="2026",
            effective_date=AS_OF_DATE,
            current=True,
            filename="disaster-hardship-response-sop-2026.md",
            sharepoint_path="Disaster Hardship Response SOP.md",
            media_type="text/markdown",
            seed_stage="initial",
            acl=_acl("ops", "approver", "frontline"),
        ),
        Artifact(
            artifact_id="art-collections-2025",
            title="Consumer Auto Collections Policy",
            family="collections-policy",
            version="2025",
            effective_date=date(2025, 1, 1),
            current=False,
            filename="consumer-auto-collections-policy-2025.md",
            sharepoint_path="Consumer Auto Collections Policy 2025.md",
            media_type="text/markdown",
            seed_stage="initial",
            acl=_acl("approver", classification="restricted"),
        ),
        Artifact(
            artifact_id="art-collections-2026",
            title="Consumer Auto Collections Policy",
            family="collections-policy",
            version="2026",
            effective_date=date(2026, 1, 1),
            current=True,
            filename="consumer-auto-collections-policy-2026.md",
            sharepoint_path="Consumer Auto Collections Policy.md",
            media_type="text/markdown",
            seed_stage="initial",
            acl=_acl("ops", "approver", classification="restricted"),
        ),
        Artifact(
            artifact_id="art-reg-e-bulletin-2025",
            title="Reg E Operations Bulletin",
            family="reg-e-operations-bulletin",
            version="2025",
            effective_date=date(2025, 6, 1),
            current=False,
            filename="reg-e-operations-bulletin-2025.md",
            sharepoint_path="Reg E Operations Bulletin 2025.md",
            media_type="text/markdown",
            seed_stage="initial",
            acl=_acl("approver", classification="restricted"),
        ),
        Artifact(
            artifact_id="art-reg-e-bulletin-2026",
            title="Reg E Operations Bulletin",
            family="reg-e-operations-bulletin",
            version="2026",
            effective_date=date(2026, 6, 1),
            current=True,
            filename="reg-e-operations-bulletin-2026.md",
            sharepoint_path="Reg E Operations Bulletin.md",
            media_type="text/markdown",
            seed_stage="initial",
            acl=_acl("ops", "approver", classification="restricted"),
        ),
        Artifact(
            artifact_id="art-disaster-counties-2026",
            title="Disaster Declaration County Reference",
            family="disaster-county-reference",
            version="2026",
            effective_date=AS_OF_DATE,
            current=True,
            filename="disaster-county-reference-2026.csv",
            sharepoint_path="Disaster Declaration County Reference.csv",
            media_type="text/csv",
            seed_stage="initial",
            acl=_acl("ops", "approver", "frontline"),
        ),
        Artifact(
            artifact_id="art-servicing-notes-2026",
            title="Servicing Note Quality Standard",
            family="servicing-note-standard",
            version="2026",
            effective_date=date(2026, 2, 1),
            current=True,
            filename="servicing-note-quality-standard-2026.md",
            sharepoint_path="Servicing Note Quality Standard.md",
            media_type="text/markdown",
            seed_stage="initial",
            acl=_acl("ops", "frontline"),
        ),
        Artifact(
            artifact_id="art-complaints-2026",
            title="Complaint Escalation Playbook",
            family="complaint-escalation",
            version="2026",
            effective_date=date(2026, 3, 1),
            current=True,
            filename="complaint-escalation-playbook-2026.md",
            sharepoint_path="Complaint Escalation Playbook.md",
            media_type="text/markdown",
            seed_stage="initial",
            acl=_acl("approver", "frontline", classification="restricted"),
        ),
    ]


def _artifact_bytes(
    artifacts: list[Artifact],
    fee_schedule: list[FeeScheduleRow],
    declaration: DisasterDeclaration,
) -> dict[str, bytes]:
    del artifacts  # Metadata and bytes are independently enumerated and cross-checked by tests.
    unchanged = [
        "Purpose: investigate debit-card error notices consistently and document every decision.",
        (
            "Scope: consumer debit cards and electronic fund transfer claims handled under "
            "Regulation E."
        ),
        "Open a case on the date Meridian receives customer notice.",
        (
            "Standard tier: provide provisional credit within 10 business days after customer "
            "notice when eligibility criteria are met."
        ),
        "Record the investigation outcome and customer communication in the servicing case.",
    ]
    v3 = build_docx(
        "Debit Card Dispute Handling SOP",
        [
            *unchanged,
            (
                "Documentation requirement: obtain a signed written statement before beginning "
                "the investigation."
            ),
            "Escalation: disputes of every amount follow the standard 10-business-day tier.",
        ],
        version="3.0",
        effective="2025-08-01",
    )
    v4 = build_docx(
        "Debit Card Dispute Handling SOP",
        [
            *unchanged,
            (
                "Expedited tier: for a dispute amount of at least $500, provide provisional credit "
                "within 5 business days after customer notice when eligibility criteria are met."
            ),
            (
                "Documentation requirement: oral notice starts the investigation; collect a "
                "signed customer attestation within 10 calendar days."
            ),
        ],
        version="4.0",
        effective="2026-08-18",
    )
    fees = canonical_json([row.model_dump(mode="json") for row in fee_schedule])
    county_rows = [
        ["declaration_id", "state", "county", "declaration_date", "scope"],
    ]
    county_rows.extend(
        [
            declaration.declaration_id,
            declaration.state,
            county,
            str(declaration.declaration_date),
            "meridian-pilot-selected-designated-county",
        ]
        for county in declaration.counties
    )
    return {
        "debit-card-dispute-handling-sop-v3.docx": v3,
        "debit-card-dispute-handling-sop-v4.docx": v4,
        "reg-dd-disclosure-2026.pdf": build_pdf(
            "Regulation DD Deposit Account Disclosure",
            [
                "Annual percentage yield and interest terms are disclosed before account opening.",
                "Paid overdraft item fee: $35.00. Returned item fee: $0.00.",
                "See the current Consumer Deposit Fee Schedule for all service fees.",
                "Synthetic Meridian Bank demonstration disclosure - not a real bank product.",
            ],
        ),
        "fee-schedule-2026.json": fees,
        "disaster-hardship-response-sop-2026.md": (
            "# Disaster Hardship Response SOP (2026)\n\n"
            f"For {declaration.declaration_id}, identify customers whose current address is in "
            f"{', '.join(declaration.counties)}. An auto loan is actionable only when it is "
            "active, "
            "has an unpaid scheduled payment due from the as-of date through 14 calendar days "
            "inclusive, has no prior hardship offer, and is not in repossession. Record the basis "
            "for every included and excluded loan.\n"
        ).encode(),
        "consumer-auto-collections-policy-2025.md": (
            b"# Consumer Auto Collections Policy (2025)\n\nCollectors begin outreach at day 5, "
            b"review hardship notes before escalation, and pause activity after a "
            b"declared-disaster "
            b"hardship offer. Repossession review begins at day 45.\n"
        ),
        "consumer-auto-collections-policy-2026.md": (
            b"# Consumer Auto Collections Policy (2026)\n\nCollectors begin outreach at day 5, "
            b"review hardship notes before escalation, and pause activity after a "
            b"declared-disaster "
            b"hardship offer. Repossession review begins at day 60 and requires approver review.\n"
        ),
        "reg-e-operations-bulletin-2025.md": (
            b"# Reg E Operations Bulletin (2025)\n\nThis bulletin applies to consumer "
            b"debit-card electronic fund transfer claims. Open the case on the date Meridian "
            b"receives notice, preserve the customer communication, document each investigation "
            b"step, and record the final decision in the servicing case. Use Debit Card Dispute "
            b"Handling SOP v3. All eligible disputes follow the 10-business-day provisional-credit "
            b"deadline. A signed written statement is required before investigation.\n"
        ),
        "reg-e-operations-bulletin-2026.md": (
            b"# Reg E Operations Bulletin (2026)\n\nThis bulletin applies to consumer "
            b"debit-card electronic fund transfer claims. Open the case on the date Meridian "
            b"receives notice, preserve the customer communication, document each investigation "
            b"step, and record the final decision in the servicing case. Use Debit Card Dispute "
            b"Handling SOP v4. Eligible disputes at or above $500 follow the 5-business-day "
            b"expedited deadline. Oral notice starts investigation; signed attestation follows "
            b"within 10 calendar days.\n"
        ),
        "disaster-county-reference-2026.csv": csv_bytes(county_rows),
        "servicing-note-quality-standard-2026.md": (
            b"# Servicing Note Quality Standard (2026)\n\nState the customer request, loan "
            b"identifier, offer status, eligibility basis, next action, and date. Never infer an "
            b"offer from silence.\n"
        ),
        "complaint-escalation-playbook-2026.md": (
            b"# Complaint Escalation Playbook (2026)\n\nEscalate open complaints tied to overdue "
            b"provisional-credit obligations to an approver on the same business day.\n"
        ),
    }


def _business_days_elapsed(start: date, end: date) -> int:
    day = start + timedelta(days=1)
    elapsed = 0
    while day <= end:
        if day.weekday() < 5:
            elapsed += 1
        day += timedelta(days=1)
    return elapsed


def _derive_b1(corpus: MeridianCorpus) -> B1GroundTruth:
    declaration = corpus.disaster_declarations[0]
    affected_counties = set(declaration.counties)
    window_start = corpus.as_of_date
    window_end = window_start + timedelta(days=14)
    loans_by_customer: dict[str, list[AutoLoan]] = {}
    for loan in corpus.auto_loans:
        loans_by_customer.setdefault(loan.customer_id, []).append(loan)
    payments_by_loan: dict[str, list[PaymentSchedule]] = {}
    for payment in corpus.payment_schedules:
        payments_by_loan.setdefault(payment.loan_id, []).append(payment)
    notes_by_loan: dict[str, list[ServicingNote]] = {}
    for note in corpus.servicing_notes:
        if note.loan_id:
            notes_by_loan.setdefault(note.loan_id, []).append(note)

    rows: list[B1Row] = []
    for customer in corpus.customers:
        affected = customer.address.county in affected_counties
        customer_loans = loans_by_customer.get(customer.customer_id) or [None]
        for loan in customer_loans:
            basis = [
                (
                    f"address county {customer.address.county} is in Meridian's selected "
                    f"designated-county subset for {declaration.declaration_id}"
                    if affected
                    else (
                        f"address county {customer.address.county} is outside Meridian's selected "
                        "three-county pilot subset"
                    )
                )
            ]
            actionable = False
            if loan is None:
                basis.append("customer has no auto loan")
            else:
                basis.append(f"loan status is {loan.status}")
                scheduled_due = any(
                    payment.status == "scheduled" and window_start <= payment.due_date <= window_end
                    for payment in payments_by_loan.get(loan.loan_id, [])
                )
                basis.append(
                    "an unpaid scheduled payment is due within the inclusive 14-day window"
                    if scheduled_due
                    else "no unpaid scheduled payment is due within the inclusive 14-day window"
                )
                note_types = {note.note_type for note in notes_by_loan.get(loan.loan_id, [])}
                already_offered = "hardship_offered" in note_types
                repossession = loan.status == "repossession" or "repossession" in note_types
                basis.append(
                    "a prior hardship offer is recorded"
                    if already_offered
                    else "no prior hardship offer is recorded"
                )
                basis.append(
                    "repossession activity is recorded"
                    if repossession
                    else "no repossession activity is recorded"
                )
                actionable = (
                    affected
                    and loan.status == "active"
                    and scheduled_due
                    and not already_offered
                    and not repossession
                )
            rows.append(
                B1Row(
                    customer_id=customer.customer_id,
                    county=customer.address.county,
                    loan_id=loan.loan_id if loan else None,
                    affected=affected,
                    actionable=actionable,
                    basis=basis,
                )
            )
    affected_rows = [row for row in rows if row.affected]
    actionable_rows = [row for row in rows if row.actionable]
    distractor_rows = [row for row in rows if not row.actionable]
    return B1GroundTruth(
        question=(
            f"For {declaration.declaration_id}, which affected Meridian auto loans have an unpaid "
            "scheduled payment due within 14 days and remain eligible for a new hardship offer?"
        ),
        declaration_id=declaration.declaration_id,
        window_start=window_start,
        window_end=window_end,
        affected_rows=affected_rows,
        actionable_rows=actionable_rows,
        distractor_rows=distractor_rows,
    )


def _derive_b2(corpus: MeridianCorpus) -> B2GroundTruth:
    answers: list[B2VersionAnswer] = []
    for policy in _POLICIES:
        answer_ids: list[str] = []
        basis: list[str] = []
        for dispute in corpus.reg_e_disputes:
            if dispute.status != "open" or dispute.provisional_credit_amount_cents is not None:
                continue
            deadline = policy.standard_deadline_business_days
            if (
                policy.expedited_threshold_cents is not None
                and dispute.amount_cents >= policy.expedited_threshold_cents
            ):
                deadline = policy.expedited_deadline_business_days
            elapsed = _business_days_elapsed(dispute.notice_date, corpus.as_of_date)
            if elapsed > deadline:
                answer_ids.append(dispute.dispute_id)
                basis.append(
                    f"{dispute.dispute_id}: {elapsed} business days elapsed exceeds {deadline}"
                )
        answers.append(
            B2VersionAnswer(
                artifact_id=policy.artifact_id,
                version=policy.version,
                current=policy.current,
                threshold_cents=policy.expedited_threshold_cents,
                deadline_business_days=(
                    policy.expedited_deadline_business_days
                    if policy.expedited_threshold_cents is not None
                    else policy.standard_deadline_business_days
                ),
                answer_dispute_ids=sorted(answer_ids),
                basis=basis,
            )
        )
    current = [answer for answer in answers if answer.current]
    if len(current) != 1:
        raise ValueError("exactly one current dispute SOP is required")
    return B2GroundTruth(
        question="Which open debit-card disputes are overdue for provisional credit under the SOP?",
        answers_by_version=answers,
        current_artifact_id=current[0].artifact_id,
        current_answer_dispute_ids=current[0].answer_dispute_ids,
    )


def _derive_b3(corpus: MeridianCorpus) -> B3GroundTruth:
    credited = [
        dispute
        for dispute in corpus.reg_e_disputes
        if dispute.provisional_credit_amount_cents is not None
    ]
    return B3GroundTruth(
        question="What is the total value of Reg E provisional credits that were issued late?",
        answerable=False,
        expected_response=(
            "Refuse to calculate a late-credit total: provisional-credit issue dates are absent "
            "from the source records, so timeliness cannot be determined."
        ),
        missing_fields=["reg_e_disputes.provisional_credit_issue_date"],
        control_question="What is the total recorded amount of Reg E provisional credits?",
        control_answer_cents=sum(d.provisional_credit_amount_cents or 0 for d in credited),
        contributing_dispute_ids=sorted(d.dispute_id for d in credited),
    )


def derive_ground_truth(corpus: MeridianCorpus) -> GroundTruth:
    """Derive every business-question answer from corpus source records."""
    return GroundTruth(
        corpus_version=CORPUS_VERSION,
        generated_from=[
            "corpus.customers",
            "corpus.auto_loans",
            "corpus.payment_schedules",
            "corpus.servicing_notes",
            "corpus.reg_e_disputes",
            "corpus.disaster_declarations",
            "corpus.artifacts",
        ],
        b1=_derive_b1(corpus),
        b2=_derive_b2(corpus),
        b3=_derive_b3(corpus),
    )


def _at_midnight(value: date) -> datetime:
    return datetime.combine(value, time.min, tzinfo=UTC)


def _operational_records(corpus: MeridianCorpus) -> OperationalRecordSet:
    """Map the banking sidecar into the frozen generic operational schema."""
    accounts = [
        Account(
            id=household.household_id,
            name=household.household_name,
            domain=f"{household.household_id}.meridian.example",
            industry="Retail Banking Household",
            status=AccountStatus.active,
            source_system="meridian-salesforce",
            source_record_id=household.household_id,
            created_at=_at_midnight(date(2024, 1, 1)),
            updated_at=_at_midnight(corpus.as_of_date),
        )
        for household in corpus.households
    ]
    people = [
        Person(
            id=customer.customer_id,
            given_name=customer.first_name,
            family_name=customer.last_name,
            email=customer.email,
            title="Meridian Bank Customer",
            phone=f"+1-555-01{index:02d}",
            works_at=customer.household_id,
            is_active=customer.status == "active",
            source_system="meridian-salesforce",
            source_record_id=customer.customer_id,
            created_at=_at_midnight(date(2024, 1, 1)),
            updated_at=_at_midnight(corpus.as_of_date),
        )
        for index, customer in enumerate(corpus.customers)
    ]
    documents = [
        Document(
            id=artifact.artifact_id,
            title=f"{artifact.title} ({artifact.version})",
            doc_type=(
                DocumentType.report
                if artifact.family in {"fee-schedule", "disaster-county-reference"}
                else DocumentType.policy
            ),
            uri=f"meridian://fixtures/assets/{artifact.filename}",
            mime_type=artifact.media_type,
            author="mer-cus-001",
            owned_by="mer-cus-001",
            source_system="meridian-sharepoint",
            source_record_id=artifact.artifact_id,
            created_at=_at_midnight(artifact.effective_date),
            updated_at=_at_midnight(artifact.effective_date),
        )
        for artifact in corpus.artifacts
    ]
    customer_to_household = {c.customer_id: c.household_id for c in corpus.customers}
    tickets: list[Ticket] = []
    for source in corpus.servicing_tickets:
        status = {
            "open": TicketStatus.open,
            "pending": TicketStatus.pending,
            "closed": TicketStatus.closed,
        }[source.status]
        tickets.append(
            Ticket(
                id=source.ticket_id,
                subject=source.subject,
                description=(
                    f"category={source.category}; customer_id={source.customer_id}; "
                    f"loan_id={source.loan_id}"
                ),
                status=status,
                priority=TicketPriority.normal,
                belongs_to=customer_to_household[source.customer_id],
                requester=source.customer_id,
                assignee=source.customer_id,
                opened_at=_at_midnight(source.opened_date),
                resolved_at=(
                    _at_midnight(source.opened_date) if status == TicketStatus.closed else None
                ),
                source_system="meridian-salesforce",
                source_record_id=source.ticket_id,
                created_at=_at_midnight(source.opened_date),
                updated_at=_at_midnight(source.opened_date),
            )
        )
    for source in corpus.complaints:
        status = {
            "open": TicketStatus.open,
            "investigating": TicketStatus.pending,
            "resolved": TicketStatus.resolved,
        }[source.status]
        tickets.append(
            Ticket(
                id=source.complaint_id,
                subject=f"Complaint {source.complaint_id}",
                description=(
                    f"{source.summary} customer_id={source.customer_id}; "
                    f"dispute_id={source.dispute_id}"
                ),
                status=status,
                priority=TicketPriority.high,
                belongs_to=customer_to_household[source.customer_id],
                requester=source.customer_id,
                assignee=source.customer_id,
                opened_at=_at_midnight(source.received_date),
                resolved_at=(
                    _at_midnight(source.received_date) if status == TicketStatus.resolved else None
                ),
                source_system="meridian-salesforce",
                source_record_id=source.complaint_id,
                created_at=_at_midnight(source.received_date),
                updated_at=_at_midnight(source.received_date),
            )
        )
    for source in corpus.reg_e_disputes:
        status = TicketStatus.open if source.status == "open" else TicketStatus.closed
        tickets.append(
            Ticket(
                id=source.dispute_id,
                subject=f"Debit-card dispute {source.dispute_id}",
                description=(
                    f"customer_id={source.customer_id}; card_id={source.card_id}; "
                    f"notice_date={source.notice_date}; amount_cents={source.amount_cents}; "
                    f"provisional_credit_amount_cents={source.provisional_credit_amount_cents}; "
                    "provisional_credit_issue_date=ABSENT"
                ),
                status=status,
                priority=(
                    TicketPriority.high if source.amount_cents >= 50_000 else TicketPriority.normal
                ),
                belongs_to=customer_to_household[source.customer_id],
                requester=source.customer_id,
                assignee=source.customer_id,
                opened_at=_at_midnight(source.notice_date),
                resolved_at=(
                    _at_midnight(source.notice_date) if status == TicketStatus.closed else None
                ),
                source_system="meridian-salesforce",
                source_record_id=source.dispute_id,
                created_at=_at_midnight(source.notice_date),
                updated_at=_at_midnight(source.notice_date),
            )
        )
    return OperationalRecordSet(
        accounts=accounts,
        people=people,
        documents=documents,
        tickets=tickets,
        orders=[],
    )


def generate(
    spec: DeclarationSpec, *, seed: int = DEFAULT_SEED
) -> tuple[OperationalRecordSet, MeridianCorpus, GroundTruth, dict[str, bytes]]:
    """Generate the corpus, source-derived answers, and all document bytes."""
    rng = DetRandom(seed)
    households = _households()
    customers = _customers(spec)
    accounts = _deposit_accounts(rng)
    loans = _auto_loans()
    schedules = _payment_schedules(loans)
    tickets, notes = _servicing()
    fee_schedule, fee_postings = _fees()
    declaration = DisasterDeclaration(
        declaration_id=spec.declaration_id,
        state="GA",
        declaration_date=spec.declaration_date,
        incident=spec.incident,
        counties=list(spec.counties),
        scope_description=spec.scope_description,
        source_url=spec.source_url,
    )
    artifacts = _artifacts()
    corpus = MeridianCorpus(
        corpus_version=CORPUS_VERSION,
        as_of_date=AS_OF_DATE,
        personas=_personas(),
        households=households,
        customers=customers,
        deposit_accounts=accounts,
        auto_loans=loans,
        debit_cards=_cards(rng),
        payment_schedules=schedules,
        servicing_tickets=tickets,
        servicing_notes=notes,
        complaints=_complaints(),
        reg_e_disputes=_disputes(),
        fee_postings=fee_postings,
        disaster_declarations=[declaration],
        fee_schedule_rows=fee_schedule,
        artifacts=artifacts,
    )
    ground_truth = derive_ground_truth(corpus)
    return (
        _operational_records(corpus),
        corpus,
        ground_truth,
        _artifact_bytes(artifacts, fee_schedule, declaration),
    )


def serialize(
    record_set: OperationalRecordSet,
    corpus: MeridianCorpus,
    ground_truth: GroundTruth,
    assets: dict[str, bytes],
    *,
    seed: int = DEFAULT_SEED,
) -> dict[str, bytes]:
    records_bytes = canonical_json(record_set.model_dump(mode="json", exclude_none=True))
    corpus_bytes = canonical_json(corpus.model_dump(mode="json"))
    truth_bytes = canonical_json(ground_truth.model_dump(mode="json"))
    output: dict[str, bytes] = {
        "records.json": records_bytes,
        "banking.json": corpus_bytes,
        "ground_truth.json": truth_bytes,
    }
    for filename in sorted(assets):
        output[f"{ASSET_PREFIX}{filename}"] = assets[filename]
    sha256 = {name: hashlib.sha256(body).hexdigest() for name, body in output.items()}
    counts = {
        "households": len(corpus.households),
        "customers": len(corpus.customers),
        "deposit_accounts": len(corpus.deposit_accounts),
        "auto_loans": len(corpus.auto_loans),
        "debit_cards": len(corpus.debit_cards),
        "payment_schedules": len(corpus.payment_schedules),
        "servicing_tickets": len(corpus.servicing_tickets),
        "servicing_notes": len(corpus.servicing_notes),
        "complaints": len(corpus.complaints),
        "reg_e_disputes": len(corpus.reg_e_disputes),
        "fee_postings": len(corpus.fee_postings),
        "artifacts": len(corpus.artifacts),
        "operational_accounts": len(record_set.accounts or []),
        "operational_people": len(record_set.people or []),
        "operational_documents": len(record_set.documents or []),
        "operational_tickets": len(record_set.tickets or []),
    }
    output["manifest.json"] = canonical_json(
        CorpusManifest(
            corpus_version=CORPUS_VERSION,
            seed=seed,
            generator=GENERATOR_NAME,
            as_of_date=corpus.as_of_date,
            counts=counts,
            sha256=sha256,
        ).model_dump(mode="json")
    )
    return output


def write_fixture(files: dict[str, bytes], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        path = out_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)


def check_fixture(files: dict[str, bytes], fixtures_dir: Path) -> list[str]:
    expected = set(files)
    actual = {
        path.relative_to(fixtures_dir).as_posix()
        for path in fixtures_dir.rglob("*")
        if path.is_file()
    }
    drift = [name for name in sorted(expected) if not (fixtures_dir / name).is_file()]
    drift.extend(
        name
        for name in sorted(expected & actual)
        if (fixtures_dir / name).read_bytes() != files[name]
    )
    drift.extend(f"unexpected:{name}" for name in sorted(actual - expected))
    return drift
