# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The C5 conformance kit over the in-memory reference store (always-on)."""

from __future__ import annotations

from ne.brain_graph.testing.kits import GraphStoreContractKit
from ne.brain_graph.testing.memory import InMemoryGraphStore


class TestInMemoryGraphStoreConformance(GraphStoreContractKit):
    def create_graph_store(self) -> InMemoryGraphStore:
        return InMemoryGraphStore()
