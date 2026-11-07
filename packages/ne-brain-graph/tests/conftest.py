# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Pytest fixtures wrapping the shippable ``ne.brain_graph.testing.fixtures``.

Reusable pure helpers (FixtureConnector, the canonical mapping, make_node/edge)
live in the importable ``testing.fixtures`` module so every test can import them
directly under ``--import-mode=importlib``; this file only exposes them as pytest
fixtures for convenience.
"""

from __future__ import annotations

import pytest

from ne.brain_graph.testing.fixtures import FixtureConnector
from ne.brain_graph.testing.memory import InMemoryGraphStore


@pytest.fixture
def fixture_connector() -> FixtureConnector:
    return FixtureConnector()


@pytest.fixture
def in_memory_store() -> InMemoryGraphStore:
    return InMemoryGraphStore()
