# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-085d: the mutation-aware reconciling lift (validate-then-atomic-swap).

Proves both empirically-reproduced 085b failure modes are closed by
:meth:`PropertyGraphLift.reconcile` WITHOUT weakening the fail-closed gateway:
(1) a changed single-valued slot reconciles to the new value only (no sh:maxCount
whole-node quarantine — the append run() path keeps that red, demonstrably);
(2) an ACL change vacates the old per-ACL graph (C7 over-return closed); plus
element purge, non-vacuous atomic-swap rollback, and a byte-identical no-op.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from ne.brain_graph.testing.memory import InMemoryGraphStore
from ne.brain_ontology import PropertyGraphLift, graph_iri_for_label, graphs_for_labels
from ne.brain_ontology.config import OntologySettings
from ne.brain_ontology.gateway import ValidatingTripleStore
from ne.brain_ontology.lift.graph_to_rdf import NEOP
from ne.brain_ontology.quarantine import SqliteTripleQuarantine
from ne.brain_ontology.shapes import build_validator, load_compiled_shapes
from ne.brain_ontology.store.oxigraph import OxigraphBackend
from ne.schema import ShapeViolation

_RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
_CONFIG_HASH = "a" * 64
_NAME = f"SELECT ?n WHERE {{ <urn:ne:entity:acme> <{NEOP}name> ?n }}"


class _RecordingSink:
    def __init__(self) -> None:
        self.events: list[ShapeViolation] = []

    def emit(self, event: ShapeViolation) -> None:
        self.events.append(event)


class _SpyBackend(OxigraphBackend):
    """Records whole-graph swap/purge calls — the no-op reconcile proof."""

    def __init__(self) -> None:
        super().__init__()
        self.replace_calls = 0
        self.delete_calls = 0

    async def replace_graph_turtle(self, data: bytes, *, graph_iri: str) -> None:
        self.replace_calls += 1
        await super().replace_graph_turtle(data, graph_iri=graph_iri)

    async def delete_graph(self, graph_iri: str) -> None:
        self.delete_calls += 1
        await super().delete_graph(graph_iri)


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


