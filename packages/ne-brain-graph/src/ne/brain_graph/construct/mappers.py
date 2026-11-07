# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Pure, deterministic Record -> typed entity + edges mapping (tier2-spec §4.3).

``map_record`` projects a connector ``Record.payload`` through the declarative
``GraphEntityMapping`` into the generated ``ENTITY_CONTRACTS`` model (extra
forbidden). A validation failure QUARANTINES that record (counted, batch
continues — never a partial node). Node/edge keys are deterministic UUIDv5
(``entity_key_for``/``edge_key_for``); the content hash is a sha256 over the
canonical JSON of the TYPED projection PLUS each edge's ``(edge_kind, target_key)``
(DECISION-2 — so an FK-only change registers as a content change and the stale
outgoing edge is deleted, not vacuously kept). Attribute-and-edge-irrelevant
source churn does not re-upsert. No clock reads — same input, same output.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from ne.brain_graph.config import GraphEntityMapping
from ne.brain_graph.model import edge_key_for, entity_key_for
from ne.connect.identity import source_acl_hash, source_ref_for, source_snapshot_hash
from ne.contracts.integration import Record
from ne.contracts.source_authority import SourceAuthorityStamp
from ne.schema import ENTITY_CONTRACTS

__all__ = [
    "DISPLAY_PROPERTY",
    "MappedEdge",
    "MappedEntity",
    "QuarantinedRecord",
    "acl_hash_for",
    "map_record",
]

#: How each entity's ``display_name`` node property is composed (DECIDED §4.3).
DISPLAY_PROPERTY: dict[str, tuple[str, ...]] = {
    "Account": ("name",),
    "Person": ("given_name", "family_name"),
    "Document": ("title",),
    "Ticket": ("subject",),
    "Order": ("order_number",),
    "Customer": ("first_name", "last_name"),
    "AutoLoan": ("loan_identifier",),
    "PaymentSchedule": ("payment_identifier",),
    "ServicingNote": ("note_identifier",),
}


@dataclass(frozen=True)
class MappedEdge:
    props: dict[str, Any]
    target_key: str


@dataclass(frozen=True)
class MappedEntity:
    key: str
    entity_class: str
    content_hash: str
    acl_hash: str
    source_system: str
    source_ref: str
    source_snapshot_hash: str
    acl: tuple[str, ...]
    node_props: dict[str, Any]
    edges: tuple[MappedEdge, ...] = ()


@dataclass(frozen=True)
class QuarantinedRecord:
    reason: str
    fields: dict[str, Any] = field(default_factory=dict)


def _canonical_json(payload: Mapping[str, Any]) -> str:
    """Deterministic JSON (sorted keys) — local reimplementation, no private import."""
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)


def acl_hash_for(acl: Sequence[str]) -> str:
    return source_acl_hash(tuple(acl))


def _display_name(entity_class: str, typed: Mapping[str, Any]) -> str:
    parts = [str(typed.get(f, "")) for f in DISPLAY_PROPERTY.get(entity_class, ())]
    return " ".join(p for p in parts if p).strip()


def _iter_fk_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if v is not None and str(v) != ""]
    if str(value) == "":
        return []
    return [str(value)]


def map_record(
    record: Record,
    mapping: GraphEntityMapping,
    *,
    source_system: str,
    acl: tuple[str, ...],
    config_hash: str,
    schema_version: str,
    source_authority_stamps: tuple[SourceAuthorityStamp, ...] = (),
) -> MappedEntity | QuarantinedRecord:
    entity_class = mapping.entity_class
    contract = ENTITY_CONTRACTS[entity_class]
    payload = record.payload

    projection = {
        field_name: payload[payload_key]
        for field_name, payload_key in mapping.fields.items()
        if payload_key in payload
    }
    try:
        model = contract.model_validate(projection)
    except ValidationError as exc:
        return QuarantinedRecord(reason=f"contract validation failed: {exc}", fields=projection)

    typed = model.model_dump(mode="json", exclude_none=True)

    source_ref = source_ref_for(record)
    snapshot_hash = source_snapshot_hash(source_system, record)
    key = entity_key_for(source_system, entity_class, record.source_id, record.record_id)
    display_name = _display_name(entity_class, typed)

    # Edges are computed BEFORE the content hash so their FK targets can be folded
    # in (DECISION-2): an FK-only change (attributes identical, a changed/removed
    # edge target) must register as a content change so the 3-way UPSERT path fires
    # and the stale outgoing edge is deleted — otherwise the delete is vacuous.
    edges: list[MappedEdge] = []
    for edge_map in mapping.edges:
        raw = payload.get(edge_map.payload_key)
        if edge_map.expected_value is not None and _iter_fk_values(raw) != [
            edge_map.expected_value
        ]:
            return QuarantinedRecord(
                reason="operational edge payload does not match its exact admitted target",
                fields={edge_map.payload_key: raw},
            )
        for target_record_id in _iter_fk_values(raw):
            target_source_id = edge_map.target_source_id_template.replace(
                "{value}", target_record_id
            )
            target_record_key = edge_map.target_record_id_template.replace(
                "{value}", target_record_id
            )
            target_key = entity_key_for(
                source_system, edge_map.target_class, target_source_id, target_record_key
            )
            edge_key = edge_key_for(edge_map.edge_kind, key, target_key, source_ref)
            edge_props: dict[str, Any] = {
                "key": edge_key,
                "edge_kind": edge_map.edge_kind,
                "from_key": key,
                "to_key": target_key,
                "source_system": source_system,
                "source_ref": source_ref,
                "acl": list(acl),
                "config_hash": config_hash,
                "source_authority_stamps": [
                    stamp.model_dump(mode="json") for stamp in source_authority_stamps
                ],
            }
            edges.append(MappedEdge(props=edge_props, target_key=target_key))

    # Projection identity is load-bearing provenance: a mapping, generated schema,
    # or platform configuration revision must rewrite the served node even when
    # the source attributes happen to serialize identically.
    projection_revision = hashlib.sha256(
        _canonical_json(
            {
                "mapping": mapping.model_dump(mode="json"),
                "schema_version": schema_version,
                "config_hash": config_hash,
                "source_authority_stamps": [
                    stamp.model_dump(mode="json") for stamp in source_authority_stamps
                ],
            }
        ).encode()
    ).hexdigest()

    # Fold each edge's (edge_kind, target_key) into the fingerprint (DECISION-2).
    edge_fingerprint = sorted([e.props["edge_kind"], e.target_key] for e in edges)
    content_hash = hashlib.sha256(
        _canonical_json(
            {
                "attributes": typed,
                "edges": edge_fingerprint,
                "projection_revision": projection_revision,
            }
        ).encode()
    ).hexdigest()

    node_props: dict[str, Any] = {
        **typed,
        "key": key,
        "entity_class": entity_class,
        "display_name": display_name,
        "source_system": source_system,
        "source_ref": source_ref,
        "acl": list(acl),
        "content_hash": content_hash,
        "config_hash": config_hash,
        "schema_version": schema_version,
        "projection_revision": projection_revision,
        "source_authority_stamps": [
            stamp.model_dump(mode="json") for stamp in source_authority_stamps
        ],
    }

    return MappedEntity(
        key=key,
        entity_class=entity_class,
        content_hash=content_hash,
        acl_hash=acl_hash_for(acl),
        source_system=source_system,
        source_ref=source_ref,
        source_snapshot_hash=snapshot_hash,
        acl=tuple(acl),
        node_props=node_props,
        edges=tuple(edges),
    )
