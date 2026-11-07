# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Dual-backend conformance kit for the ``triple_store`` slot (NE-085a, C5).

The SAME parametrized cases run against every backend: oxigraph in-process
(ALWAYS), Fuseki (LIVE-PARKED via the ``backend`` fixture — skipped without a
reachable ``NE_FUSEKI_URL``). This kit IS the backend contract — a future
QLever/GraphDB adapter merges only by passing it. SPARQL 1.1 Query/Update ONLY:
no SERVICE / text:query / vendor functions anywhere in these queries.
"""

from __future__ import annotations

import asyncio

from ne.brain_ontology.store.protocols import DatasetSpec, TripleStoreBackend

_G1 = "urn:ne:test:g1"
_G2 = "urn:ne:test:g2"
_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"


def test_named_graph_crud_and_listing(backend: TripleStoreBackend) -> None:
    async def scenario() -> None:
        await backend.load_turtle(b'<http://ex/a> <http://ex/p> "in-g1" .', graph_iri=_G1)
        graphs = set(await backend.graphs())
        assert _G1 in graphs
        # replace_graph_turtle is destructive (PUT): the old triple is gone.
        await backend.replace_graph_turtle(
            b'<http://ex/a> <http://ex/p> "replaced" .', graph_iri=_G1
        )
        rows = await backend.select(
            "SELECT ?o WHERE { GRAPH <" + _G1 + "> { <http://ex/a> <http://ex/p> ?o } }",
            dataset=DatasetSpec(),
        )
        assert rows == [{"o": "replaced"}]
        await backend.delete_graph(_G1)
        assert _G1 not in set(await backend.graphs())

    asyncio.run(scenario())


def test_select_ask_parity(backend: TripleStoreBackend) -> None:
    async def scenario() -> None:
        await backend.load_turtle(b'<http://ex/s> <http://ex/p> "v" .', graph_iri=_G1)
        rows = await backend.select(
            "SELECT ?s WHERE { ?s <http://ex/p> ?o }", dataset=DatasetSpec()
        )
        assert rows == [{"s": "http://ex/s"}]
        present = await backend.ask("ASK { ?s <http://ex/p> ?o }", dataset=DatasetSpec())
        absent = await backend.ask("ASK { ?s <http://ex/nope> ?o }", dataset=DatasetSpec())
        assert present is True
        assert absent is False
        await backend.delete_graph(_G1)

    asyncio.run(scenario())


def test_construct_nt_roundtrip(backend: TripleStoreBackend) -> None:
    async def scenario() -> None:
        await backend.insert_nt(b"<http://ex/s> <http://ex/p> <http://ex/o> .\n", graph_iri=_G1)
        nt = await backend.construct_nt(
            "CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o }", dataset=DatasetSpec()
        )
        assert b"http://ex/s" in nt and b"http://ex/o" in nt
        await backend.delete_graph(_G1)

    asyncio.run(scenario())


def test_unicode_and_datatype_roundtrip(backend: TripleStoreBackend) -> None:
    async def scenario() -> None:
        await backend.load_turtle(
            f'<http://ex/x> <{_LABEL}> "café — naïve — 日本語" .'.encode(),
            graph_iri=_G1,
        )
        rows = await backend.select(
            "SELECT ?o WHERE { <http://ex/x> <" + _LABEL + "> ?o }", dataset=DatasetSpec()
        )
        assert rows == [{"o": "café — naïve — 日本語"}]
        await backend.delete_graph(_G1)

    asyncio.run(scenario())


def test_large_batch_insert(backend: TripleStoreBackend) -> None:
    async def scenario() -> None:
        lines = "".join(f'<http://ex/s{i}> <http://ex/p> "v{i}" .\n' for i in range(5000)).encode(
            "utf-8"
        )
        await backend.insert_nt(lines, graph_iri=_G1)
        rows = await backend.select(
            "SELECT (COUNT(*) AS ?n) WHERE { ?s <http://ex/p> ?o }", dataset=DatasetSpec()
        )
        assert int(rows[0]["n"]) == 5000
        await backend.delete_graph(_G1)

    asyncio.run(scenario())


def test_dataset_spec_named_graph_scoping(backend: TripleStoreBackend) -> None:
    async def scenario() -> None:
        await backend.load_turtle(b'<http://ex/a> <http://ex/p> "g1" .', graph_iri=_G1)
        await backend.load_turtle(b'<http://ex/b> <http://ex/p> "g2" .', graph_iri=_G2)
        # A default_graphs restricted to G1 must NOT see G2's triple.
        rows = await backend.select(
            "SELECT ?s WHERE { ?s <http://ex/p> ?o }",
            dataset=DatasetSpec(default_graphs=(_G1,)),
        )
        assert {r["s"] for r in rows} == {"http://ex/a"}
        await backend.delete_graph(_G1)
        await backend.delete_graph(_G2)

    asyncio.run(scenario())
