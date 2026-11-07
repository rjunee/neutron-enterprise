# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-085b: deterministic property-graph->RDF mapping unit tests (recording store)."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any, cast

from rdflib import Graph, URIRef
from rdflib.namespace import SH

from ne.brain_graph.er.merge import ER_SOURCE_SYSTEM, SAME_AS_EDGE_KIND
from ne.brain_graph.testing.memory import InMemoryGraphStore
from ne.brain_ontology.acl import graph_iri_for_label
from ne.brain_ontology.gateway import DeclaredLiteral, ValidatingTripleStore
from ne.brain_ontology.lift import graph_to_rdf as lift_mod
from ne.brain_ontology.lift.graph_to_rdf import (
    NEA,
    NEOP,
    PropertyGraphLift,
    entity_iri,
    sameas_iri,
)
from ne.contracts.source_authority import (
    M365SourcePoint,
    SourceAuthorityStamp,
    SourceContentArtifactIdentity,
    SourceContractIdentity,
    SourceIdentity,
)
from ne.schema import EDGE_DEFS, ENTITY_CLASSES

_RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
_XSD_INTEGER = "http://www.w3.org/2001/XMLSchema#integer"
_XSD_BOOLEAN = "http://www.w3.org/2001/XMLSchema#boolean"


def _authority_stamp() -> SourceAuthorityStamp:
    contract = SourceContractIdentity(
        source_kind="m365-sharepoint", descriptor_schema_version=1, descriptor_hash="a" * 64
    )
    source = SourceIdentity(
        deployment_id="deployment-a",
        source_kind="m365-sharepoint",
        connector_instance_id="connector-a",
        point=M365SourcePoint(
            graph_tenant_id="tenant-a", site_id="", drive_id="drive-a", item_id="item-a"
        ),
        generation=1,
    )
    artifact = SourceContentArtifactIdentity(
        source_identity=source,
        source_snapshot_hash="b" * 64,
        content_snapshot_hash="c" * 64,
        acl_snapshot_hash="d" * 64,
        source_contract=contract,
        artifact_schema_version=1,
    )
    return SourceAuthorityStamp(
        source_identity=source,
        artifact_identity=artifact,
        selected_head_revision="head-a",
        acl_snapshot_hash=artifact.acl_snapshot_hash,
        source_contract=contract,
        contract_epoch=1,
        authority_receipt_id="receipt-a",
        authority_receipt_hash="e" * 64,
    )


