# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Checkpoint-C re-check: NE-004 seed schema fitness vs actual T1 + event-spine use.

Asserts the fitness facts the Checkpoint-C panel reviews: the operational seed
entities carry the T1-provenance fields the ingestion path stamps, their ids are
str (joinable to ``ne.contracts.types.Chunk.document_id``), and the event spine's
``PlatformEvent`` mandates ``config_hash`` (SPEC §4) with an optional ``kpi_tag``
(C8). Misfits surfaced for the panel are documented, not asserted away.
"""

from __future__ import annotations

from ne.contracts.types import Chunk
from ne.schema.generated.events import PlatformEvent
from ne.schema.generated.operational import (
    Account,
    Document,
    OperationalEntity,
    Order,
    Person,
    Ticket,
)


def test_document_provenance_fields() -> None:
    fields = Document.model_fields
    for required in ("id", "title", "doc_type"):
        assert fields[required].is_required(), f"Document.{required} must be required (T1 ingest)"
    for optional in ("uri", "mime_type", "source_system", "source_record_id"):
        assert optional in fields, f"Document missing provenance field {optional!r}"
        assert not fields[optional].is_required(), f"Document.{optional} should be optional"


def test_operational_entity_id_joinable_to_chunk_document_id() -> None:
    # ne.brain.ingest.contracts.ParsedDocument.document_id (str) flows into
    # ne.contracts.types.Chunk.document_id (str); the T1 join key must match.
    assert OperationalEntity.model_fields["id"].annotation is str
    assert Chunk.model_fields["document_id"].annotation is str


def test_seed_entities_exist_and_subclass_operational_entity() -> None:
    for entity in (Account, Person, Document, Ticket, Order):
        assert issubclass(entity, OperationalEntity), (
            f"{entity.__name__} must be an OperationalEntity"
        )


def test_platform_event_config_hash_and_kpi_tag() -> None:
    fields = PlatformEvent.model_fields
    assert fields["config_hash"].is_required(), "PlatformEvent.config_hash is required (SPEC §4)"
    assert fields["config_hash"].annotation is str
    assert "kpi_tag" in fields, "PlatformEvent must declare an optional kpi_tag (C8)"
    assert not fields["kpi_tag"].is_required(), "kpi_tag must be optional"


def test_surfaced_misfits_are_real() -> None:
    # These are the concrete misfits raised for the Checkpoint-C panel (see the
    # checkpoint doc). Encoding them as assertions makes the doc's claims testable.
    # (1) Document has no chunk-level ACL mirror though Chunk.acl exists.
    assert "acl" in Chunk.model_fields
    assert "acl" not in Document.model_fields
    # (2) Document has no content-hash / version field for NE-026 doc-versioned idempotency.
    assert "content_hash" not in Document.model_fields
    assert "version" not in Document.model_fields
