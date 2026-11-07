# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Exact live-item mapping for the Meridian operational M365 population."""

from __future__ import annotations

from collections.abc import Sequence

from ne.brain_graph.config import GraphEdgeMapping, GraphEntityMapping, GraphSourceMapping
from ne.connect.m365.operational_graph import (
    OperationalGraphItemBinding,
    edge_payload_key,
    field_payload_key,
    validate_binding_set,
)

__all__ = ["meridian_m365_graph_mapping", "validate_meridian_m365_graph_mapping"]

_FIELDS: dict[str, tuple[str, ...]] = {
    "Customer": ("id", "customer_identifier", "first_name", "last_name", "county", "status"),
    "AutoLoan": (
        "id",
        "loan_identifier",
        "status",
        "principal_balance_cents",
        "next_due_date",
    ),
    "PaymentSchedule": ("id", "payment_identifier", "due_date", "amount_cents", "status"),
    "ServicingNote": (
        "id",
        "note_identifier",
        "ticket_identifier",
        "note_type",
        "created_date",
        "text",
    ),
}


def _source_id(drive_id: str, item_id: str) -> str:
    return f"{drive_id}:{item_id}"


def meridian_m365_graph_mapping(
    *,
    source_system: str,
    drive_id: str,
    bindings: Sequence[OperationalGraphItemBinding],
) -> GraphSourceMapping:
    """Build one Document fallback plus the exact 45 live-item projections."""
    validate_binding_set(bindings)
    by_record = {(binding.entity_class, binding.record_id): binding for binding in bindings}
    entities: list[GraphEntityMapping] = [
        GraphEntityMapping(
            source_id_prefix=drive_id,
            entity_class="Document",
            fields={"id": "id", "title": "name", "doc_type": "graph_doc_type"},
        )
    ]
    for binding in sorted(bindings, key=lambda item: (item.entity_class, item.record_id)):
        edges: list[GraphEdgeMapping] = []
        for edge in binding.edges:
            target = by_record[(edge.target_class, edge.target_record_id)]
            edges.append(
                GraphEdgeMapping(
                    payload_key=edge_payload_key(edge.edge_kind),
                    edge_kind=edge.edge_kind,
                    target_class=edge.target_class,
                    target_source_id_template=_source_id(drive_id, target.item_id),
                    target_record_id_template=target.item_id,
                    expected_value=edge.target_record_id,
                )
            )
        entities.append(
            GraphEntityMapping(
                source_id_prefix=_source_id(drive_id, binding.item_id),
                entity_class=binding.entity_class,
                fields={field: field_payload_key(field) for field in _FIELDS[binding.entity_class]},
                edges=tuple(edges),
            )
        )
    mapping = GraphSourceMapping(source_system=source_system, entities=tuple(entities))
    validate_meridian_m365_graph_mapping(mapping, drive_ids=(drive_id,))
    return mapping


