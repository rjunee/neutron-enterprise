# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-085b scope item 3: the RDF lift goes THROUGH the fail-closed SHACL gateway."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import pytest

from ne.brain_graph.er.merge import ER_SOURCE_SYSTEM, SAME_AS_EDGE_KIND
from ne.brain_graph.testing.memory import InMemoryGraphStore
from ne.brain_ontology import PropertyGraphLift, graph_iri_for_label, graphs_for_labels
from ne.brain_ontology.config import OntologySettings
from ne.brain_ontology.gateway import TripleQuarantinedError, ValidatingTripleStore
from ne.brain_ontology.lift.graph_to_rdf import NEA, NEOP
from ne.brain_ontology.quarantine import QuarantinedTriples, SqliteTripleQuarantine
from ne.brain_ontology.shapes import build_validator, load_compiled_shapes
from ne.brain_ontology.store.oxigraph import OxigraphBackend
from ne.schema import ShapeViolation

_RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
_CONFIG_HASH = "a" * 64


class _RecordingSink:
    def __init__(self) -> None:
        self.events: list[ShapeViolation] = []

    def emit(self, event: ShapeViolation) -> None:
        self.events.append(event)


def _gateway(
    *, sink: _RecordingSink | None = None, quarantine: SqliteTripleQuarantine | None = None
) -> ValidatingTripleStore:
    settings = OntologySettings(include_ontology_graph=False)
    return ValidatingTripleStore(
        OxigraphBackend(),
        build_validator(settings),
        quarantine or SqliteTripleQuarantine(),
        sink or _RecordingSink(),
        settings=settings,
        config_hash=_CONFIG_HASH,
        compiled_shapes=load_compiled_shapes(),
    )


def _account(key: str, acl: list[str], *, name: str | None = "Acme Corp") -> dict[str, Any]:
    node = {
        "key": key,
        "entity_class": "Account",
        "display_name": "Acme",
        "source_system": "crm",
        "source_ref": f"{key}:1",
        "acl": acl,
        "content_hash": "h",
        "config_hash": "c",
        "schema_version": "v",
        "id": f"{key}-id",
    }
    if name is not None:
        node["name"] = name
    return node


def _person(key: str, acl: list[str]) -> dict[str, Any]:
    return {
        "key": key,
        "entity_class": "Person",
        "display_name": "Ada",
        "source_system": "crm",
        "source_ref": f"{key}:1",
        "acl": acl,
        "content_hash": "h",
        "config_hash": "c",
        "schema_version": "v",
        "id": f"{key}-id",
        "given_name": "Ada",
        "family_name": "Lovelace",
        "email": "ada@example.org",
    }


def _document(
    key: str, acl: list[str], *, uri: str | None = "https://example.com/doc/1"
) -> dict[str, Any]:
    # Document.uri is a LinkML external-URI slot but is SHAPED as an xsd:string
    # LITERAL (nodeKind sh:Literal). An IRI-shaped value therefore MUST land as an
    # RDF literal, not a URIRef — the FIX-1 regression.
    node: dict[str, Any] = {
        "key": key,
        "entity_class": "Document",
        "display_name": "Doc",
        "source_system": "crm",
        "source_ref": f"{key}:1",
        "acl": acl,
        "content_hash": "h",
        "config_hash": "c",
        "schema_version": "v",
        "id": f"{key}-id",
        "title": "Q3 report",
        "doc_type": "report",
    }
    if uri is not None:
        node["uri"] = uri
    return node


async def _seed(nodes: list[Mapping[str, Any]], edges: list[Mapping[str, Any]]):
    mem = InMemoryGraphStore()
    if nodes:
        await mem.upsert_nodes(nodes)
    if edges:
        await mem.upsert_edges(edges)
    return mem


async def _dump(gw: ValidatingTripleStore) -> list[tuple[str, str, str, str]]:
    rows = await gw.sparql("SELECT ?g ?s ?p ?o WHERE { GRAPH ?g { ?s ?p ?o } }")
    return sorted((r.get("g", ""), r.get("s", ""), r.get("p", ""), r.get("o", "")) for r in rows)


def _counts(report: Any) -> tuple[int, ...]:
    return (
        report.nodes_lifted,
        report.edges_lifted,
        report.sameas_lifted,
        report.nodes_skipped_unmapped,
        report.edges_skipped_unmapped,
        report.elements_skipped_no_acl,
        report.edges_skipped_empty_intersection,
        len(report.quarantined),
    )


