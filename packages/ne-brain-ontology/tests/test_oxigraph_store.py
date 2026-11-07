# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""OxigraphBackend behavioral tests (NE-085a — retargeted from the spike).

The spike's frozen-Protocol OxigraphTripleStore is REPLACED by the gateway; the
same behavioral assertions now run through OxigraphBackend (the package-local
TripleStoreBackend) + the ValidatingTripleStore gateway (see test_gateway_*).
"""

from __future__ import annotations

import asyncio

from ne.brain_ontology.store.oxigraph import OxigraphBackend
from ne.brain_ontology.store.protocols import DatasetSpec, TripleStoreBackend

_RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"

# Structural conformance to the package-local backend Protocol — checked by pyright.
_conforms: TripleStoreBackend = OxigraphBackend()


def test_insert_nt_and_select_roundtrip() -> None:
    async def scenario() -> list:
        store = OxigraphBackend()
        await store.insert_nt(
            f"<http://ex/alice> <{_RDF_TYPE}> <http://ex/Person> .\n".encode(),
            graph_iri="urn:ne:data:default",
        )
        return list(
            await store.select(
                f"SELECT ?s WHERE {{ ?s <{_RDF_TYPE}> <http://ex/Person> }}",
                dataset=DatasetSpec(),
            )
        )

    assert asyncio.run(scenario()) == [{"s": "http://ex/alice"}]


def test_object_iri_vs_literal_via_turtle() -> None:
    async def scenario() -> tuple[set[str], set[str]]:
        store = OxigraphBackend()
        await store.load_turtle(
            (
                f"<http://ex/a> <{_LABEL}> <http://ex/SomeIri> .\n"
                f"<http://ex/b> <{_LABEL}> <urn:ne:thing> .\n"
                f'<http://ex/c> <{_LABEL}> "plain text label" .\n'
            ).encode(),
            graph_iri="urn:ne:data:default",
        )
        iri_rows = await store.select(
            f"SELECT ?s WHERE {{ ?s <{_LABEL}> ?o FILTER isIRI(?o) }}", dataset=DatasetSpec()
        )
        literal_rows = await store.select(
            f"SELECT ?s WHERE {{ ?s <{_LABEL}> ?o FILTER isLiteral(?o) }}", dataset=DatasetSpec()
        )
        return {r["s"] for r in iri_rows}, {r["s"] for r in literal_rows}

    iris, literals = asyncio.run(scenario())
    assert iris == {"http://ex/a", "http://ex/b"}
    assert literals == {"http://ex/c"}


def test_ask_query() -> None:
    async def scenario() -> tuple[bool, bool]:
        store = OxigraphBackend()
        await store.insert_nt(
            f"<http://ex/a> <{_RDF_TYPE}> <http://ex/Person> .\n".encode(),
            graph_iri="urn:ne:data:default",
        )
        present = await store.ask(
            f"ASK {{ ?s <{_RDF_TYPE}> <http://ex/Person> }}", dataset=DatasetSpec()
        )
        absent = await store.ask("ASK { ?s <http://ex/nope> ?o }", dataset=DatasetSpec())
        return present, absent

    present, absent = asyncio.run(scenario())
    assert present is True
    assert absent is False


def test_named_graph_union_default() -> None:
    async def scenario() -> list:
        store = OxigraphBackend()
        store_ttl = b'<http://ex/g> <http://ex/p> "in-named-graph" .'
        await store.load_turtle(store_ttl, graph_iri="urn:ne:ontology:test:g")
        # DatasetSpec() → union of all graphs, so the named-graph triple is visible.
        return list(
            await store.select(
                "SELECT ?o WHERE { <http://ex/g> <http://ex/p> ?o }", dataset=DatasetSpec()
            )
        )

    assert asyncio.run(scenario()) == [{"o": "in-named-graph"}]
