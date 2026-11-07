# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Domain-semantics proof for the generated operational Pydantic models."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from ne.schema import (
    Account,
    AccountStatus,
    Document,
    DocumentType,
    OperationalRecordSet,
    Order,
    OrderStatus,
    Person,
    Ticket,
    TicketPriority,
    TicketStatus,
)

_TS = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _account() -> Account:
    return Account(
        id="acc-acme",
        name="Acme Corp",
        domain="acme.com",
        industry="manufacturing",
        status=AccountStatus.active,
        employee_count=500,
    )


def _person() -> Person:
    return Person(
        id="per-ada",
        given_name="Ada",
        family_name="Lovelace",
        email="ada@acme.com",
        title="CTO",
        works_at="acc-acme",
        is_active=True,
    )


def _document() -> Document:
    return Document(
        id="doc-msa",
        title="Master Services Agreement",
        doc_type=DocumentType.contract,
        uri="s3://docs/msa.pdf",
        author="per-ada",
        owned_by="per-ada",
        about=["acc-acme"],
    )


def _ticket() -> Ticket:
    return Ticket(
        id="tkt-1",
        subject="Login fails",
        description="User cannot log in",
        status=TicketStatus.open,
        priority=TicketPriority.high,
        belongs_to="acc-acme",
        requester="per-ada",
        assignee="per-ada",
        opened_at=_TS,
    )


def _order() -> Order:
    return Order(
        id="ord-1",
        order_number="ORD-0001",
        account="acc-acme",
        placed_by="per-ada",
        status=OrderStatus.placed,
        currency="USD",
        total_amount=Decimal("1999.99"),
        placed_at=_TS,
    )


def _valid_graph() -> OperationalRecordSet:
    return OperationalRecordSet(
        accounts=[_account()],
        people=[_person()],
        documents=[_document()],
        tickets=[_ticket()],
        orders=[_order()],
    )


def test_valid_graph_roundtrips() -> None:
    graph = _valid_graph()
    restored = OperationalRecordSet.model_validate_json(graph.model_dump_json())
    assert restored == graph


def test_relations_are_str_id_references() -> None:
    person = _person()
    order = _order()
    document = _document()
    assert isinstance(person.works_at, str)
    assert person.works_at == "acc-acme"
    assert isinstance(order.account, str)
    assert document.about == ["acc-acme"]


@pytest.mark.parametrize(
    "cls, kwargs",
    [
        (Account, {"id": "a"}),  # missing name
        (Ticket, {"id": "t", "subject": "s"}),  # missing status/belongs_to/opened_at
        (Order, {"id": "o", "order_number": "ORD-0001", "account": "a", "placed_by": "p"}),
    ],
)
def test_required_fields_enforced(cls: type, kwargs: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        cls(**kwargs)


def test_id_required_on_every_entity() -> None:
    with pytest.raises(ValidationError):
        Account.model_validate({"name": "A"})
    with pytest.raises(ValidationError):
        Person.model_validate({"given_name": "a", "family_name": "b", "email": "a@b.co"})


def test_valid_enum_values_accepted() -> None:
    assert Account(id="a", name="A", status=AccountStatus.churned).status == "churned"


@pytest.mark.parametrize(
    "payload",
    [
        {"id": "t", "subject": "s", "status": "nope", "belongs_to": "a", "opened_at": _TS},
        {
            "id": "o",
            "order_number": "ORD-0001",
            "account": "a",
            "placed_by": "p",
            "status": "shipped",  # not a valid OrderStatus
            "currency": "USD",
            "total_amount": "1",
            "placed_at": _TS,
        },
    ],
)
def test_invalid_enum_values_rejected(payload: dict[str, object]) -> None:
    model = Ticket if "subject" in payload else Order
    with pytest.raises(ValidationError):
        model.model_validate(payload)


@pytest.mark.parametrize("email", ["not-an-email", "a@b", "@acme.com", "ada acme.com"])
def test_bad_email_rejected(email: str) -> None:
    with pytest.raises(ValidationError):
        Person(id="p", given_name="a", family_name="b", email=email)


@pytest.mark.parametrize("currency", ["usd", "US", "USDD", "12A"])
def test_bad_currency_rejected(currency: str) -> None:
    with pytest.raises(ValidationError):
        Order(
            id="o",
            order_number="ORD-0001",
            account="a",
            placed_by="p",
            status=OrderStatus.placed,
            currency=currency,
            total_amount=Decimal("1"),
            placed_at=_TS,
        )


@pytest.mark.parametrize("order_number", ["ORD-1", "1234", "ORD-ABCD", "ord-0001"])
def test_bad_order_number_rejected(order_number: str) -> None:
    with pytest.raises(ValidationError):
        Order(
            id="o",
            order_number=order_number,
            account="a",
            placed_by="p",
            status=OrderStatus.placed,
            currency="USD",
            total_amount=Decimal("1"),
            placed_at=_TS,
        )


def test_extra_fields_forbidden() -> None:
    with pytest.raises(ValidationError):
        Account.model_validate({"id": "a", "name": "A", "unknown_field": "x"})


def test_record_set_accepts_all_five_entity_types() -> None:
    graph = _valid_graph()
    assert graph.accounts is not None and len(graph.accounts) == 1
    assert graph.people is not None and len(graph.people) == 1
    assert graph.documents is not None and len(graph.documents) == 1
    assert graph.tickets is not None and len(graph.tickets) == 1
    assert graph.orders is not None and len(graph.orders) == 1
