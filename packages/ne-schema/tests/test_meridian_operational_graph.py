# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""PRE-016C producer proof for the generated Meridian operational vocabulary."""

from __future__ import annotations

import enum
import json
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from ne.schema.generated.operational import (
    AutoLoan,
    AutoLoanStatus,
    Customer,
    CustomerStatus,
    OperationalRecordSet,
    PaymentSchedule,
    PaymentScheduleStatus,
    ServicingNote,
    ServicingNoteType,
)
from ne.schema.generated.operational_graph import (
    ENTITY_CONTRACTS,
    AutoLoanEntity,
    CustomerEntity,
    PaymentScheduleEntity,
    ServicingNoteEntity,
)

_JSON_SCHEMA = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "ne"
    / "schema"
    / "generated"
    / "operational.schema.json"
)

_MODELS: dict[str, type[BaseModel]] = {
    "Customer": Customer,
    "AutoLoan": AutoLoan,
    "PaymentSchedule": PaymentSchedule,
    "ServicingNote": ServicingNote,
}
_CONTRACTS: dict[str, type[BaseModel]] = {
    "Customer": CustomerEntity,
    "AutoLoan": AutoLoanEntity,
    "PaymentSchedule": PaymentScheduleEntity,
    "ServicingNote": ServicingNoteEntity,
}
_ENUMS: dict[str, type[enum.Enum]] = {
    "CustomerStatus": CustomerStatus,
    "AutoLoanStatus": AutoLoanStatus,
    "PaymentScheduleStatus": PaymentScheduleStatus,
    "ServicingNoteType": ServicingNoteType,
}


def _customer() -> Customer:
    return Customer(
        id="mer-cus-999",
        customer_identifier="mer-cus-999",
        first_name="Test",
        last_name="Customer",
        county="Example",
        status=CustomerStatus.active,
        has_auto_loan=["mer-loan-999"],
        has_servicing_note=["mer-note-999"],
    )


def _loan() -> AutoLoan:
    return AutoLoan(
        id="mer-loan-999",
        loan_identifier="mer-loan-999",
        status=AutoLoanStatus.active,
        principal_balance_cents=1,
        next_due_date="2026-09-01",
        has_payment_schedule=["mer-pay-999"],
        has_servicing_note=["mer-note-999"],
    )


def _payment() -> PaymentSchedule:
    return PaymentSchedule(
        id="mer-pay-999",
        payment_identifier="mer-pay-999",
        due_date="2026-09-01",
        amount_cents=1,
        status=PaymentScheduleStatus.scheduled,
    )


def _note() -> ServicingNote:
    return ServicingNote(
        id="mer-note-999",
        note_identifier="mer-note-999",
        ticket_identifier="mer-tkt-999",
        note_type=ServicingNoteType.contact,
        created_date="2026-08-01",
        text="Synthetic schema-control note.",
    )


def test_record_set_roundtrips_exact_meridian_path() -> None:
    records = OperationalRecordSet(
        customers=[_customer()],
        auto_loans=[_loan()],
        payment_schedules=[_payment()],
        servicing_notes=[_note()],
    )
    restored = OperationalRecordSet.model_validate_json(records.model_dump_json())
    assert restored == records
    assert restored.customers is not None
    assert restored.auto_loans is not None
    assert restored.customers[0].has_auto_loan == ["mer-loan-999"]
    assert restored.auto_loans[0].has_payment_schedule == ["mer-pay-999"]


def test_graph_contracts_preserve_scalars_and_exclude_relationships() -> None:
    assert {name: ENTITY_CONTRACTS[name] for name in _CONTRACTS} == _CONTRACTS
    relationship_fields = {"has_auto_loan", "has_payment_schedule", "has_servicing_note"}
    for contract in _CONTRACTS.values():
        assert not (set(contract.model_fields) & relationship_fields)

    customer = CustomerEntity(
        id="mer-cus-999",
        customer_identifier="mer-cus-999",
        first_name="Test",
        last_name="Customer",
        county="Example",
        status=CustomerStatus.active,
    )
    assert customer.customer_identifier == "mer-cus-999"


