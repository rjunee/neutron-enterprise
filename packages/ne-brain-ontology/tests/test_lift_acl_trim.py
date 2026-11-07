# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-085b C7 acceptance: lifted-triple reads trim identically to the T2 source."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from ne.brain_graph.read import EntityGraph
from ne.brain_graph.testing.memory import InMemoryGraphStore
from ne.brain_ontology import PropertyGraphLift, graphs_for_labels
from ne.brain_ontology.config import OntologySettings
from ne.brain_ontology.gateway import ValidatingTripleStore
from ne.brain_ontology.lift.graph_to_rdf import NEOP, entity_iri
from ne.brain_ontology.quarantine import SqliteTripleQuarantine
from ne.brain_ontology.shapes import build_validator, load_compiled_shapes
from ne.brain_ontology.store.oxigraph import OxigraphBackend
from ne.contracts.types import Principal

_CONFIG_HASH = "a" * 64
_RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"

# All node keys in the fixture (for the hidden-entity probe).
_ALL_KEYS = ("per_alpha", "acc_alpha", "acc_beta", "acc_both")


def _gateway() -> ValidatingTripleStore:
    settings = OntologySettings(include_ontology_graph=False)
    return ValidatingTripleStore(
        OxigraphBackend(),
        build_validator(settings),
        SqliteTripleQuarantine(),
        _NullSink(),
        settings=settings,
        config_hash=_CONFIG_HASH,
        compiled_shapes=load_compiled_shapes(),
    )


class _NullSink:
    def emit(self, event: Any) -> None:
        return None


def _account(key: str, acl: list[str]) -> dict[str, Any]:
    return {
        "key": key,
        "entity_class": "Account",
        "display_name": "Acct",
        "source_system": "crm",
        "source_ref": f"{key}:1",
        "acl": acl,
        "content_hash": "h",
        "config_hash": "c",
        "schema_version": "v",
        "id": f"{key}-id",
        "name": f"{key} Corp",
    }


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


async def _visible_via_scoped(gw: ValidatingTripleStore, key: str, labels: list[str]) -> bool:
    rows = await gw.sparql_scoped(
        f"ASK {{ <{entity_iri(key)}> ?p ?o }}", dataset=graphs_for_labels(labels)
    )
    return bool(rows[0]["ask"])


def test_lifted_reads_match_t2_visibility_including_hidden_iri_probe(no_network: None) -> None:
    async def scenario() -> None:
        mem = InMemoryGraphStore()
        await mem.upsert_nodes(
            [
                _person("per_alpha", ["alpha"]),
                _account("acc_alpha", ["alpha"]),
                _account("acc_beta", ["beta"]),
                _account("acc_both", ["alpha", "beta"]),
            ]
        )
        # A WORKS_AT edge whose endpoints are BOTH alpha-only (hidden from B).
        await mem.upsert_edges(
            [
                {
                    "key": "w1",
                    "edge_kind": "WORKS_AT",
                    "from_key": "per_alpha",
                    "to_key": "acc_alpha",
                    "source_system": "crm",
                    "source_ref": "per_alpha:1",
                    "acl": ["alpha"],
                    "config_hash": "c",
                }
            ]
        )
        gw = _gateway()
        await PropertyGraphLift(mem, gw).run()

        graph = EntityGraph(mem, group_expander=None, audit_sink=None)
        principals = {
            "A": Principal(id="user:a", groups=("alpha",)),
            "B": Principal(id="user:b", groups=("beta",)),
        }
        for principal in principals.values():
            labels = [principal.id, *principal.groups]
            t2_nodes = await graph.lookup(principal=principal)
            visible_keys = {n.key for n in t2_nodes}
            assert visible_keys  # each principal sees something
            # Every entity T2 returns is answerable via the scoped RDF read...
            for key in visible_keys:
                assert await _visible_via_scoped(gw, key, labels), (principal.id, key)
            # ...and every entity T2 HIDES yields zero rows — including the
            # known-IRI probe of the hidden entity's exact urn:ne:entity:<key> IRI.
            for key in set(_ALL_KEYS) - visible_keys:
                assert not await _visible_via_scoped(gw, key, labels), (principal.id, key)

    asyncio.run(scenario())


def test_endpoint_hidden_edge_never_exceeds_t2_conjunctive_rule(no_network: None) -> None:
    async def scenario() -> None:
        mem = InMemoryGraphStore()
        await mem.upsert_nodes([_person("per_alpha", ["alpha"]), _account("acc_alpha", ["alpha"])])
        await mem.upsert_edges(
            [
                {
                    "key": "w1",
                    "edge_kind": "WORKS_AT",
                    "from_key": "per_alpha",
                    "to_key": "acc_alpha",
                    "source_system": "crm",
                    "source_ref": "per_alpha:1",
                    "acl": ["alpha"],
                    "config_hash": "c",
                }
            ]
        )
        gw = _gateway()
        await PropertyGraphLift(mem, gw).run()

        works_at_ask = f"ASK {{ <{entity_iri('per_alpha')}> <{NEOP}works_at> ?o }}"
        # Principal A (alpha) sees the edge; principal B (beta) must NOT — its
        # endpoints are alpha-only, so the conjunctive rule hides it (never over-return).
        a_ds = graphs_for_labels(["user:a", "alpha"])
        b_ds = graphs_for_labels(["user:b", "beta"])
        a_rows = await gw.sparql_scoped(works_at_ask, dataset=a_ds)
        b_rows = await gw.sparql_scoped(works_at_ask, dataset=b_ds)
        assert a_rows == [{"ask": True}]
        assert b_rows == [{"ask": False}]

    asyncio.run(scenario())


def test_graphs_for_labels_empty_raises() -> None:
    with pytest.raises(ValueError, match="empty label set"):
        graphs_for_labels(())
