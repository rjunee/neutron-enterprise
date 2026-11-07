# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""create_agent_memory backend flag: episodic default vs temporal-graph plugin (C5)."""

from __future__ import annotations

import importlib.metadata
from collections.abc import Iterator

import pytest
import sqlalchemy as sa

from ne.contracts.runtime import MemoryProvider
from ne.memory_graph.facade import TemporalAgentMemory
from ne.runtime.config import RuntimeSettings
from ne.runtime.factory import create_agent_memory
from ne.runtime.memory_store import AgentMemory, ensure_memory_schema


@pytest.fixture
def engine() -> Iterator[sa.Engine]:
    eng = sa.create_engine("sqlite://")
    ensure_memory_schema(eng)
    try:
        yield eng
    finally:
        eng.dispose()


def _as_provider(p: MemoryProvider) -> MemoryProvider:
    return p


def test_default_backend_is_agent_memory(engine: sa.Engine) -> None:
    provider = create_agent_memory("agent-a", settings=RuntimeSettings(), engine=engine)
    assert isinstance(provider, AgentMemory)
    _as_provider(provider)


def test_temporal_graph_backend_returns_temporal_agent_memory(engine: sa.Engine) -> None:
    settings = RuntimeSettings(memory_backend="temporal-graph")
    provider = create_agent_memory("agent-a", settings=settings, engine=engine)
    assert isinstance(provider, TemporalAgentMemory)
    _as_provider(provider)


def test_temporal_graph_missing_plugin_raises_actionable_error(
    engine: sa.Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _no_entry_points(*, group: str) -> list[object]:
        return []

    monkeypatch.setattr(importlib.metadata, "entry_points", _no_entry_points)
    settings = RuntimeSettings(memory_backend="temporal-graph")
    with pytest.raises(ValueError, match="ne-memory-graph package"):
        create_agent_memory("agent-a", settings=settings, engine=engine)