@pytest.mark.parametrize(
    "contract,payload",
    [
        (
            CustomerEntity,
            {
                "id": "mer-cus-999",
                "customer_identifier": "foreign-customer",
                "first_name": "Test",
                "last_name": "Customer",
                "county": "Example",
                "status": "active",
            },
        ),
        (
            AutoLoanEntity,
            {
                "id": "mer-loan-999",
                "loan_identifier": "mer-loan-999",
                "status": "unknown",
                "principal_balance_cents": 1,
                "next_due_date": "2026-09-01",
            },
        ),
        (
            PaymentScheduleEntity,
            {
                "id": "mer-pay-999",
                "payment_identifier": "mer-pay-999",
                "due_date": "2026-09-01",
                "amount_cents": -1,
                "status": "scheduled",
            },
        ),
        (
            PaymentScheduleEntity,
            {
                "id": "mer-pay-999",
                "payment_identifier": "mer-pay-999",
                "due_date": "09/01/2026",
                "amount_cents": 1,
                "status": "scheduled",
            },
        ),
        (
            ServicingNoteEntity,
            {
                "id": "mer-note-999",
                "note_identifier": "mer-note-999",
                "ticket_identifier": "mer-tkt-999",
                "note_type": "invented",
                "created_date": "2026-08-01",
                "text": "Synthetic schema-control note.",
            },
        ),
    ],
)
def test_graph_contracts_reject_malformed_values(
    contract: type[BaseModel], payload: dict[str, object]
) -> None:
    with pytest.raises(ValidationError):
        contract.model_validate(payload)


@pytest.mark.parametrize("contract", list(_CONTRACTS.values()))
def test_graph_contracts_reject_foreign_fields(contract: type[BaseModel]) -> None:
    payload: dict[str, object]
    if contract is CustomerEntity:
        payload = {
            "id": "mer-cus-999",
            "customer_identifier": "mer-cus-999",
            "first_name": "Test",
            "last_name": "Customer",
            "county": "Example",
            "status": "active",
        }
    elif contract is AutoLoanEntity:
        payload = {
            "id": "mer-loan-999",
            "loan_identifier": "mer-loan-999",
            "status": "active",
            "principal_balance_cents": 1,
            "next_due_date": "2026-09-01",
        }
    elif contract is PaymentScheduleEntity:
        payload = {
            "id": "mer-pay-999",
            "payment_identifier": "mer-pay-999",
            "due_date": "2026-09-01",
            "amount_cents": 1,
            "status": "scheduled",
        }
    else:
        payload = {
            "id": "mer-note-999",
            "note_identifier": "mer-note-999",
            "ticket_identifier": "mer-tkt-999",
            "note_type": "contact",
            "created_date": "2026-08-01",
            "text": "Synthetic schema-control note.",
        }
    payload["foreign_field"] = "must fail closed"
    with pytest.raises(ValidationError):
        contract.model_validate(payload)


def test_json_schema_matches_models_enums_and_constraints() -> None:
    defs = json.loads(_JSON_SCHEMA.read_text(encoding="utf-8"))["$defs"]
    for name, model in _MODELS.items():
        assert defs[name]["additionalProperties"] is False
        expected_required = {
            field_name for field_name, field in model.model_fields.items() if field.is_required()
        }
        assert set(defs[name].get("required", [])) == expected_required
    for name, enum_model in _ENUMS.items():
        assert set(defs[name]["enum"]) == {member.value for member in enum_model}

    assert defs["Customer"]["properties"]["customer_identifier"]["pattern"] == (
        "^mer-cus-[0-9]{3}$"
    )
    assert defs["AutoLoan"]["properties"]["loan_identifier"]["pattern"] == ("^mer-loan-[0-9]{3}$")
    assert defs["PaymentSchedule"]["properties"]["amount_cents"]["minimum"] == 0
    assert defs["AutoLoan"]["properties"]["principal_balance_cents"]["minimum"] == 0
    date_pattern = "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
    assert defs["AutoLoan"]["properties"]["next_due_date"]["pattern"] == date_pattern
    assert defs["PaymentSchedule"]["properties"]["due_date"]["pattern"] == date_pattern
    assert defs["ServicingNote"]["properties"]["created_date"]["pattern"] == date_pattern
