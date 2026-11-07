# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Static seed-class signature table (design §4.1 "assemble, don't invent").

Transcribed from ``packages/ne-schema/src/ne/schema/schemas/operational.yaml``
(the nine non-abstract operational classes: Account, Person, Document,
Ticket, Order, Customer, AutoLoan, PaymentSchedule, and ServicingNote;
verified against source at build time). Each
signature is the class's non-provenance attribute names — the shared
``OperationalEntity`` provenance slots (:data:`PROVENANCE_SLOTS`) are excluded
from both sides of every anchoring comparison.

U1 hard-codes this signature instead of importing ``ne.schema``: importing
``ne-schema`` would drag its codegen/linkml dependency surface into this
leaf-level, dependency-free package (design §3.2 — ``ne-schema`` itself
"imports nothing from other `ne` packages", and this package mirrors that
discipline in reverse).
"""

from __future__ import annotations

SEED_CLASS_SIGNATURES: dict[str, frozenset[str]] = {
    "Account": frozenset({"name", "domain", "industry", "status", "employee_count"}),
    "Person": frozenset(
        {"given_name", "family_name", "email", "title", "phone", "works_at", "is_active"}
    ),
    "Document": frozenset({"title", "doc_type", "uri", "mime_type", "author", "owned_by", "about"}),
    "Ticket": frozenset(
        {
            "subject",
            "description",
            "status",
            "priority",
            "belongs_to",
            "requester",
            "assignee",
            "opened_at",
            "resolved_at",
        }
    ),
    "Order": frozenset(
        {
            "order_number",
            "account",
            "placed_by",
            "status",
            "total_amount",
            "currency",
            "placed_at",
        }
    ),
    "Customer": frozenset(
        {
            "customer_identifier",
            "first_name",
            "last_name",
            "county",
            "status",
            "has_auto_loan",
            "has_servicing_note",
        }
    ),
    "AutoLoan": frozenset(
        {
            "loan_identifier",
            "status",
            "principal_balance_cents",
            "next_due_date",
            "has_payment_schedule",
            "has_servicing_note",
        }
    ),
    "PaymentSchedule": frozenset({"payment_identifier", "due_date", "amount_cents", "status"}),
    "ServicingNote": frozenset(
        {"note_identifier", "ticket_identifier", "note_type", "created_date", "text"}
    ),
}

PROVENANCE_SLOTS: frozenset[str] = frozenset(
    {"id", "source_system", "source_record_id", "created_at", "updated_at"}
)