class _RecordingStore:
    """A gateway stand-in that records (triples, graph_iri) without validating."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[tuple[str, str, str | DeclaredLiteral], ...], str]] = []
        self.contexts: list[tuple[tuple[str, str, str | DeclaredLiteral], ...]] = []

    async def insert_triples_into(
        self,
        triples: Sequence[tuple[str, str, str | DeclaredLiteral]],
        *,
        graph_iri: str,
        context_triples: Sequence[tuple[str, str, str | DeclaredLiteral]] = (),
    ) -> None:
        self.calls.append((tuple(triples), graph_iri))
        self.contexts.append(tuple(context_triples))


def _account_node(key: str, acl: list[str]) -> dict[str, Any]:
    # Shaped exactly like construct/mappers.py map_record output.
    return {
        "key": key,
        "entity_class": "Account",
        "display_name": "Acme Corp",
        "source_system": "crm",
        "source_ref": "acme:1",
        "acl": acl,
        "content_hash": "h",
        "config_hash": "c",
        "schema_version": "v",
        "id": "a1",
        "name": "Acme Corp",
        "employee_count": 412,
    }


def _person_node(key: str, acl: list[str]) -> dict[str, Any]:
    return {
        "key": key,
        "entity_class": "Person",
        "display_name": "Ada Lovelace",
        "source_system": "crm",
        "source_ref": "ada:1",
        "acl": acl,
        "content_hash": "h",
        "config_hash": "c",
        "schema_version": "v",
        "id": "p1",
        "given_name": "Ada",
        "family_name": "Lovelace",
        "email": "ada@example.org",
        "is_active": True,
    }


async def _lift(nodes: list[Mapping[str, Any]], edges: list[Mapping[str, Any]]):
    mem = InMemoryGraphStore()
    if nodes:
        await mem.upsert_nodes(nodes)
    if edges:
        await mem.upsert_edges(edges)
    store = _RecordingStore()
    report = await PropertyGraphLift(mem, store).run()  # type: ignore[arg-type]
    return report, store


def test_stable_iri_minting() -> None:
    assert entity_iri("abc-123") == "urn:ne:entity:abc-123"
    assert sameas_iri("edge-9") == "urn:ne:sameas:edge-9"


def test_account_node_maps_to_exact_triple_set() -> None:
    triples = set(PropertyGraphLift._node_triples(_account_node("k", ["alpha"]), "Account"))
    iri = "urn:ne:entity:k"
    # Slot objects are slot-datatype-authoritative DeclaredLiterals (FIX-1): the lift
    # carries the shape-declared datatype (untyped xsd:string id/name → None,
    # xsd:integer employee_count) so the gateway never re-guesses IRI-vs-literal.
    assert triples == {
        (iri, _RDF_TYPE, NEOP + "Account"),
        (iri, NEOP + "id", DeclaredLiteral("a1", None)),
        (iri, NEOP + "name", DeclaredLiteral("Acme Corp", None)),
        (iri, NEOP + "employee_count", DeclaredLiteral("412", _XSD_INTEGER)),
    }


def test_bool_maps_to_xsd_lexical_true_false() -> None:
    triples = dict(
        (p, o) for _s, p, o in PropertyGraphLift._node_triples(_person_node("k", ["a"]), "Person")
    )
    assert triples[NEOP + "is_active"] == DeclaredLiteral("true", _XSD_BOOLEAN)
    # Negative control: str(True) would be 'True' and fail sh:datatype xsd:boolean.
    assert triples[NEOP + "is_active"] != DeclaredLiteral("True", _XSD_BOOLEAN)


def test_iri_shaped_xsd_string_slot_maps_to_declared_literal() -> None:
    # FIX-1 (unit level): Document.uri is an xsd:string slot holding an IRI-shaped
    # value; the lift must emit a DeclaredLiteral (never a bare IRI-shaped str that
    # the gateway heuristic would coerce to a URIRef and quarantine).
    node = {
        "key": "doc1",
        "entity_class": "Document",
        "display_name": "Doc",
        "source_system": "crm",
        "source_ref": "doc1:1",
        "acl": ["alpha"],
        "content_hash": "h",
        "config_hash": "c",
        "schema_version": "v",
        "id": "d1",
        "title": "Q3 report",
        "doc_type": "report",
        "uri": "https://example.com/doc/1",
    }
    triples = set(PropertyGraphLift._node_triples(node, "Document"))
    iri = "urn:ne:entity:doc1"
    assert (iri, NEOP + "uri", DeclaredLiteral("https://example.com/doc/1", None)) in triples


def test_non_contract_property_keys_are_skipped() -> None:
    node = _account_node("k", ["alpha"])
    node["not_a_contract_field"] = "junk"  # deny-by-default: skipped, never lifted
    predicates = {p for _s, p, _o in PropertyGraphLift._node_triples(node, "Account")}
    assert NEOP + "not_a_contract_field" not in predicates
    # Governance/provenance keys are skipped too (source_system is a contract field).
    assert NEOP + "source_system" not in predicates
    assert NEOP + "acl" not in predicates


def test_unmapped_entity_class_is_skipped_and_counted() -> None:
    async def scenario() -> None:
        node = _account_node("k", ["alpha"])
        node["entity_class"] = "Widget"  # no LinkML mapping
        report, store = await _lift([node], [])
        assert report.nodes_lifted == 0
        assert report.nodes_skipped_unmapped == 1
        assert store.calls == []

    asyncio.run(scenario())


def test_missing_or_empty_acl_element_is_skipped_and_counted() -> None:
    async def scenario() -> None:
        # A node with empty acl is rejected at T2 upsert, so exercise the lift's own
        # guard by scanning a hand-built source.
        node = _account_node("k", [])

        class _Src:
            async def scan_nodes(self, entity_class: str | None = None):
                return [node]

            async def scan_edges(self):
                return []

        store = _RecordingStore()
        report = await PropertyGraphLift(_Src(), store).run()  # type: ignore[arg-type]
        assert report.elements_skipped_no_acl == 1
        assert report.nodes_lifted == 0
        assert store.calls == []

    asyncio.run(scenario())


def test_source_bearing_lift_refuses_unstamped_element_when_enforced() -> None:
    async def scenario() -> None:
        node = _account_node("k", ["alpha"])

        class _Src:
            async def scan_nodes(self, entity_class: str | None = None):
                return [node]

            async def scan_edges(self):
                return []

        store = _RecordingStore()
        report = await PropertyGraphLift(_Src(), store, require_source_authority=True).run()  # type: ignore[arg-type]
        assert report.elements_skipped_unknown_authority == 1
        assert report.nodes_lifted == 0
        assert store.calls == []

    asyncio.run(scenario())


def test_source_bearing_lift_retains_authority_before_graph_write() -> None:
    async def scenario() -> None:
        node = _account_node("k", ["alpha"])
        node["source_authority_stamps"] = [_authority_stamp().model_dump(mode="json")]

        class _Src:
            async def scan_nodes(self, entity_class: str | None = None):
                return [node]

            async def scan_edges(self):
                return []

        class _FailingSink:
            async def retain(self, subjects, stamps, graphs):
                raise RuntimeError("authority store unavailable")

        store = _RecordingStore()
        lift = PropertyGraphLift(
            _Src(),
            cast(ValidatingTripleStore, store),
            require_source_authority=True,
            authority_sink=_FailingSink(),
        )
        try:
            await lift.run()
        except RuntimeError as exc:
            assert str(exc) == "authority store unavailable"
        else:
            raise AssertionError("authority retention failure must propagate")
        assert store.calls == []

    asyncio.run(scenario())


def test_unknown_edge_kind_is_skipped_and_counted() -> None:
    async def scenario() -> None:
        acct = _account_node("a", ["alpha"])
        per = _person_node("p", ["alpha"])
        edge = {
            "key": "e",
            "edge_kind": "BOGUS",
            "from_key": "p",
            "to_key": "a",
            "source_system": "crm",
            "source_ref": "x:1",
            "acl": ["alpha"],
            "config_hash": "c",
        }
        report, _store = await _lift([acct, per], [edge])
        assert report.edges_skipped_unmapped == 1
        assert report.edges_lifted == 0

    asyncio.run(scenario())


def test_edge_acl_intersection_rule_and_empty_intersection_skip() -> None:
    async def scenario() -> None:
        # Person(alpha) --WORKS_AT--> Account(beta): empty intersection -> skipped.
        per = _person_node("p", ["alpha"])
        acct = _account_node("a", ["beta"])
        edge = {
            "key": "e",
            "edge_kind": "WORKS_AT",
            "from_key": "p",
            "to_key": "a",
            "source_system": "crm",
            "source_ref": "x:1",
            "acl": ["alpha", "beta"],
            "config_hash": "c",
        }
        report, _store = await _lift([per, acct], [edge])
        assert report.edges_lifted == 0
        assert report.edges_skipped_empty_intersection == 1

        # Now both on 'alpha': the edge lifts into exactly the intersection graph.
        acct2 = _account_node("a", ["alpha"])
        report2, store2 = await _lift([per, acct2], [edge])
        assert report2.edges_lifted == 1
        edge_calls = [c for c in store2.calls if any(t[1] == NEOP + "works_at" for t in c[0])]
        assert len(edge_calls) == 1
        triples, graph_iri = edge_calls[0]
        assert graph_iri == graph_iri_for_label("alpha")
        assert triples == (("urn:ne:entity:p", NEOP + "works_at", "urn:ne:entity:a"),)

    asyncio.run(scenario())


def test_same_as_maps_to_reified_identity_assertion() -> None:
    # _sameas_triples takes RAW endpoint keys and mints entity IRIs itself.
    triples = set(
        PropertyGraphLift._sameas_triples({"key": "sa1", "source_ref": "er:99"}, "L", "R")
    )
    subject = "urn:ne:sameas:sa1"
    assert triples == {
        (subject, _RDF_TYPE, NEA + "IdentityAssertion"),
        (subject, NEA + "same_as_left", "urn:ne:entity:L"),
        (subject, NEA + "same_as_right", "urn:ne:entity:R"),
        # source_ref is an xsd:string slot → DeclaredLiteral(..., None) (FIX-1).
        (subject, NEA + "source_ref", DeclaredLiteral("er:99", None)),
    }
    # owl:sameAs is deliberately NOT asserted.
    assert not any("sameAs" in p for _s, p, _o in triples)


def test_lift_constants_match_er_merge_constants() -> None:
    # Convention drift guard: the lift's SAME_AS/ER constants equal T2's.
    assert lift_mod.SAME_AS_EDGE_KIND == SAME_AS_EDGE_KIND
    assert lift_mod.ER_SOURCE_SYSTEM == ER_SOURCE_SYSTEM


def test_neop_iris_match_generated_shape_target_and_path_iris() -> None:
    # Convention drift guard: neop class IRIs == the generated sh:targetClass IRIs,
    # and each EDGE_DEF slot IRI == a generated sh:path IRI. Drift fails HERE.
    from importlib.resources import files

    shapes = Graph()
    shapes.parse(
        data=files("ne.schema.generated").joinpath("operational.shapes.ttl").read_bytes(),
        format="turtle",
    )
    target_classes = {str(o) for o in shapes.objects(None, SH.targetClass) if isinstance(o, URIRef)}
    paths = {str(o) for o in shapes.objects(None, SH.path) if isinstance(o, URIRef)}
    for entity_class in ENTITY_CLASSES:
        assert NEOP + entity_class in target_classes, entity_class
    for edge_def in EDGE_DEFS:
        assert NEOP + edge_def.slot_name in paths, edge_def.slot_name
