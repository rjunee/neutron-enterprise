# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ne-schema — one LinkML source, generated Pydantic models + JSON Schema.

``operational.yaml`` (in ``schemas/``) is the single schema source; the Pydantic
models and JSON Schema in ``generated/`` are produced from it by
``ne schema generate`` (SPEC §3.1 tier-upgrade mechanism). This package re-exports
the generated operational entity models and the seed schema version.

The re-exports are resolved lazily (PEP 562) so the ``ne.schema.codegen`` driver
stays importable even before the artifacts have been generated (bootstrap) — the
codegen driver imports only stdlib and must not depend on generated modules.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

__version__ = "0.1.0"

# Attribute name -> (submodule, symbol) resolved on first access.
_LAZY: dict[str, tuple[str, str]] = {
    "SCHEMA_VERSION": ("ne.schema.generated._manifest", "SCHEMA_VERSION"),
    "Account": ("ne.schema.generated.operational", "Account"),
    "AccountStatus": ("ne.schema.generated.operational", "AccountStatus"),
    "Document": ("ne.schema.generated.operational", "Document"),
    "DocumentType": ("ne.schema.generated.operational", "DocumentType"),
    "OperationalRecordSet": ("ne.schema.generated.operational", "OperationalRecordSet"),
    "Order": ("ne.schema.generated.operational", "Order"),
    "OrderStatus": ("ne.schema.generated.operational", "OrderStatus"),
    "Person": ("ne.schema.generated.operational", "Person"),
    "Ticket": ("ne.schema.generated.operational", "Ticket"),
    "TicketPriority": ("ne.schema.generated.operational", "TicketPriority"),
    "TicketStatus": ("ne.schema.generated.operational", "TicketStatus"),
    # Event spine (NE-005) — second schema in the codegen registry.
    "EVENTS_SCHEMA_VERSION": ("ne.schema.generated._events_manifest", "SCHEMA_VERSION"),
    "PlatformEvent": ("ne.schema.generated.events", "PlatformEvent"),
    "AuditEvent": ("ne.schema.generated.events", "AuditEvent"),
    "CorrectionSignal": ("ne.schema.generated.events", "CorrectionSignal"),
    "ApprovalEvent": ("ne.schema.generated.events", "ApprovalEvent"),
    "CostEvent": ("ne.schema.generated.events", "CostEvent"),
    "ShapeViolation": ("ne.schema.generated.events", "ShapeViolation"),
    "EventRecordSet": ("ne.schema.generated.events", "EventRecordSet"),
    "AuditOutcome": ("ne.schema.generated.events", "AuditOutcome"),
    "CorrectionKind": ("ne.schema.generated.events", "CorrectionKind"),
    "ApprovalDecision": ("ne.schema.generated.events", "ApprovalDecision"),
    "ViolationSeverity": ("ne.schema.generated.events", "ViolationSeverity"),
    # Tier-2 entity-graph contracts (NE-081) — 4th, additive codegen artifact.
    "AccountEntity": ("ne.schema.generated.operational_graph", "AccountEntity"),
    "PersonEntity": ("ne.schema.generated.operational_graph", "PersonEntity"),
    "DocumentEntity": ("ne.schema.generated.operational_graph", "DocumentEntity"),
    "TicketEntity": ("ne.schema.generated.operational_graph", "TicketEntity"),
    "OrderEntity": ("ne.schema.generated.operational_graph", "OrderEntity"),
    "EdgeDef": ("ne.schema.generated.operational_graph", "EdgeDef"),
    "EDGE_DEFS": ("ne.schema.generated.operational_graph", "EDGE_DEFS"),
    "ENTITY_CLASSES": ("ne.schema.generated.operational_graph", "ENTITY_CLASSES"),
    "EDGE_KINDS": ("ne.schema.generated.operational_graph", "EDGE_KINDS"),
    "ENTITY_CONTRACTS": ("ne.schema.generated.operational_graph", "ENTITY_CONTRACTS"),
}

