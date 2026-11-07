# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-085e: PropertyGraphLift.run() lands SHACL-required-edge entities on the
GENERAL append-only initial-load path (SPEC §11 NE-LIFT-FIX deeper gap).

Reproduces the bug empirically: an Order/Ticket whose LinkML/SHACL contract has
a REQUIRED edge (minCount>=1: Order.account, Order.placed_by, Ticket.belongs_to)
used to be quarantined WHOLE by ``run()`` because the node's literal triples were
validated BEFORE its outgoing edges landed (they push as separate per-element
SHACL deltas). The fix threads a desired-union two-hop CBD ``context_triples``
into each element's validation payload (mirroring ``reconcile``'s
``replace_acl_graph`` context handling) so a node validates WITH its required
edges even though the edges land in a DIFFERENT ACL-graph partition.

Fail-closed is unchanged: a genuinely-missing required edge is absent from the
desired union, so ``sh:minCount`` still fires and the entity still quarantines
(per-element granularity retained); the new gateway parameter is a
validation-payload addition only — it is NEVER written to the backend.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any

from ne.brain_graph.testing.memory import InMemoryGraphStore
from ne.brain_ontology import PropertyGraphLift
from ne.brain_ontology.config import OntologySettings
from ne.brain_ontology.gateway import ValidatingTripleStore
from ne.brain_ontology.lift.graph_to_rdf import NEOP
from ne.brain_ontology.quarantine import SqliteTripleQuarantine
from ne.brain_ontology.shapes import build_validator, load_compiled_shapes
from ne.brain_ontology.store.oxigraph import OxigraphBackend
from ne.schema import ShapeViolation

_CONFIG_HASH = "a" * 64


class _RecordingSink:
    def __init__(self) -> None:
        self.events: list[ShapeViolation] = []

    def emit(self, event: ShapeViolation) -> None:
        self.events.append(event)


def _gateway_over(
    backend: OxigraphBackend,
    *,
    sink: _RecordingSink | None = None,
    quarantine: SqliteTripleQuarantine | None = None,
) -> ValidatingTripleStore:
    settings = OntologySettings(include_ontology_graph=False)
    return ValidatingTripleStore(
        backend,
        build_validator(settings),
        quarantine or SqliteTripleQuarantine(),
        sink or _RecordingSink(),
        settings=settings,
        config_hash=_CONFIG_HASH,
        compiled_shapes=load_compiled_shapes(),
    )


def _gateway(
    *, sink: _RecordingSink | None = None, quarantine: SqliteTripleQuarantine | None = None
) -> ValidatingTripleStore:
    return _gateway_over(OxigraphBackend(), sink=sink, quarantine=quarantine)


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


def _order(key: str, acl: list[str]) -> dict[str, Any]:
    return {
        "key": key,
        "entity_class": "Order",
        "display_name": "Order 1",
        "source_system": "crm",
        "source_ref": f"{key}:1",
        "acl": acl,
        "content_hash": "h",
        "config_hash": "c",
        "schema_version": "v",
        "id": f"{key}-id",
        "order_number": "ORD-0001",
        "status": "placed",
        "currency": "USD",
        # Canonical xsd:decimal lexical form (no trailing zero): a NON-canonical
        # form like "100.50" is re-serialized by the oxigraph backend as "100.5"
        # on read-back (RDF 1.1 does not mandate lexical preservation), which is a
        # pre-existing store/gateway interaction ORTHOGONAL to NE-085e (reproducible
        # on unmodified code with a same-batch re-insert, no context_triples
        # involved) — out of scope here; this fixture avoids exercising it so the
        # test isolates the required-edge landing behavior this unit fixes.
        "total_amount": "100.5",
        "placed_at": "2026-01-02T00:00:00+00:00",
    }


def _ticket(key: str, acl: list[str]) -> dict[str, Any]:
    return {
        "key": key,
        "entity_class": "Ticket",
        "display_name": "Ticket 1",
        "source_system": "crm",
        "source_ref": f"{key}:1",
        "acl": acl,
        "content_hash": "h",
        "config_hash": "c",
        "schema_version": "v",
        "id": f"{key}-id",
        "subject": "Printer on fire",
        "status": "open",
        "opened_at": "2026-01-02T00:00:00+00:00",
    }


def _edge(kind: str, key: str, from_key: str, to_key: str, acl: list[str]) -> dict[str, Any]:
    return {
        "key": key,
        "edge_kind": kind,
        "from_key": from_key,
        "to_key": to_key,
        "source_system": "crm",
        "source_ref": f"{key}:1",
        "acl": acl,
        "config_hash": "c",
    }


