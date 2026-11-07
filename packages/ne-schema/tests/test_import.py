# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Import smoke test for ne-schema."""

from __future__ import annotations


def test_import_and_version() -> None:
    import ne.schema

    assert ne.schema.__version__ == "0.1.0"


def test_schema_version_reexport() -> None:
    import ne.schema

    assert ne.schema.SCHEMA_VERSION == "1.1.0"


def test_all_entities_and_enums_importable() -> None:
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

    classes = [Account, Person, Document, Ticket, Order, OperationalRecordSet]
    enums = [AccountStatus, DocumentType, TicketStatus, TicketPriority, OrderStatus]
    assert all(isinstance(c, type) for c in classes)
    assert all(isinstance(e, type) for e in enums)


def test_events_schema_version_reexport() -> None:
    import ne.schema

    assert ne.schema.EVENTS_SCHEMA_VERSION == "1.1.0"


def test_all_event_classes_and_enums_importable() -> None:
    from ne.schema import (
        ApprovalDecision,
        ApprovalEvent,
        AuditEvent,
        AuditOutcome,
        CorrectionKind,
        CorrectionSignal,
        CostEvent,
        EventRecordSet,
        PlatformEvent,
        ShapeViolation,
        ViolationSeverity,
    )

    classes = [
        PlatformEvent,
        AuditEvent,
        CorrectionSignal,
        ApprovalEvent,
        CostEvent,
        ShapeViolation,
        EventRecordSet,
    ]
    enums = [AuditOutcome, CorrectionKind, ApprovalDecision, ViolationSeverity]
    assert all(isinstance(c, type) for c in classes)
    assert all(isinstance(e, type) for e in enums)
