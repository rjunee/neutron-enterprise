# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The ONE-source invariant: the committed JSON Schema and the generated Pydantic
models agree on required fields and enum values (both come from operational.yaml)."""

from __future__ import annotations

import enum
import json
from pathlib import Path

import pytest
from pydantic import BaseModel

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

_JSON_SCHEMA = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "ne"
    / "schema"
    / "generated"
    / "operational.schema.json"
)

_ENTITIES: dict[str, type[BaseModel]] = {
    "Account": Account,
    "Person": Person,
    "Document": Document,
    "Ticket": Ticket,
    "Order": Order,
    "OperationalRecordSet": OperationalRecordSet,
}

_ENUMS: dict[str, type[enum.Enum]] = {
    "AccountStatus": AccountStatus,
    "DocumentType": DocumentType,
    "TicketStatus": TicketStatus,
    "TicketPriority": TicketPriority,
    "OrderStatus": OrderStatus,
}


def _schema() -> dict:
    return json.loads(_JSON_SCHEMA.read_text(encoding="utf-8"))


def test_schema_parses() -> None:
    assert isinstance(_schema(), dict)


def test_defs_contain_all_classes_and_enums() -> None:
    defs = _schema()["$defs"]
    for name in (*_ENTITIES, *_ENUMS):
        assert name in defs, f"{name} missing from JSON Schema $defs"


def test_root_targets_operational_record_set() -> None:
    schema = _schema()
    record_set_props = set(schema["$defs"]["OperationalRecordSet"]["properties"])
    assert set(schema["properties"]) == record_set_props


def test_closed_world_additional_properties_false() -> None:
    defs = _schema()["$defs"]
    for name in _ENTITIES:
        assert defs[name]["additionalProperties"] is False


@pytest.mark.parametrize("name", list(_ENTITIES))
def test_required_fields_match_pydantic(name: str) -> None:
    defs = _schema()["$defs"]
    schema_required = set(defs[name].get("required", []))
    model = _ENTITIES[name]
    pydantic_required = {
        field_name for field_name, info in model.model_fields.items() if info.is_required()
    }
    assert schema_required == pydantic_required


@pytest.mark.parametrize("name", list(_ENUMS))
def test_enum_values_match_pydantic(name: str) -> None:
    defs = _schema()["$defs"]
    schema_values = set(defs[name]["enum"])
    python_values = {member.value for member in _ENUMS[name]}
    assert schema_values == python_values


# --- events schema JSON Schema artifact --------------------------------------

_EVENTS_JSON_SCHEMA = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "ne"
    / "schema"
    / "generated"
    / "events.schema.json"
)

_EVENT_CLASSES = (
    "AuditEvent",
    "CorrectionSignal",
    "ApprovalEvent",
    "CostEvent",
    "ShapeViolation",
    "PlatformEvent",
)
_EVENT_ENUMS = ("AuditOutcome", "CorrectionKind", "ApprovalDecision", "ViolationSeverity")


def _events_schema() -> dict:
    return json.loads(_EVENTS_JSON_SCHEMA.read_text(encoding="utf-8"))


def test_events_root_targets_event_record_set() -> None:
    schema = _events_schema()
    record_set_props = set(schema["$defs"]["EventRecordSet"]["properties"])
    assert set(schema["properties"]) == record_set_props
    assert record_set_props == {
        "audit_events",
        "correction_signals",
        "approval_events",
        "cost_events",
        "shape_violations",
    }


def test_events_defs_contain_all_event_classes_and_enums() -> None:
    defs = _events_schema()["$defs"]
    for name in (*_EVENT_CLASSES, *_EVENT_ENUMS):
        assert name in defs, f"{name} missing from events JSON Schema $defs"
