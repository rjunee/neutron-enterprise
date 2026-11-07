# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Every cross-reference in the corpus resolves to a real record."""

from __future__ import annotations

import json
from pathlib import Path

from ne.schema import OperationalRecordSet

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "acme-v1"
ACME_ACCOUNT_ID = "acct-0000"


def _load_record_set() -> OperationalRecordSet:
    obj = json.loads((FIXTURES / "records.json").read_bytes())
    return OperationalRecordSet.model_validate(obj)


def test_document_refs_resolve() -> None:
    rs = _load_record_set()
    assert rs.people is not None and rs.documents is not None and rs.accounts is not None
    people_ids = {p.id for p in rs.people}
    account_ids = {a.id for a in rs.accounts}
    for doc in rs.documents:
        assert doc.author is not None and doc.author in people_ids
        assert doc.owned_by is not None and doc.owned_by in people_ids
        if doc.about:
            for account_id in doc.about:
                assert account_id in account_ids


def test_ticket_refs_resolve() -> None:
    rs = _load_record_set()
    assert rs.tickets is not None and rs.people is not None and rs.accounts is not None
    people_ids = {p.id for p in rs.people}
    account_ids = {a.id for a in rs.accounts}
    for ticket in rs.tickets:
        assert ticket.belongs_to in account_ids
        assert ticket.belongs_to != ACME_ACCOUNT_ID
        assert ticket.requester is not None and ticket.requester in people_ids
        assert ticket.assignee is not None and ticket.assignee in people_ids


def test_order_refs_resolve() -> None:
    rs = _load_record_set()
    assert rs.orders is not None and rs.people is not None and rs.accounts is not None
    people_ids = {p.id for p in rs.people}
    account_ids = {a.id for a in rs.accounts}
    for order in rs.orders:
        assert order.account in account_ids
        assert order.placed_by in people_ids


def test_people_work_at_acme() -> None:
    rs = _load_record_set()
    assert rs.people is not None and rs.accounts is not None
    assert len(rs.people) == 40
    assert all(p.works_at == ACME_ACCOUNT_ID for p in rs.people)
    acme = next(a for a in rs.accounts if a.id == ACME_ACCOUNT_ID)
    assert acme.name == "Acme Corporation"


def test_ticket_resolved_at_consistent() -> None:
    rs = _load_record_set()
    assert rs.tickets is not None
    for ticket in rs.tickets:
        if ticket.status in ("resolved", "closed"):
            assert ticket.resolved_at is not None
            assert ticket.resolved_at > ticket.opened_at
        else:
            assert ticket.resolved_at is None