def validate_meridian_m365_graph_mapping(
    mapping: GraphSourceMapping,
    *,
    drive_ids: Sequence[str],
) -> None:
    """Fail closed unless a mapping is one exact 45-record/40-edge population.

    WHY THIS IS CLOSED, and what widening it actually costs (POST_DEMO #17).

    The counts are not a stylistic constraint on this file: they are the exact
    set of ``.negraph.json`` driveItems the seeder uploaded to the live
    SharePoint drive, whose item ids the seed manifest carries as
    ``OperationalGraphItemBinding`` receipts. A mapping entry for an item that
    does not exist in the drive produces no vertex -- the construct runner
    resolves the mapping BY ``source_id`` off the connector stream and counts a
    miss as ``unmapped``. So a wider mapping without a wider seed is a silently
    empty widening, which is precisely what this validator refuses to let ship.

    T3 projects SEVEN classes; T2 holds four operational ones plus the Document
    fallback. The genuine gap is TWO classes -- ``DisasterDeclaration`` (DR-4830)
    and ``RegEDispute`` (6 rows). It is NOT three: T3's ``DisputePolicy`` rows
    are the two ``debit-card-dispute-sop`` artifacts, which share ONE SharePoint
    path ("Debit Card Dispute Handling SOP.docx") and are already in T2 as one
    versioned ``Document``; RDF has no version dimension, so T3 splits into two
    subjects what T1 lineage and T2 carry as one item with two versions.

    Bringing the two real classes in is a FOUR-layer change, in this order:

    1. ``packages/ne-schema/src/ne/schema/schemas/operational.yaml`` gains both
       classes and their edges, and ``version`` moves off 1.1.0. The generated
       tree must then be regenerated with ``ne schema generate`` (the isolated
       hash-pinned linkml venv) because CI byte-compares it
       (``test_generated_freshness.py``). Nothing downstream can admit a class
       ``ENTITY_CONTRACTS`` and ``EDGE_DEFS`` do not carry.
    2. ``ne.connect.m365.operational_graph`` widens
       ``MERIDIAN_OPERATIONAL_ENTITY_CLASSES`` and both ``Literal`` unions, and
       its 1.1.0 fail-loud pin follows the new schema version.
    3. ``tools/meridian-seed``'s slice emits the new ``.negraph.json`` bodies,
       and this file gains their ``_FIELDS`` plus the new expected population:
       ``DisasterDeclaration`` 1, ``RegEDispute`` 6 -> 52 records / 53 mapping
       entries, and 57 edges (40 today, plus DR-4830 -> the 11 Customers in
       Columbia/Richmond/Jefferson, plus one per RegEDispute -> its Customer).
       A RegEDispute -> AutoLoan edge would be INVENTED: a Reg E dispute is
       bound to a DebitCard and a DepositAccount, neither of which T2 holds.
    4. The seeder RE-RUNS against the live drive to create those items, and the
       manifest is re-issued with their real item ids; only then do the m365
       sync and the graph construct produce the vertices.

    Step 4 is the one that cannot be done from a code change, and it is why
    ``DR-4830`` is not a graph node and Beat 2a had to anchor on customer
    display names: the disaster declaration reaches T1 only as the content of
    "Disaster Declaration County Reference.csv", whose Document vertex is keyed
    by an opaque SharePoint item id and carries no ``DR-4830`` identity to
    address a path from.
    """
    drives = tuple(drive_ids)
    if len(drives) != 1 or not drives[0] or len(set(drives)) != 1:
        raise ValueError("Meridian M365 graph mapping requires exactly one resolved drive")
    drive_id = drives[0]
    fallback = [
        entity
        for entity in mapping.entities
        if entity.source_id_prefix == drive_id and entity.entity_class == "Document"
    ]
    if (
        len(fallback) != 1
        or fallback[0].fields
        != {
            "id": "id",
            "title": "name",
            "doc_type": "graph_doc_type",
        }
        or fallback[0].edges
    ):
        raise ValueError("Meridian M365 graph mapping lacks its exact Document fallback")
    operational = [entity for entity in mapping.entities if entity not in fallback]
    prefixes = [entity.source_id_prefix for entity in mapping.entities]
    if len(prefixes) != len(set(prefixes)):
        raise ValueError("Meridian M365 graph mapping contains duplicate source prefixes")
    operational_prefixes = {entity.source_id_prefix for entity in operational}
    counts = {entity_class: 0 for entity_class in _FIELDS}
    edge_counts: dict[tuple[str, str, str], int] = {}
    for entity in operational:
        if entity.entity_class not in counts:
            raise ValueError("Meridian M365 graph mapping contains a foreign entity class")
        if not entity.source_id_prefix.startswith(f"{drive_id}:"):
            raise ValueError("Meridian M365 graph mapping contains a foreign item prefix")
        if entity.fields != {
            field: field_payload_key(field) for field in _FIELDS[entity.entity_class]
        }:
            raise ValueError("Meridian M365 graph mapping has a non-canonical field projection")
        counts[entity.entity_class] += 1
        for edge in entity.edges:
            if edge.expected_value is None:
                raise ValueError("Meridian M365 graph edge lacks its exact payload binding")
            if not edge.target_source_id_template.startswith(f"{drive_id}:"):
                raise ValueError("Meridian M365 graph edge targets a foreign drive")
            if edge.target_source_id_template not in operational_prefixes:
                raise ValueError(
                    "Meridian M365 graph mapping is not the exact closed population: "
                    "edge target is absent"
                )
            if edge.target_record_id_template != edge.target_source_id_template.split(":", 1)[1]:
                raise ValueError("Meridian M365 graph edge target item identity is inconsistent")
            triple = (entity.entity_class, edge.edge_kind, edge.target_class)
            edge_counts[triple] = edge_counts.get(triple, 0) + 1
    expected_counts = {"Customer": 15, "AutoLoan": 10, "PaymentSchedule": 10, "ServicingNote": 10}
    expected_edges = {
        ("Customer", "HAS_AUTO_LOAN", "AutoLoan"): 10,
        ("Customer", "HAS_SERVICING_NOTE", "ServicingNote"): 10,
        ("AutoLoan", "HAS_PAYMENT_SCHEDULE", "PaymentSchedule"): 10,
        ("AutoLoan", "HAS_SERVICING_NOTE", "ServicingNote"): 10,
    }
    if counts != expected_counts or edge_counts != expected_edges or len(mapping.entities) != 46:
        raise ValueError("Meridian M365 graph mapping is not the exact closed 45/40 population")