async def _seed(
    nodes: Sequence[Mapping[str, Any]], edges: Sequence[Mapping[str, Any]]
) -> InMemoryGraphStore:
    mem = InMemoryGraphStore()
    if nodes:
        await mem.upsert_nodes(nodes)
    if edges:
        await mem.upsert_edges(edges)
    return mem


async def _dump(gw: ValidatingTripleStore) -> list[tuple[str, str, str, str]]:
    rows = await gw.sparql("SELECT ?g ?s ?p ?o WHERE { GRAPH ?g { ?s ?p ?o } }")
    return sorted((r.get("g", ""), r.get("s", ""), r.get("p", ""), r.get("o", "")) for r in rows)


def test_required_edge_order_lands_on_initial_run() -> None:
    async def scenario() -> None:
        mem = await _seed(
            [_account("acct1", ["alpha"]), _person("p1", ["alpha"]), _order("ord1", ["alpha"])],
            [
                _edge("ACCOUNT", "e1", "ord1", "acct1", ["alpha"]),
                _edge("PLACED_BY", "e2", "ord1", "p1", ["alpha"]),
            ],
        )
        gw = _gateway()
        report = await PropertyGraphLift(mem, gw).run()
        assert report.quarantined == ()
        assert report.nodes_lifted == 3
        assert report.edges_lifted == 2
        rows = await gw.sparql(
            "SELECT ?o ?account ?status WHERE { "
            f"?o a <{NEOP}Order> ; <{NEOP}account> ?account ; <{NEOP}status> ?status . "
            "}"
        )
        assert rows == [
            {
                "o": "urn:ne:entity:ord1",
                "account": "urn:ne:entity:acct1",
                "status": "placed",
            }
        ]

    asyncio.run(scenario())


def test_required_edge_ticket_lands_on_initial_run() -> None:
    async def scenario() -> None:
        mem = await _seed(
            [_account("acct1", ["alpha"]), _ticket("t1", ["alpha"])],
            [_edge("BELONGS_TO", "e1", "t1", "acct1", ["alpha"])],
        )
        gw = _gateway()
        report = await PropertyGraphLift(mem, gw).run()
        assert report.quarantined == ()
        assert report.nodes_lifted == 2
        assert report.edges_lifted == 1
        rows = await gw.sparql(
            "SELECT ?t WHERE { "
            f"?t a <{NEOP}Ticket> ; <{NEOP}belongs_to> <urn:ne:entity:acct1> . "
            "}"
        )
        assert rows == [{"t": "urn:ne:entity:t1"}]

    asyncio.run(scenario())


def test_missing_required_edge_still_quarantines() -> None:
    async def scenario() -> None:
        mem = await _seed(
            [_account("acct1", ["alpha"]), _order("ord1", ["alpha"])],
            [],
        )
        gw = _gateway()
        report = await PropertyGraphLift(mem, gw).run()
        assert len(report.quarantined) == 1
        # The Account sibling still lands — per-element quarantine granularity.
        assert report.nodes_lifted == 1
        rows = await gw.sparql(f"SELECT ?o WHERE {{ <urn:ne:entity:ord1> a <{NEOP}Order> }}")
        assert rows == []

    asyncio.run(scenario())


def test_context_triples_validate_only_never_written() -> None:
    async def scenario() -> None:
        gw = _gateway()
        from ne.brain_ontology.acl import graph_iri_for_label

        account_a = [
            (
                "urn:ne:entity:acct-a",
                "http://www.w3.org/1999/02/22-rdf-syntax-ns#type",
                NEOP + "Account",
            ),
            ("urn:ne:entity:acct-a", NEOP + "name", "A Corp"),
        ]
        account_b = [
            (
                "urn:ne:entity:acct-b",
                "http://www.w3.org/1999/02/22-rdf-syntax-ns#type",
                NEOP + "Account",
            ),
            ("urn:ne:entity:acct-b", NEOP + "name", "B Corp"),
        ]
        graph_iri = graph_iri_for_label("alpha")
        await gw.insert_triples_into(account_a, graph_iri=graph_iri, context_triples=account_b)
        dump = await _dump(gw)
        subjects = {row[1] for row in dump}
        assert "urn:ne:entity:acct-a" in subjects
        assert "urn:ne:entity:acct-b" not in subjects

    asyncio.run(scenario())