def test_valid_entity_lifts_and_is_queryable() -> None:
    async def scenario() -> None:
        mem = await _seed([_account("acme", ["alpha"])], [])
        gw = _gateway()
        report = await PropertyGraphLift(mem, gw).run()
        assert report.nodes_lifted == 1
        assert report.quarantined == ()
        rows = await gw.sparql(f"SELECT ?s WHERE {{ ?s <{_RDF_TYPE}> <{NEOP}Account> }}")
        assert rows == [{"s": "urn:ne:entity:acme"}]
        scoped = await gw.sparql_scoped(
            f"SELECT ?n WHERE {{ <urn:ne:entity:acme> <{NEOP}name> ?n }}",
            dataset=graphs_for_labels(["alpha"]),
        )
        assert scoped == [{"n": "Acme Corp"}]

    asyncio.run(scenario())


def test_document_with_iri_shaped_uri_lifts_and_uri_object_is_literal() -> None:
    # FIX-1 (§5-row): a fully valid Document whose uri is an IRI-shaped value living
    # in an xsd:string slot must LIFT (not be quarantined) and the uri triple's
    # object must be an RDF Literal. Pre-fix the gateway's _rdf_object heuristic
    # coerced the string into a URIRef, tripping sh:datatype xsd:string and
    # quarantining the whole node.
    async def scenario() -> None:
        mem = await _seed([_document("doc1", ["alpha"])], [])
        gw = _gateway()
        report = await PropertyGraphLift(mem, gw).run()
        assert report.nodes_lifted == 1
        assert report.quarantined == ()
        rows = await gw.sparql(f"SELECT ?o WHERE {{ <urn:ne:entity:doc1> <{NEOP}uri> ?o }}")
        assert rows == [{"o": "https://example.com/doc/1"}]
        is_literal = await gw.sparql(
            f"ASK {{ <urn:ne:entity:doc1> <{NEOP}uri> ?o . FILTER(isLiteral(?o)) }}"
        )
        assert is_literal == [{"ask": True}]

    asyncio.run(scenario())


def test_invalid_entity_quarantines_whole_valid_sibling_lands() -> None:
    async def scenario() -> None:
        mem = await _seed([_account("good", ["alpha"]), _account("bad", ["alpha"], name=None)], [])
        sink = _RecordingSink()
        quarantine = SqliteTripleQuarantine()
        gw = _gateway(sink=sink, quarantine=quarantine)
        report = await PropertyGraphLift(mem, gw).run()
        # The valid sibling landed; the invalid one is quarantined whole.
        assert report.nodes_lifted == 1
        assert len(report.quarantined) == 1
        assert report.quarantined[0] in {q.quarantine_id for q in quarantine.list()}
        # A ShapeViolation event was emitted (quarantined=True).
        assert len(sink.events) == 1
        assert sink.events[0].quarantined is True
        # ZERO triples of the invalid entity in the store; the valid one present.
        bad = await gw.sparql("SELECT ?p WHERE { <urn:ne:entity:bad> ?p ?o }")
        assert bad == []
        good = await gw.sparql("SELECT ?p WHERE { <urn:ne:entity:good> ?p ?o }")
        assert good != []

    asyncio.run(scenario())


def test_typed_relationship_validates_across_acl_graphs() -> None:
    async def scenario() -> None:
        # Person and Account each on {alpha}; the WORKS_AT edge's sh:class Account
        # constraint is satisfied via the full-union CBD even though the endpoints
        # live in their own per-ACL-label graphs.
        edge = {
            "key": "w1",
            "edge_kind": "WORKS_AT",
            "from_key": "per",
            "to_key": "acc",
            "source_system": "crm",
            "source_ref": "per:1",
            "acl": ["alpha"],
            "config_hash": "c",
        }
        mem = await _seed([_person("per", ["alpha"]), _account("acc", ["alpha"])], [edge])
        gw = _gateway()
        report = await PropertyGraphLift(mem, gw).run()
        assert report.edges_lifted == 1
        assert report.quarantined == ()
        rows = await gw.sparql(f"SELECT ?a WHERE {{ <urn:ne:entity:per> <{NEOP}works_at> ?a }}")
        assert rows == [{"a": "urn:ne:entity:acc"}]

    asyncio.run(scenario())