def _works_at(key: str, from_key: str, to_key: str, acl: list[str]) -> dict[str, Any]:
    return {
        "key": key,
        "edge_kind": "WORKS_AT",
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


async def _name_rows(
    gw: ValidatingTripleStore, labels: Sequence[str] | None = None
) -> list[Mapping[str, str]]:
    if labels is None:
        return list(await gw.sparql(_NAME))
    return list(await gw.sparql_scoped(_NAME, dataset=graphs_for_labels(list(labels))))


# -- PROOF 1: changed single-valued slot reconciles to the new value only -------


def test_changed_single_valued_slot_reconciles_to_new_value_only() -> None:
    async def scenario() -> None:
        mem = await _seed([_account("acme", ["alpha"], name="Acme Corp")], [])
        gw = _gateway()
        await PropertyGraphLift(mem, gw).run()
        # Wholesale prop replace: the SAME key now carries a different single-valued name.
        await mem.upsert_nodes([_account("acme", ["alpha"], name="Apex Corp")])
        report = await PropertyGraphLift(mem, gw).reconcile()
        assert report.quarantined == ()
        # Exactly ONE name row, and it is the NEW value — the node was NOT quarantined.
        assert await _name_rows(gw) == [{"n": "Apex Corp"}]
        assert await _name_rows(gw, ["alpha"]) == [{"n": "Apex Corp"}]

    asyncio.run(scenario())


def test_append_run_still_quarantines_changed_slot() -> None:
    # The RED baseline stays demonstrable: the append run() path leaves the stale
    # value, so sh:maxCount quarantines the whole node — documenting WHY reconcile()
    # exists and guarding against silently changing run()'s append semantics.
    async def scenario() -> None:
        mem = await _seed([_account("acme", ["alpha"], name="Acme Corp")], [])
        gw = _gateway()
        await PropertyGraphLift(mem, gw).run()
        await mem.upsert_nodes([_account("acme", ["alpha"], name="Apex Corp")])
        report = await PropertyGraphLift(mem, gw).run()
        assert len(report.quarantined) == 1
        assert report.nodes_lifted == 0

    asyncio.run(scenario())


# -- PROOF 2: ACL change closes the C7 over-return ------------------------------


def test_acl_change_closes_over_return() -> None:
    async def scenario() -> None:
        mem = await _seed([_account("acme", ["alpha"])], [])
        gw = _gateway()
        await PropertyGraphLift(mem, gw).run()
        # Revoke alpha, grant beta.
        await mem.update_acl(["acme"], ["beta"])
        report = await PropertyGraphLift(mem, gw).reconcile()
        assert report.quarantined == ()
        # The vacated alpha graph holds NO stale triples — a principal scoped to
        # alpha can no longer read the entity (over-return closed)...
        assert await _name_rows(gw, ["alpha"]) == []
        # ...while a beta principal now sees it.
        assert await _name_rows(gw, ["beta"]) == [{"n": "Acme Corp"}]
        assert report.graphs_deleted >= 1

    asyncio.run(scenario())


# -- PROOF 3: deleted element is purged -----------------------------------------


def test_deleted_element_is_purged_on_reconcile() -> None:
    async def scenario() -> None:
        gw = _gateway()
        mem = await _seed(
            [_account("a", ["alpha"], name="A Corp"), _account("b", ["alpha"], name="B Corp")], []
        )
        await PropertyGraphLift(mem, gw).run()
        # Re-lift from a fresh source lacking B (reconcile is full-scan/source-agnostic).
        fresh = await _seed([_account("a", ["alpha"], name="A Corp")], [])
        await PropertyGraphLift(fresh, gw).reconcile()
        # B has ZERO triples in ANY graph; A is intact.
        b_rows = await gw.sparql("SELECT ?g ?p WHERE { GRAPH ?g { <urn:ne:entity:b> ?p ?o } }")
        assert b_rows == []
        a_rows = await gw.sparql(f"SELECT ?n WHERE {{ <urn:ne:entity:a> <{NEOP}name> ?n }}")
        assert a_rows == [{"n": "A Corp"}]

    asyncio.run(scenario())


def test_vacated_acl_graph_is_deleted() -> None:
    async def scenario() -> None:
        gw = _gateway()
        mem = await _seed([_account("g", ["gamma"])], [])
        await PropertyGraphLift(mem, gw).run()
        assert graph_iri_for_label("gamma") in await gw.acl_graphs()
        # The label's last element left T2 entirely.
        report = await PropertyGraphLift(await _seed([], []), gw).reconcile()
        assert graph_iri_for_label("gamma") not in await gw.acl_graphs()
        assert report.graphs_deleted == 1

    asyncio.run(scenario())


# -- PROOF 4: fail-closed preserved (non-vacuous atomic-swap rollback) ----------


def test_nonconforming_reconcile_quarantines_and_leaves_prior_graph_intact() -> None:
    async def scenario() -> None:
        sink = _RecordingSink()
        quarantine = SqliteTripleQuarantine()
        gw = _gateway(sink=sink, quarantine=quarantine)
        mem = await _seed([_account("acme", ["alpha"], name="Acme Corp")], [])
        await PropertyGraphLift(mem, gw).run()
        # Mutate to an INVALID node (name=None → required-slot violation).
        await mem.upsert_nodes([_account("acme", ["alpha"], name=None)])
        report = await PropertyGraphLift(mem, gw).reconcile()
        assert len(report.quarantined) == 1
        assert report.graphs_rebuilt == 0
        # The quarantine row carries the per-ACL graph_iri; a quarantined event fired.
        entry = quarantine.list()[0]
        assert entry.graph_iri == graph_iri_for_label("alpha")
        assert sum(1 for e in sink.events if e.quarantined) == 1
        # The PRIOR graph content is still readable — not deleted, not emptied.
        assert await _name_rows(gw) == [{"n": "Acme Corp"}]

    asyncio.run(scenario())


# -- PROOF 5: unchanged re-lift is a byte-identical no-op -----------------------


def test_unchanged_reconcile_is_noop_and_byte_identical() -> None:
    async def scenario() -> None:
        spy = _SpyBackend()
        gw = _gateway_over(spy)
        # Multi-graph dataset: node triples + WORKS_AT edge across two per-ACL graphs.
        nodes = [_person("per", ["alpha", "beta"]), _account("acc", ["alpha", "beta"])]
        edge = _works_at("w1", "per", "acc", ["alpha", "beta"])
        mem = await _seed(nodes, [edge])
        await PropertyGraphLift(mem, gw).run()
        dump1 = await _dump(gw)
        report = await PropertyGraphLift(mem, gw).reconcile()
        dump2 = await _dump(gw)
        assert dump1 == dump2
        assert report.graphs_rebuilt == 0
        assert report.graphs_deleted == 0
        assert report.graphs_unchanged == len(await gw.acl_graphs())
        assert report.graphs_unchanged == 2
        # ZERO backend writes on an unchanged re-lift.
        assert spy.replace_calls == 0
        assert spy.delete_calls == 0

    asyncio.run(scenario())


def test_reconcile_on_empty_store_matches_run() -> None:
    async def scenario() -> None:
        nodes = [_person("per", ["alpha"]), _account("acc", ["alpha"])]
        edge = _works_at("w1", "per", "acc", ["alpha"])

        gw_run = _gateway()
        await PropertyGraphLift(await _seed(nodes, [edge]), gw_run).run()
        dump_run = await _dump(gw_run)

        gw_rec = _gateway()
        await PropertyGraphLift(await _seed(nodes, [edge]), gw_rec).reconcile()
        dump_rec = await _dump(gw_rec)

        assert dump_run == dump_rec

    asyncio.run(scenario())


def test_multi_label_node_reconciles_every_graph() -> None:
    async def scenario() -> None:
        mem = await _seed([_account("acme", ["alpha", "beta"], name="Acme Corp")], [])
        gw = _gateway()
        await PropertyGraphLift(mem, gw).run()
        await mem.upsert_nodes([_account("acme", ["alpha", "beta"], name="Apex Corp")])
        report = await PropertyGraphLift(mem, gw).reconcile()
        assert report.quarantined == ()
        # BOTH per-label graphs hold ONLY the new value.
        for label in ("alpha", "beta"):
            assert await _name_rows(gw, [label]) == [{"n": "Apex Corp"}]

    asyncio.run(scenario())


def test_edge_survives_reconcile_via_cross_graph_context() -> None:
    async def scenario() -> None:
        nodes = [_person("per", ["alpha"]), _account("acc", ["alpha"])]
        edge = _works_at("w1", "per", "acc", ["alpha"])
        mem = await _seed(nodes, [edge])
        gw = _gateway()
        # Initial load via reconcile: the WORKS_AT sh:class Account is satisfied by the
        # desired-union CBD context (proves the context feeds sh:class).
        r1 = await PropertyGraphLift(mem, gw).reconcile()
        assert r1.quarantined == ()
        assert r1.edges_lifted == 1
        rows = await gw.sparql(f"SELECT ?a WHERE {{ <urn:ne:entity:per> <{NEOP}works_at> ?a }}")
        assert rows == [{"a": "urn:ne:entity:acc"}]
        # Move both endpoints AND the edge alpha→beta; the edge lands per the
        # intersection rule and the vacated alpha graph no longer carries it.
        await mem.update_acl(["per", "acc", "w1"], ["beta"])
        r2 = await PropertyGraphLift(mem, gw).reconcile()
        assert r2.quarantined == ()
        edge_q = f"SELECT ?a WHERE {{ <urn:ne:entity:per> <{NEOP}works_at> ?a }}"
        beta = await gw.sparql_scoped(edge_q, dataset=graphs_for_labels(["beta"]))
        assert beta == [{"a": "urn:ne:entity:acc"}]
        alpha = await gw.sparql_scoped(edge_q, dataset=graphs_for_labels(["alpha"]))
        assert alpha == []

    asyncio.run(scenario())


def test_replace_acl_graph_refuses_non_acl_graph() -> None:
    async def scenario() -> None:
        gw = _gateway()
        settings = OntologySettings(include_ontology_graph=False)
        triple = [("urn:ne:entity:x", _RDF_TYPE, NEOP + "Account")]
        for bad in (
            "urn:ne:ontology:ne:operational-alignment",
            settings.data_graph_iri,
            "urn:ne:ontology:sync-state",
        ):
            with pytest.raises(ValueError, match="urn:ne:graph:acl"):
                await gw.replace_acl_graph(triple, graph_iri=bad)

    asyncio.run(scenario())