__all__ = [
    "EDGE_DEFS",
    "EDGE_KINDS",
    "ENTITY_CLASSES",
    "ENTITY_CONTRACTS",
    "EVENTS_SCHEMA_VERSION",
    "SCHEMA_VERSION",
    "Account",
    "AccountEntity",
    "AccountStatus",
    "ApprovalDecision",
    "ApprovalEvent",
    "AuditEvent",
    "AuditOutcome",
    "CorrectionKind",
    "CorrectionSignal",
    "CostEvent",
    "Document",
    "DocumentEntity",
    "DocumentType",
    "EdgeDef",
    "EventRecordSet",
    "OperationalRecordSet",
    "Order",
    "OrderEntity",
    "OrderStatus",
    "Person",
    "PersonEntity",
    "PlatformEvent",
    "ShapeViolation",
    "Ticket",
    "TicketEntity",
    "TicketPriority",
    "TicketStatus",
    "ViolationSeverity",
    "__version__",
]


def __getattr__(name: str) -> object:
    try:
        module_name, symbol = _LAZY[name]
    except KeyError as exc:  # pragma: no cover - standard attribute error path
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    import importlib

    return getattr(importlib.import_module(module_name), symbol)


if TYPE_CHECKING:  # give type-checkers/IDEs the concrete symbols
    from ne.schema.generated._events_manifest import SCHEMA_VERSION as EVENTS_SCHEMA_VERSION
    from ne.schema.generated._manifest import SCHEMA_VERSION as SCHEMA_VERSION
    from ne.schema.generated.events import ApprovalDecision as ApprovalDecision
    from ne.schema.generated.events import ApprovalEvent as ApprovalEvent
    from ne.schema.generated.events import AuditEvent as AuditEvent
    from ne.schema.generated.events import AuditOutcome as AuditOutcome
    from ne.schema.generated.events import CorrectionKind as CorrectionKind
    from ne.schema.generated.events import CorrectionSignal as CorrectionSignal
    from ne.schema.generated.events import CostEvent as CostEvent
    from ne.schema.generated.events import EventRecordSet as EventRecordSet
    from ne.schema.generated.events import PlatformEvent as PlatformEvent
    from ne.schema.generated.events import ShapeViolation as ShapeViolation
    from ne.schema.generated.events import ViolationSeverity as ViolationSeverity
    from ne.schema.generated.operational import Account as Account
    from ne.schema.generated.operational import AccountStatus as AccountStatus
    from ne.schema.generated.operational import Document as Document
    from ne.schema.generated.operational import DocumentType as DocumentType
    from ne.schema.generated.operational import OperationalRecordSet as OperationalRecordSet
    from ne.schema.generated.operational import Order as Order
    from ne.schema.generated.operational import OrderStatus as OrderStatus
    from ne.schema.generated.operational import Person as Person
    from ne.schema.generated.operational import Ticket as Ticket
    from ne.schema.generated.operational import TicketPriority as TicketPriority
    from ne.schema.generated.operational import TicketStatus as TicketStatus
    from ne.schema.generated.operational_graph import EDGE_DEFS as EDGE_DEFS
    from ne.schema.generated.operational_graph import EDGE_KINDS as EDGE_KINDS
    from ne.schema.generated.operational_graph import ENTITY_CLASSES as ENTITY_CLASSES
    from ne.schema.generated.operational_graph import ENTITY_CONTRACTS as ENTITY_CONTRACTS
    from ne.schema.generated.operational_graph import AccountEntity as AccountEntity
    from ne.schema.generated.operational_graph import DocumentEntity as DocumentEntity
    from ne.schema.generated.operational_graph import EdgeDef as EdgeDef
    from ne.schema.generated.operational_graph import OrderEntity as OrderEntity
    from ne.schema.generated.operational_graph import PersonEntity as PersonEntity
    from ne.schema.generated.operational_graph import TicketEntity as TicketEntity
