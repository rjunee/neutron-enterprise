# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Import + no-bypass structural tests for ne-brain-ontology (brain-tier3-spec §12.2)."""

from __future__ import annotations

import importlib.metadata

import ne.brain_ontology
from ne.brain_ontology.gateway import ValidatingTripleStore
from ne.contracts.plugins import load_plugin


def test_import_and_version() -> None:
    assert ne.brain_ontology.__version__ == "0.6.0"
    # NE-CLEAN5 (085d-followup): pin the runtime constant in lockstep with the
    # distribution metadata (pyproject/uv.lock) so the two can never diverge
    # again without a test failure.
    assert ne.brain_ontology.__version__ == importlib.metadata.version("ne-brain-ontology")


def test_backends_absent_from_public_api() -> None:
    # The raw backends are NEVER exported: every write must go through the gateway.
    exported = set(ne.brain_ontology.__all__)
    assert "OxigraphBackend" not in exported
    assert "FusekiBackend" not in exported
    assert "TripleStoreBackend" not in exported
    assert not hasattr(ne.brain_ontology, "OxigraphBackend")


def test_lift_and_acl_surfaces_exported() -> None:
    # NE-085b public surface: the lift + ACL partitioning helpers + registry types
    # ARE exported (085c consumes them); no backend is.
    exported = set(ne.brain_ontology.__all__)
    for name in (
        "PropertyGraphLift",
        "LiftReport",
        "GraphScanSource",
        "graphs_for_labels",
        "graph_iri_for_label",
        "OntologyVersion",
        "SyncReport",
    ):
        assert name in exported, name
        assert hasattr(ne.brain_ontology, name), name


def test_triple_store_entry_points_yield_gateway() -> None:
    # Both entry points resolve to gateway FACTORIES whose product is the
    # ValidatingTripleStore — never a raw backend.
    for name in ("oxigraph", "fuseki"):
        factory = load_plugin("triple_store", name)
        assert callable(factory)
    store = load_plugin("triple_store", "oxigraph")()
    assert isinstance(store, ValidatingTripleStore)


def test_shape_validator_entry_point_resolves() -> None:
    factory = load_plugin("shape_validator", "pyshacl")
    assert callable(factory)
