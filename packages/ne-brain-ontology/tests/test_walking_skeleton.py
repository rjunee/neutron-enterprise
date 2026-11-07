# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The NE-085a headline: the gateway walking skeleton, fully offline (C2).

verify assets → sync the vendored BFO/CCO corpus into an in-process oxigraph
backend → SPARQL finds a known BFO class → a conforming operational Person is
written THROUGH the ValidatingTripleStore gateway and is visible → a
non-conforming Person is quarantined and absent. Every step runs with sockets
disabled (C2 + fail-closed in one offline proof).
"""

from __future__ import annotations

import asyncio

from ne.brain_ontology.config import OntologySettings
from ne.brain_ontology.gateway import TripleQuarantinedError, ValidatingTripleStore
from ne.brain_ontology.ontology import load_manifest, verify_assets
from ne.brain_ontology.quarantine import SqliteTripleQuarantine
from ne.brain_ontology.shapes import build_validator, load_compiled_shapes
from ne.brain_ontology.store.oxigraph import OxigraphBackend
from ne.brain_ontology.store.sync import sync_ontologies

_RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
_NEOP = "https://neutron.enterprise/schema/operational/"
_PERSON = _NEOP + "Person"


class _CollectingSink:
    def __init__(self) -> None:
        self.events: list = []

    def emit(self, event: object) -> None:
        self.events.append(event)


def test_walking_skeleton_gateway_end_to_end_offline(no_network: None) -> None:
    async def scenario() -> None:
        # 1. Integrity gate (C11) then hash-diff sync the corpus offline (C2).
        manifest = load_manifest()
        verify_assets(manifest)
        backend = OxigraphBackend()
        report = await sync_ontologies(backend, manifest)
        assert report.loaded == 13  # 1 BFO + 11 CCO modules + 1 NE alignment

        settings = OntologySettings(include_ontology_graph=False)
        gateway = ValidatingTripleStore(
            backend,
            build_validator(settings),
            SqliteTripleQuarantine(),
            _CollectingSink(),
            settings=settings,
            config_hash="b" * 64,
            compiled_shapes=load_compiled_shapes(),
        )

        # 2. SPARQL finds the BFO 'continuant' class (union default sees the corpus).
        hits = await gateway.sparql(
            f'SELECT ?c WHERE {{ ?c <{_LABEL}> ?l FILTER(STR(?l) = "continuant") }}'
        )
        assert hits, "BFO 'continuant' not found in the synced corpus"

        # 3. A CONFORMING operational Person, written THROUGH the gateway, is visible.
        good = [
            ("urn:ne:p/erin", _RDF, _PERSON),
            ("urn:ne:p/erin", _NEOP + "id", "erin"),
            ("urn:ne:p/erin", _NEOP + "email", "erin@example.org"),
            ("urn:ne:p/erin", _NEOP + "family_name", "Example"),
            ("urn:ne:p/erin", _NEOP + "given_name", "Erin"),
        ]
        await gateway.insert_triples(good)
        rows = await gateway.sparql(f"SELECT ?s WHERE {{ ?s <{_RDF}> <{_PERSON}> }}")
        assert rows == [{"s": "urn:ne:p/erin"}]

        # 4. A label-less/required-slot-less Person is quarantined and NOT written.
        bad = [("urn:ne:p/frank", _RDF, _PERSON), ("urn:ne:p/frank", _NEOP + "id", "frank")]
        try:
            await gateway.insert_triples(bad)
            raise AssertionError("expected TripleQuarantinedError")
        except TripleQuarantinedError as exc:
            assert exc.violations
        frank = await gateway.sparql("SELECT ?p WHERE { <urn:ne:p/frank> ?p ?o }")
        assert frank == []

    asyncio.run(scenario())
