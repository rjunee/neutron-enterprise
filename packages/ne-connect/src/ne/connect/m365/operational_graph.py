# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Strict Meridian operational-record envelopes carried by M365 driveItems.

The SharePoint object is the source: its immutable item id, version and ACL are
still observed by the normal M365 connector.  The JSON body contributes only a
schema-validated operational projection.  Provider metadata can therefore
never select an entity type, invent an edge, or widen an ACL.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ne.schema import EDGE_DEFS, ENTITY_CONTRACTS, SCHEMA_VERSION

__all__ = [
    "MERIDIAN_FRONTLINE_WITHHELD_CLASSES",
    "MERIDIAN_OPERATIONAL_ENTITY_CLASSES",
    "MERIDIAN_OPERATIONAL_MEDIA_TYPE",
    "MERIDIAN_OPERATIONAL_SCHEMA",
    "MERIDIAN_OPERATIONAL_SCHEMA_VERSION",
    "MERIDIAN_OPERATIONAL_SUFFIX",
    "OperationalGraphEdge",
    "OperationalGraphEnvelope",
    "OperationalGraphItemBinding",
    "canonical_operational_graph_bytes",
    "edge_payload_key",
    "field_payload_key",
    "manifest_record_key",
    "parse_manifest_record_key",
    "parse_operational_graph_body",
    "validate_binding_acls",
    "validate_binding_set",
]

MERIDIAN_OPERATIONAL_SCHEMA = "meridian-operational-record/v1"
MERIDIAN_OPERATIONAL_SCHEMA_VERSION = "1.1.0"
MERIDIAN_OPERATIONAL_MEDIA_TYPE = "application/json"
MERIDIAN_OPERATIONAL_SUFFIX = ".negraph.json"
MERIDIAN_OPERATIONAL_ENTITY_CLASSES = frozenset(
    {"Customer", "AutoLoan", "PaymentSchedule", "ServicingNote"}
)
#: The operational classes the frontline persona is NOT granted, in the seed and
#: therefore in every tier built from it. One set, used both where the seeder
#: AUTHORS the grants (``ne.meridian_seed.slice``) and where the manifest receipt
#: is VERIFIED (:func:`validate_binding_acls`), so the two can never disagree.
#:
#: Every account-level operational record is withheld: loan balances, payment
#: schedules and servicing notes are all ops/approver material. ``Customer`` is
#: deliberately NOT in the set -- it is the anchor a frontline user resolves a
#: person by, and the demo's graph traversal starts there.
#:
#: WHY THIS IS A SET AND NOT A PER-CLASS FLAG: the defect it closes was one
#: frontline principal getting two different verdicts for the same question
#: depending on which record type served it -- ``AutoLoan``/``PaymentSchedule``
#: readable while ``ServicingNote`` refused. A refusal that depends on the
#: retrieval path is not a governance posture, it is a leak with a narrow mouth.
MERIDIAN_FRONTLINE_WITHHELD_CLASSES = frozenset({"AutoLoan", "PaymentSchedule", "ServicingNote"})

_SCHEMA_VERSION = MERIDIAN_OPERATIONAL_SCHEMA_VERSION
if SCHEMA_VERSION != _SCHEMA_VERSION:  # fail loud on generated-schema drift
    raise RuntimeError(
        f"Meridian operational envelopes require generated schema 1.1.0 (loaded {SCHEMA_VERSION!r})"
    )

_EDGE_INDEX = frozenset(
    (edge.subject_class, edge.edge_kind, edge.object_class) for edge in EDGE_DEFS
)
_PAYLOAD_PREFIX = "ne_meridian_operational_"


def field_payload_key(field_name: str) -> str:
    """Return the connector-owned payload key for one typed entity field."""
    return f"{_PAYLOAD_PREFIX}field_{field_name}"


def edge_payload_key(edge_kind: str) -> str:
    """Return the connector-owned payload key for one relation's target ids."""
    return f"{_PAYLOAD_PREFIX}edge_{edge_kind.lower()}"


def manifest_record_key(entity_class: str, record_id: str) -> str:
    """Stable content-free key used by the private seed manifest."""
    if entity_class not in MERIDIAN_OPERATIONAL_ENTITY_CLASSES:
        raise ValueError(f"unsupported Meridian operational entity class {entity_class!r}")
    if not record_id or record_id != record_id.strip() or ":" in record_id:
        raise ValueError(
            "Meridian operational record id must be non-empty, trimmed, and colon-free"
        )
    return f"meridian-operational:{entity_class}:{record_id}"


