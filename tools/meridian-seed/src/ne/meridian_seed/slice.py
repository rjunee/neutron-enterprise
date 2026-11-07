# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Typed, hash-verified SharePoint/Salesforce slice of Meridian fixtures."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from ne.connect.m365.operational_graph import (
    MERIDIAN_FRONTLINE_WITHHELD_CLASSES,
    MERIDIAN_OPERATIONAL_MEDIA_TYPE,
    OperationalGraphEdge,
    OperationalGraphEnvelope,
    canonical_operational_graph_bytes,
)
from ne.meridian.cli import default_fixtures_dir
from ne.meridian.models import (
    PERSONA_IDS,
    ArtifactAcl,
    CorpusManifest,
    GroundTruth,
    MeridianCorpus,
    PersonaId,
)
from ne.meridian_seed.errors import SeedError


class SliceDocument(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact_id: str
    filename: str
    sharepoint_path: str
    media_type: str
    seed_stage: Literal["initial", "supersession"]
    content: bytes
    acl: ArtifactAcl


class OperationalGraphDocument(BaseModel):
    """One canonical M365 item carrying one typed Meridian graph entity."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    record_id: str
    entity_class: Literal["Customer", "AutoLoan", "PaymentSchedule", "ServicingNote"]
    filename: str
    media_type: Literal["application/json"]
    content: bytes
    acl: ArtifactAcl
    edges: tuple[OperationalGraphEdge, ...]


class SalesforceAccount(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: str
    name: str


class SalesforceContact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: str
    account_source_id: str
    first_name: str
    last_name: str
    email: str


class SalesforceCase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: str
    account_source_id: str
    case_type: str
    subject: str
    description: str
    status: str
    priority: str


class SliceSelection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    corpus_version: str
    documents: tuple[SliceDocument, ...]
    accounts: tuple[SalesforceAccount, ...]
    contacts: tuple[SalesforceContact, ...]
    cases: tuple[SalesforceCase, ...]
    operational_graph: tuple[OperationalGraphDocument, ...]

    def documents_for_stage(
        self, stage: Literal["initial", "supersession"]
    ) -> tuple[SliceDocument, ...]:
        return tuple(document for document in self.documents if document.seed_stage == stage)


def _load_json(fixtures: Path, name: str) -> object:
    return json.loads((fixtures / name).read_bytes())


def _verify_manifest(fixtures: Path, manifest: CorpusManifest) -> None:
    for name, expected in manifest.sha256.items():
        path = fixtures / name
        if not path.is_file():
            raise SeedError(f"Meridian fixture is missing manifest artifact {name}")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            raise SeedError(f"Meridian fixture hash mismatch for {name}")


def _case_description(prefix: str, fields: list[tuple[str, object]]) -> str:
    return prefix + "\n" + "\n".join(f"{name}: {value}" for name, value in fields)


def _operational_acl(entity_class: str) -> ArtifactAcl:
    """Derive one operational record's persona ACL from its entity class alone.

    There is no per-call-site override: the withheld set is the ONLY input, so
    two record types cannot be authored with different frontline reachability by
    a caller forgetting a keyword.
    """
    restricted = entity_class in MERIDIAN_FRONTLINE_WITHHELD_CLASSES
    allowed: list[PersonaId] = ["ops", "approver"] if restricted else list(PERSONA_IDS)
    denied: list[PersonaId] = [persona for persona in PERSONA_IDS if persona not in allowed]
    return ArtifactAcl(
        allowed_personas=allowed,
        denied_personas=denied,
        classification="restricted" if restricted else "internal",
    )


def _operational_document(
    *,
    entity_class: Literal["Customer", "AutoLoan", "PaymentSchedule", "ServicingNote"],
    record_id: str,
    fields: dict[str, object],
    edges: tuple[OperationalGraphEdge, ...] = (),
) -> OperationalGraphDocument:
    envelope = OperationalGraphEnvelope(
        schema="meridian-operational-record/v1",
        schema_version="1.1.0",
        entity_class=entity_class,
        record_id=record_id,
        fields=fields,
        edges=tuple(sorted(edges, key=lambda edge: edge.model_dump_json())),
    )
    return OperationalGraphDocument(
        record_id=record_id,
        entity_class=entity_class,
        filename=f"graph-{entity_class.lower()}-{record_id}.negraph.json",
        media_type=MERIDIAN_OPERATIONAL_MEDIA_TYPE,
        content=canonical_operational_graph_bytes(envelope),
        acl=_operational_acl(entity_class),
        edges=envelope.edges,
    )


def _operational_graph(corpus: MeridianCorpus) -> tuple[OperationalGraphDocument, ...]:
    loans_by_customer = {loan.customer_id: loan for loan in corpus.auto_loans}
    payments_by_loan = {payment.loan_id: payment for payment in corpus.payment_schedules}
    notes_by_customer = {note.customer_id: note for note in corpus.servicing_notes}
    notes_by_loan = {note.loan_id: note for note in corpus.servicing_notes if note.loan_id}
    documents: list[OperationalGraphDocument] = []
    for customer in sorted(corpus.customers, key=lambda item: item.customer_id):
        edges: list[OperationalGraphEdge] = []
        loan = loans_by_customer.get(customer.customer_id)
        note = notes_by_customer.get(customer.customer_id)
        if loan is not None:
            edges.append(
                OperationalGraphEdge(
                    edge_kind="HAS_AUTO_LOAN",
                    target_class="AutoLoan",
                    target_record_id=loan.loan_id,
                )
            )
        if note is not None:
            edges.append(
                OperationalGraphEdge(
                    edge_kind="HAS_SERVICING_NOTE",
                    target_class="ServicingNote",
                    target_record_id=note.note_id,
                )
            )
        documents.append(
            _operational_document(
                entity_class="Customer",
                record_id=customer.customer_id,
                fields={
                    "id": customer.customer_id,
                    "customer_identifier": customer.customer_id,
                    "first_name": customer.first_name,
                    "last_name": customer.last_name,
                    "county": customer.address.county,
                    "status": customer.status,
                },
                edges=tuple(edges),
            )
        )
    for loan in sorted(corpus.auto_loans, key=lambda item: item.loan_id):
        payment = payments_by_loan[loan.loan_id]
        note = notes_by_loan[loan.loan_id]
        documents.append(
            _operational_document(
                entity_class="AutoLoan",
                record_id=loan.loan_id,
                fields={
                    "id": loan.loan_id,
                    "loan_identifier": loan.loan_id,
                    "status": loan.status,
                    "principal_balance_cents": loan.principal_balance_cents,
                    "next_due_date": loan.next_due_date.isoformat(),
                },
                edges=(
                    OperationalGraphEdge(
                        edge_kind="HAS_PAYMENT_SCHEDULE",
                        target_class="PaymentSchedule",
                        target_record_id=payment.payment_id,
                    ),
                    OperationalGraphEdge(
                        edge_kind="HAS_SERVICING_NOTE",
                        target_class="ServicingNote",
                        target_record_id=note.note_id,
                    ),
                ),
            )
        )
    for payment in sorted(corpus.payment_schedules, key=lambda item: item.payment_id):
        documents.append(
            _operational_document(
                entity_class="PaymentSchedule",
                record_id=payment.payment_id,
                fields={
                    "id": payment.payment_id,
                    "payment_identifier": payment.payment_id,
                    "due_date": payment.due_date.isoformat(),
                    "amount_cents": payment.amount_cents,
                    "status": payment.status,
                },
            )
        )
    for note in sorted(corpus.servicing_notes, key=lambda item: item.note_id):
        documents.append(
            _operational_document(
                entity_class="ServicingNote",
                record_id=note.note_id,
                fields={
                    "id": note.note_id,
                    "note_identifier": note.note_id,
                    "ticket_identifier": note.ticket_id,
                    "note_type": note.note_type,
                    "created_date": note.created_date.isoformat(),
                    "text": note.text,
                },
            )
        )
    return tuple(documents)


def slice_corpus(fixtures: Path | None = None) -> SliceSelection:
    fixtures = fixtures if fixtures is not None else default_fixtures_dir()
    manifest = CorpusManifest.model_validate(_load_json(fixtures, "manifest.json"))
    _verify_manifest(fixtures, manifest)
    corpus = MeridianCorpus.model_validate(_load_json(fixtures, "banking.json"))
    GroundTruth.model_validate(_load_json(fixtures, "ground_truth.json"))

    documents = tuple(
        SliceDocument(
            artifact_id=artifact.artifact_id,
            filename=artifact.filename,
            sharepoint_path=artifact.sharepoint_path,
            media_type=artifact.media_type,
            seed_stage=artifact.seed_stage,
            content=(fixtures / "assets" / artifact.filename).read_bytes(),
            acl=artifact.acl,
        )
        for artifact in sorted(corpus.artifacts, key=lambda item: item.artifact_id)
    )
    accounts = tuple(
        SalesforceAccount(source_id=h.household_id, name=h.household_name)
        for h in sorted(corpus.households, key=lambda item: item.household_id)
    )
    contacts = tuple(
        SalesforceContact(
            source_id=customer.customer_id,
            account_source_id=customer.household_id,
            first_name=customer.first_name,
            last_name=customer.last_name,
            email=customer.email,
        )
        for customer in sorted(corpus.customers, key=lambda item: item.customer_id)
    )
    household_by_customer = {c.customer_id: c.household_id for c in corpus.customers}
    cases: list[SalesforceCase] = []
    for ticket in corpus.servicing_tickets:
        cases.append(
            SalesforceCase(
                source_id=ticket.ticket_id,
                account_source_id=household_by_customer[ticket.customer_id],
                case_type="Servicing",
                subject=ticket.subject,
                description=_case_description(
                    "Synthetic Meridian servicing ticket",
                    [
                        ("customer_id", ticket.customer_id),
                        ("loan_id", ticket.loan_id),
                        ("category", ticket.category),
                    ],
                ),
                status={"open": "New", "pending": "Working", "closed": "Closed"}[ticket.status],
                priority="Medium",
            )
        )
    for complaint in corpus.complaints:
        cases.append(
            SalesforceCase(
                source_id=complaint.complaint_id,
                account_source_id=household_by_customer[complaint.customer_id],
                case_type="Complaint",
                subject=f"Complaint {complaint.complaint_id}",
                description=_case_description(
                    complaint.summary,
                    [("customer_id", complaint.customer_id), ("dispute_id", complaint.dispute_id)],
                ),
                status={"open": "New", "investigating": "Working", "resolved": "Closed"}[
                    complaint.status
                ],
                priority="High",
            )
        )
    for dispute in corpus.reg_e_disputes:
        cases.append(
            SalesforceCase(
                source_id=dispute.dispute_id,
                account_source_id=household_by_customer[dispute.customer_id],
                case_type="Reg E Dispute",
                subject=f"Debit-card dispute {dispute.dispute_id}",
                description=_case_description(
                    "Synthetic Meridian Reg E dispute",
                    [
                        ("customer_id", dispute.customer_id),
                        ("card_id", dispute.card_id),
                        ("notice_date", dispute.notice_date),
                        ("amount_cents", dispute.amount_cents),
                        (
                            "provisional_credit_amount_cents",
                            dispute.provisional_credit_amount_cents,
                        ),
                        ("provisional_credit_issue_date", "ABSENT"),
                    ],
                ),
                status="New" if dispute.status == "open" else "Closed",
                priority="High" if dispute.amount_cents >= 50_000 else "Medium",
            )
        )
    return SliceSelection(
        corpus_version=corpus.corpus_version,
        documents=documents,
        accounts=accounts,
        contacts=contacts,
        cases=tuple(sorted(cases, key=lambda item: item.source_id)),
        operational_graph=_operational_graph(corpus),
    )
