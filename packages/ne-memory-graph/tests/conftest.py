# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures for ne-memory-graph tests."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime

import httpx
import pytest
import sqlalchemy as sa

from ne.contracts.config import freeze_config_hash, reset_config_hash_for_testing
from ne.memory_graph.backfill import ensure_watermark_schema
from ne.memory_graph.client import MemoryGraphClient
from ne.memory_graph.config import MemoryGraphSettings
from ne.runtime.memory_store import SqlAlchemyEpisodicStore, ensure_memory_schema

T0 = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _freeze_config() -> Iterator[None]:
    """Freeze a config_hash so audit-emitting paths (backfill) don't raise.

    The reset on BOTH sides is load-bearing, not tidiness. The frozen hash is a
    process-global set once behind ``_frozen_lock``, so a freeze with no teardown
    leaks out of this package for the rest of the pytest process. ``ne-memory-graph``
    sorts before ``ne-permissions``, whose
    ``test_unfrozen_config_hash_raises_and_emits_nothing`` deliberately takes no
    ``frozen_config`` fixture and therefore has no reset of its own -- it simply
    observes the global. Without the teardown below that negative control stops
    raising and passes vacuously, which is exactly how it failed in CI.

    Every other package conftest that freezes already does this; this one was the
    lone outlier. The order pair
    ``test_age_codec.py::test_scalars`` then that permissions control is the
    durable regression control -- it fails within 0.1s if this teardown is removed.
    """
    reset_config_hash_for_testing()
    freeze_config_hash(MemoryGraphSettings())
    try:
        yield
    finally:
        reset_config_hash_for_testing()


@pytest.fixture
def store() -> Iterator[SqlAlchemyEpisodicStore]:
    engine = sa.create_engine("sqlite://")
    ensure_memory_schema(engine)
    ensure_watermark_schema(engine)
    try:
        yield SqlAlchemyEpisodicStore(engine)
    finally:
        engine.dispose()


class FakeSidecar:
    """An in-memory fake of the sidecar HTTP contract, driven via MockTransport.

    Records posted episodes and returns configured search hits. ``fail`` /
    ``status`` let a test simulate outages; ``hits`` is the /search response.
    """

    def __init__(
        self,
        *,
        hits: list[dict[str, object]] | None = None,
        episode_status: int = 200,
        search_status: int = 200,
        raise_connect: bool = False,
        raise_timeout: bool = False,
        fail_episodes_after: int | None = None,
    ) -> None:
        self.posted: list[dict[str, object]] = []
        self.search_bodies: list[dict[str, object]] = []
        self._hits = hits or []
        self._episode_status = episode_status
        self._search_status = search_status
        self._raise_connect = raise_connect
        self._raise_timeout = raise_timeout
        self._fail_episodes_after = fail_episodes_after

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self._raise_connect:
            raise httpx.ConnectError("connection refused", request=request)
        if self._raise_timeout:
            raise httpx.ReadTimeout("timed out", request=request)
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        if request.url.path == "/episodes":
            if (
                self._fail_episodes_after is not None
                and len(self.posted) >= self._fail_episodes_after
            ):
                return httpx.Response(500, json={"error": "boom"})
            self.posted.append(body)
            return httpx.Response(self._episode_status, json={"status": "ingested"})
        if request.url.path == "/search":
            self.search_bodies.append(body)
            if self._search_status >= 400:
                return httpx.Response(self._search_status, json={"error": "boom"})
            return httpx.Response(200, json={"hits": self._hits})
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(404)

    def client(self, settings: MemoryGraphSettings | None = None) -> MemoryGraphClient:
        transport = httpx.MockTransport(self.handler)
        return MemoryGraphClient(settings or MemoryGraphSettings(), transport=transport)
