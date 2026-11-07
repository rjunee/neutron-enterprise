# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-085b FIX-2: validate_full_graph() sees the lift's per-ACL named graphs.

Pre-fix, ``validate_full_graph()`` validated ONLY ``settings.data_graph_iri`` — the
lift writes into per-ACL-label named graphs, so the nightly/global conformance gate
(including the promoted BFO-conformance gate + global curated shapes) was BLIND to
every lifted triple (a fail-open governance hole). These tests plant a violation in
an ACL graph and prove the widened dataset now turns the gate RED, while a clean
lifted graph stays green and instance-shape junk under ``urn:ne:ontology:*`` is
excluded (it is schema, not instance data).
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from ne.brain_graph.testing.memory import InMemoryGraphStore
from ne.brain_ontology import PropertyGraphLift, graph_iri_for_label
from ne.brain_ontology.config import OntologySettings
from ne.brain_ontology.gateway import ValidatingTripleStore
from ne.brain_ontology.lift.graph_to_rdf import NEOP
from ne.brain_ontology.quarantine import SqliteTripleQuarantine
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


def _gateway(sink: _RecordingSink) -> tuple[ValidatingTripleStore, OxigraphBackend]:
    settings = OntologySettings(include_ontology_graph=False)
    backend = OxigraphBackend()
    gw = ValidatingTripleStore(
        backend,
        build_validator(settings),
        SqliteTripleQuarantine(),
        sink,
        settings=settings,
        config_hash=_CONFIG_HASH,
        compiled_shapes=load_compiled_shapes(),
    )
    return gw, backend


def _account(key: str, acl: list[str]) -> dict[str, Any]:
    return {
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
        "name": "Acme Corp",
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


async def _seed(nodes: list[Mapping[str, Any]]) -> InMemoryGraphStore:
    mem = InMemoryGraphStore()
    await mem.upsert_nodes(nodes)
    return mem


# A bare neop:Document typed node with NO title/doc_type/id → per-write minCount
# violations. The test-only backend bypass simulates post-lift drift inside an ACL
# graph that the pre-fix validate_full_graph() never looked at.
_BARE_DOCUMENT_NT = (f"<urn:ne:entity:bad> <{_RDF_TYPE}> <{NEOP}Document> .\n").encode()


def test_planted_violation_in_acl_named_graph_turns_full_validation_red() -> None:
    # FIX-2 (§5-row): a SHACL-violating node living ONLY in a per-ACL named graph
    # turns validate_full_graph() conforms=False and emits a NON-quarantined
    # ShapeViolation. Pre-fix this falsely returned conforms=True (the blindness).
    async def scenario() -> None:
        sink = _RecordingSink()
        gw, backend = _gateway(sink)
        await backend.insert_nt(_BARE_DOCUMENT_NT, graph_iri=graph_iri_for_label("secret"))
        report = await gw.validate_full_graph()
        assert report.conforms is False
        assert report.violations
        assert len(sink.events) == 1
        assert sink.events[0].quarantined is False

    asyncio.run(scenario())


def test_clean_lifted_graph_passes_full_validation() -> None:
    # No false positives: a genuinely valid lifted graph (Account+Person across ACL
    # graphs, via the real gateway) keeps the widened full-graph gate green.
    async def scenario() -> None:
        sink = _RecordingSink()
        gw, _backend = _gateway(sink)
        mem = await _seed([_account("acc", ["alpha"]), _person("per", ["beta"])])
        report = await PropertyGraphLift(mem, gw).run()
        assert report.nodes_lifted == 2
        assert report.quarantined == ()
        full = await gw.validate_full_graph()
        assert full.conforms is True
        assert sink.events == []

    asyncio.run(scenario())


def test_ontology_graphs_are_excluded_from_instance_validation() -> None:
    # Exclusion boundary: the SAME bare neop:Document node planted under
    # urn:ne:ontology:* is NOT pulled in as instance data (it is schema), so
    # validate_full_graph() stays conforms=True.
    async def scenario() -> None:
        sink = _RecordingSink()
        gw, backend = _gateway(sink)
        await backend.insert_nt(_BARE_DOCUMENT_NT, graph_iri="urn:ne:ontology:planted")
        report = await gw.validate_full_graph()
        assert report.conforms is True
        assert sink.events == []

    asyncio.run(scenario())
