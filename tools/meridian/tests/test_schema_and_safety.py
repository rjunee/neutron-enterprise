# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Frozen-schema projection, sidecar bounds, referential closure, and safety."""

from __future__ import annotations

import json

from ne.meridian.models import MeridianCorpus
from ne.schema import OperationalRecordSet

from .conftest import FIXTURES


def test_minimum_domain_bounds_and_all_required_entity_collections(corpus: MeridianCorpus) -> None:
    assert len(corpus.households) == 10
    assert len(corpus.customers) >= 15
    assert len(corpus.deposit_accounts) >= 15
    assert len(corpus.auto_loans) >= 8
    assert len(corpus.debit_cards) >= 15
    assert len(corpus.payment_schedules) == len(corpus.auto_loans)
    assert corpus.servicing_tickets and corpus.servicing_notes
    assert corpus.complaints and corpus.reg_e_disputes and corpus.fee_postings
    assert len(corpus.artifacts) >= 10
    assert len(corpus.fee_schedule_rows) >= 4


def test_frozen_schema_projection_validates_and_maps_all_required_types(
    records: OperationalRecordSet, corpus: MeridianCorpus
) -> None:
    assert len(records.accounts or []) == len(corpus.households)
    assert len(records.people or []) == len(corpus.customers)
    assert len(records.documents or []) == len(corpus.artifacts)
    expected_tickets = (
        len(corpus.servicing_tickets) + len(corpus.complaints) + len(corpus.reg_e_disputes)
    )
    assert len(records.tickets or []) == expected_tickets


def test_operational_projection_has_referential_closure(records: OperationalRecordSet) -> None:
    account_ids = {row.id for row in records.accounts or []}
    person_ids = {row.id for row in records.people or []}
    assert len(account_ids) == len(records.accounts or [])
    assert len(person_ids) == len(records.people or [])
    assert all(person.works_at in account_ids for person in records.people or [])
    for document in records.documents or []:
        assert document.author in person_ids
        assert document.owned_by in person_ids
    for ticket in records.tickets or []:
        assert ticket.belongs_to in account_ids
        assert ticket.requester in person_ids
        assert ticket.assignee in person_ids


def test_banking_sidecar_references_are_closed(corpus: MeridianCorpus) -> None:
    households = {row.household_id for row in corpus.households}
    customers = {row.customer_id for row in corpus.customers}
    accounts = {row.account_id for row in corpus.deposit_accounts}
    loans = {row.loan_id for row in corpus.auto_loans}
    cards = {row.card_id for row in corpus.debit_cards}
    tickets = {row.ticket_id for row in corpus.servicing_tickets}
    disputes = {row.dispute_id for row in corpus.reg_e_disputes}
    assert all(row.household_id in households for row in corpus.customers)
    assert all(row.customer_id in customers for row in corpus.deposit_accounts)
    assert all(row.customer_id in customers for row in corpus.auto_loans)
    assert all(
        row.customer_id in customers and row.deposit_account_id in accounts
        for row in corpus.debit_cards
    )
    assert all(row.loan_id in loans for row in corpus.payment_schedules)
    assert all(row.ticket_id in tickets for row in corpus.servicing_notes)
    assert all(row.dispute_id is None or row.dispute_id in disputes for row in corpus.complaints)
    assert all(row.card_id in cards for row in corpus.reg_e_disputes)


def test_fixture_is_obviously_synthetic_and_has_no_issue_date_field(corpus: MeridianCorpus) -> None:
    # NE-CS-SEND: mer-cus-001 alone carries the owner's real demo inbox (the S5
    # outreach recipient is looked up from source data, and the deployment's
    # send allowlist admits exactly this address). Every other customer must
    # stay obviously synthetic.
    from ne.meridian.generator import DEMO_STANDIN_EMAIL

    assert all(
        customer.email == DEMO_STANDIN_EMAIL
        if customer.customer_id == "mer-cus-001"
        else customer.email.endswith("@meridian.example")
        for customer in corpus.customers
    )
    assert all(
        customer.address.line1.endswith("Meridian Demo Way") for customer in corpus.customers
    )
    records = json.loads((FIXTURES / "records.json").read_bytes())
    assert all(person["phone"].startswith("+1-555-01") for person in records["people"])
    banking = json.loads((FIXTURES / "banking.json").read_bytes())
    for card in banking["debit_cards"]:
        assert "issue_date" not in card
    for dispute in banking["reg_e_disputes"]:
        assert "provisional_credit_issue_date" not in dispute


def test_real_fema_constants_are_isolated_and_scope_is_not_overclaimed(
    corpus: MeridianCorpus,
) -> None:
    declaration = corpus.disaster_declarations[0]
    assert declaration.declaration_id == "DR-4830"
    assert declaration.state == "GA"
    assert declaration.incident == "Hurricane Helene"
    assert declaration.counties == ["Columbia", "Richmond", "Jefferson"]
    assert "not the exhaustive" in declaration.scope_description
    assert declaration.source_url == "https://www.fema.gov/disaster/4830"