def parse_manifest_record_key(value: str) -> tuple[str, str]:
    """Parse an exact :func:`manifest_record_key`, rejecting lookalikes."""
    parts = value.split(":")
    if len(parts) != 3 or parts[0] != "meridian-operational":
        raise ValueError(f"invalid Meridian operational manifest key {value!r}")
    entity_class, record_id = parts[1:]
    if manifest_record_key(entity_class, record_id) != value:
        raise ValueError(f"non-canonical Meridian operational manifest key {value!r}")
    return entity_class, record_id


class OperationalGraphEdge(BaseModel):
    """One source-authored, schema-admitted directed relation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    edge_kind: str
    target_class: str
    target_record_id: str = Field(min_length=1)


class OperationalGraphEnvelope(BaseModel):
    """The closed application/json body of one ``.negraph.json`` driveItem."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    schema_id: Literal["meridian-operational-record/v1"] = Field(
        alias="schema", serialization_alias="schema"
    )
    schema_version: Literal["1.1.0"]
    entity_class: Literal["Customer", "AutoLoan", "PaymentSchedule", "ServicingNote"]
    record_id: str = Field(min_length=1)
    fields: dict[str, Any]
    edges: tuple[OperationalGraphEdge, ...] = ()

    @model_validator(mode="after")
    def _validate_projection(self) -> OperationalGraphEnvelope:
        contract = ENTITY_CONTRACTS[self.entity_class]
        try:
            typed = contract.model_validate(self.fields)
        except ValidationError as exc:
            raise ValueError(f"invalid {self.entity_class} operational fields: {exc}") from exc
        canonical_fields = typed.model_dump(mode="json", exclude_none=True)
        if canonical_fields != self.fields:
            raise ValueError("operational fields must be the exact canonical schema projection")
        if self.fields.get("id") != self.record_id:
            raise ValueError("operational record_id must equal its typed entity id")
        seen: set[tuple[str, str, str]] = set()
        for edge in self.edges:
            triple = (self.entity_class, edge.edge_kind, edge.target_class)
            if triple not in _EDGE_INDEX:
                raise ValueError(f"operational edge {triple!r} is not in schema 1.1.0")
            identity = (edge.edge_kind, edge.target_class, edge.target_record_id)
            if identity in seen:
                raise ValueError(f"duplicate operational edge {identity!r}")
            seen.add(identity)
        ordered = tuple(sorted(self.edges, key=lambda edge: edge.model_dump_json()))
        if self.edges != ordered:
            raise ValueError("operational edges must use canonical sorted order")
        return self


