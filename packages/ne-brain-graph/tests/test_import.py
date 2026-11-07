# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Import smoke test + plugin entry-point resolution for ne-brain-graph."""


def test_import_and_version() -> None:
    import ne.brain_graph

    assert ne.brain_graph.__version__ == "0.2.0"


def test_public_surface_importable() -> None:
    from ne.brain_graph import (
        AgeGraphStore,
        BrainGraphSettings,
        EntityGraph,
        GraphSyncRunner,
    )

    assert AgeGraphStore is not None
    assert BrainGraphSettings is not None
    assert EntityGraph is not None
    assert GraphSyncRunner is not None


def test_graph_store_plugin_resolvable() -> None:
    from ne.brain_graph.age.store import AgeGraphStore
    from ne.contracts.plugins import load_plugin

    loaded = load_plugin("graph_store", "age")
    assert loaded is AgeGraphStore