def test_same_as_lifts_as_validated_identity_assertion() -> None:
    async def scenario() -> None:
        # apply_merge writes SAME_AS with the endpoint-intersection acl.
        sameas = {
            "key": "sa1",
            "edge_kind": SAME_AS_EDGE_KIND,
            "from_key": "l",
            "to_key": "r",
            "source_system": ER_SOURCE_SYSTEM,
            "source_ref": "er:1",
            "acl": ["alpha"],
            "config_hash": "c",
        }
        mem = await _seed([_account("l", ["alpha"]), _account("r", ["alpha"])], [sameas])
        gw = _gateway()
        report = await PropertyGraphLift(mem, gw).run()
        assert report.sameas_lifted == 1
        assert report.quarantined == ()
        ia = await gw.sparql(f"SELECT ?s WHERE {{ ?s <{_RDF_TYPE}> <{NEA}IdentityAssertion> }}")
        assert ia == [{"s": "urn:ne:sameas:sa1"}]
        # owl:sameAs was NOT asserted.
        assert await gw.sparql(
            "ASK { <urn:ne:entity:l> <http://www.w3.org/2002/07/owl#sameAs> ?o }"
        ) == [{"ask": False}]

    asyncio.run(scenario())


def test_malformed_identity_assertion_quarantines_curated_shape_non_vacuity() -> None:
    async def scenario() -> None:
        gw = _gateway()
        subject = "urn:ne:sameas:bad"
        # Missing same_as_right -> the curated per-write shape's minCount 1 fails.
        malformed = [
            (subject, _RDF_TYPE, NEA + "IdentityAssertion"),
            (subject, NEA + "same_as_left", "urn:ne:entity:x"),
            (subject, NEA + "source_ref", "er:1"),
        ]
        with pytest.raises(TripleQuarantinedError):
            await gw.insert_triples_into(malformed, graph_iri=graph_iri_for_label("alpha"))

    asyncio.run(scenario())


def test_lift_is_deterministic_and_idempotent() -> None:
    async def scenario() -> None:
        edge = {
            "key": "w1",
            "edge_kind": "WORKS_AT",
            "from_key": "per",
            "to_key": "acc",
            "source_system": "crm",
            "source_ref": "per:1",
            "acl": ["alpha"],
            "config_hash": "c",
        }
        mem = await _seed([_person("per", ["alpha"]), _account("acc", ["alpha"])], [edge])
        gw = _gateway()
        report1 = await PropertyGraphLift(mem, gw).run()
        dump1 = await _dump(gw)
        report2 = await PropertyGraphLift(mem, gw).run()
        dump2 = await _dump(gw)
        # Identical counts and byte-identical canonical store state (RDF set semantics).
        assert _counts(report1) == _counts(report2)
        assert dump1 == dump2

    asyncio.run(scenario())


def test_insert_triples_into_refuses_ontology_namespace() -> None:
    async def scenario() -> None:
        gw = _gateway()
        with pytest.raises(ValueError, match="urn:ne:ontology"):
            await gw.insert_triples_into(
                [("urn:ne:entity:x", _RDF_TYPE, NEOP + "Account")],
                graph_iri="urn:ne:ontology:ne:operational-alignment",
            )

    asyncio.run(scenario())


def test_release_of_quarantined_lift_reinserts_into_original_acl_graph() -> None:
    async def scenario() -> None:
        quarantine = SqliteTripleQuarantine()
        gw = _gateway(quarantine=quarantine)
        mem = await _seed([_account("bad", ["alpha"], name=None)], [])
        report = await PropertyGraphLift(mem, gw).run()
        assert len(report.quarantined) == 1
        entry = quarantine.list()[0]
        acl_graph = graph_iri_for_label("alpha")
        # The quarantine row carries the ORIGINAL per-ACL-label target graph.
        assert entry.graph_iri == acl_graph
        # Replace the delta with a conforming Account (same graph_iri), then release:
        # it must land in the ORIGINAL acl graph, not settings.data_graph_iri.
        subject = "urn:ne:entity:bad"
        conforming = (
            f"<{subject}> <{_RDF_TYPE}> <{NEOP}Account> .\n"
            f'<{subject}> <{NEOP}id> "bad-id" .\n'
            f'<{subject}> <{NEOP}name> "Fixed Corp" .'
        )
        quarantine.remove(entry.quarantine_id)
        quarantine.record(
            QuarantinedTriples(
                quarantine_id=entry.quarantine_id,
                delta_nt=conforming,
                violations=entry.violations,
                provenance=entry.provenance,
                shapes_hash=entry.shapes_hash,
                created_at=entry.created_at,
                graph_iri=acl_graph,
            )
        )
        await gw.release(entry.quarantine_id)
        assert quarantine.get(entry.quarantine_id) is None
        landed = await gw.sparql_scoped(
            f"SELECT ?n WHERE {{ <{subject}> <{NEOP}name> ?n }}",
            dataset=graphs_for_labels(["alpha"]),
        )
        assert landed == [{"n": "Fixed Corp"}]

    asyncio.run(scenario())