class OperationalGraphItemBinding(BaseModel):
    """Content-free seed receipt used to construct exact live-item mappings."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    record_id: str = Field(min_length=1)
    entity_class: Literal["Customer", "AutoLoan", "PaymentSchedule", "ServicingNote"]
    item_id: str = Field(min_length=1)
    path: str = Field(min_length=1)
    acl: tuple[str, ...] = Field(min_length=1)
    edges: tuple[OperationalGraphEdge, ...] = ()


def canonical_operational_graph_bytes(envelope: OperationalGraphEnvelope) -> bytes:
    """Canonical source bytes; equality is checked again by the connector."""
    payload = json.dumps(
        envelope.model_dump(mode="json", by_alias=True),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return (payload + "\n").encode("utf-8")


def _declared_operational(name: object, mime_type: object) -> bool:
    del mime_type
    return isinstance(name, str) and (
        name.endswith(MERIDIAN_OPERATIONAL_SUFFIX) or ".negraph." in name
    )


def parse_operational_graph_body(
    content: bytes,
    *,
    name: object,
    mime_type: object,
) -> dict[str, Any] | None:
    """Validate and flatten one declared body, or return ``None`` if ordinary.

    A suffix/MIME mismatch is a malformed declared record, not an unsupported
    document.  This distinction prevents a bad structured object from advancing
    the connector delta cursor as metadata-only content.
    """
    if not _declared_operational(name, mime_type):
        return None
    if (
        not isinstance(name, str)
        or not name.endswith(MERIDIAN_OPERATIONAL_SUFFIX)
        or mime_type != MERIDIAN_OPERATIONAL_MEDIA_TYPE
    ):
        raise ValueError(
            "Meridian operational driveItem requires canonical .negraph.json suffix "
            "and application/json MIME"
        )
    try:
        raw = json.loads(content)
        envelope = OperationalGraphEnvelope.model_validate(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, ValidationError, ValueError) as exc:
        raise ValueError("invalid Meridian operational record envelope") from exc
    if content != canonical_operational_graph_bytes(envelope):
        raise ValueError("Meridian operational record body is not canonical JSON")

    payload: dict[str, Any] = {
        "body_ingestion_status": "structured",
        f"{_PAYLOAD_PREFIX}schema": envelope.schema_id,
        f"{_PAYLOAD_PREFIX}schema_version": envelope.schema_version,
        f"{_PAYLOAD_PREFIX}entity_class": envelope.entity_class,
        f"{_PAYLOAD_PREFIX}record_id": envelope.record_id,
        f"{_PAYLOAD_PREFIX}content_sha256": hashlib.sha256(content).hexdigest(),
    }
    payload.update({field_payload_key(key): value for key, value in envelope.fields.items()})
    edges: dict[str, list[str]] = {}
    for edge in envelope.edges:
        edges.setdefault(edge.edge_kind, []).append(edge.target_record_id)
    payload.update({edge_payload_key(key): values for key, values in sorted(edges.items())})
    return payload


def validate_binding_set(bindings: Sequence[OperationalGraphItemBinding]) -> None:
    """Validate the exact closed 45-record/40-edge Meridian population receipt."""
    expected_counts = {"Customer": 15, "AutoLoan": 10, "PaymentSchedule": 10, "ServicingNote": 10}
    counts = {entity_class: 0 for entity_class in expected_counts}
    records: dict[tuple[str, str], OperationalGraphItemBinding] = {}
    item_ids: set[str] = set()
    edge_counts: dict[tuple[str, str, str], int] = {}
    for binding in bindings:
        expected_path = (
            f"graph-{binding.entity_class.lower()}-{binding.record_id}{MERIDIAN_OPERATIONAL_SUFFIX}"
        )
        if binding.path != expected_path:
            raise ValueError(
                "Meridian operational binding path is not its canonical .negraph.json name"
            )
        counts[binding.entity_class] += 1
        identity = (binding.entity_class, binding.record_id)
        if identity in records or binding.item_id in item_ids:
            raise ValueError("Meridian operational bindings contain duplicate record/item identity")
        records[identity] = binding
        item_ids.add(binding.item_id)
        for edge in binding.edges:
            triple = (binding.entity_class, edge.edge_kind, edge.target_class)
            edge_counts[triple] = edge_counts.get(triple, 0) + 1
            if (edge.target_class, edge.target_record_id) not in records:
                # Full target validation is repeated after all records are indexed below.
                continue
    if counts != expected_counts:
        raise ValueError(
            f"Meridian operational bindings require exact entity counts {expected_counts!r}"
        )
    for binding in bindings:
        for edge in binding.edges:
            if (edge.target_class, edge.target_record_id) not in records:
                raise ValueError(
                    "Meridian operational edge target is absent from exact binding set"
                )
    expected_edges = {
        ("Customer", "HAS_AUTO_LOAN", "AutoLoan"): 10,
        ("Customer", "HAS_SERVICING_NOTE", "ServicingNote"): 10,
        ("AutoLoan", "HAS_PAYMENT_SCHEDULE", "PaymentSchedule"): 10,
        ("AutoLoan", "HAS_SERVICING_NOTE", "ServicingNote"): 10,
    }
    if edge_counts != expected_edges:
        raise ValueError(
            f"Meridian operational bindings require exact edge counts {expected_edges!r}"
        )


def validate_binding_acls(
    bindings: Sequence[OperationalGraphItemBinding],
    *,
    ops_group: str,
    approver_group: str,
    frontline_group: str,
) -> None:
    """Prove every live binding retained its exact persona-group ACL.

    The expected set is derived from :data:`MERIDIAN_FRONTLINE_WITHHELD_CLASSES`,
    the same constant the seeder grants from, so a receipt can only pass if the
    live item carries exactly what the slice authored.
    """
    if len({ops_group, approver_group, frontline_group}) != 3 or any(
        not group or group != group.strip()
        for group in (ops_group, approver_group, frontline_group)
    ):
        raise ValueError("Meridian operational ACL identities must be three distinct group ids")
    for binding in bindings:
        expected = (
            (ops_group, approver_group)
            if binding.entity_class in MERIDIAN_FRONTLINE_WITHHELD_CLASSES
            else (ops_group, approver_group, frontline_group)
        )
        if binding.acl != expected:
            raise ValueError(
                f"Meridian operational {binding.entity_class} ACL is not its exact group set"
            )
