# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures for ne-brain-ontology tests (NE-085-spike).

The ``no_network`` fixture disables all outbound sockets so the C2 offline
guarantee is proven, not assumed: any vendored-corpus load, SHACL validation, or
owl:imports resolution that tries to touch the network fails the test instead of
silently succeeding.
"""

from __future__ import annotations

import asyncio
import os
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest

from ne.brain_ontology.ontology import ASSETS_DIR
from ne.brain_ontology.store.fuseki import FusekiBackend
from ne.brain_ontology.store.oxigraph import OxigraphBackend
from ne.brain_ontology.store.protocols import TripleStoreBackend

# Path constants shared across the suite. The ontology assets are PACKAGE DATA
# (NE-085a Codex P2 fix): resolve them via the package's importlib.resources
# root, never repo-layout path math — so the tests exercise the shipped layout.
PACKAGE_DIR = Path(__file__).resolve().parents[1]
ASSETS_ONTOLOGIES = ASSETS_DIR
BFO_TTL = ASSETS_ONTOLOGIES / "bfo" / "bfo-core.ttl"
TESTS_DATA = Path(__file__).resolve().parent / "data"
PERSON_SHAPE = TESTS_DATA / "shapes" / "person_shape.ttl"


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Fail any socket connection attempt (C2 offline proof)."""

    def _blocked(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("network disabled: C2 offline test")

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    yield


def _fuseki_reachable(url: str) -> bool:
    """Probe ASK {} within 2s so the Fuseki lane skips cleanly when unreachable."""

    async def _probe() -> bool:
        backend = FusekiBackend(url, timeout=2.0, retry_attempts=0)
        try:
            return await asyncio.wait_for(backend.health(), timeout=2.0)
        except Exception:
            return False
        finally:
            await backend.aclose()

    try:
        return asyncio.run(_probe())
    except Exception:
        return False


@pytest.fixture(params=["oxigraph", "fuseki"])
def backend(request: pytest.FixtureRequest) -> Iterator[TripleStoreBackend]:
    """Parametrized backend: oxigraph in-process ALWAYS; Fuseki LIVE-PARKED.

    The Fuseki lane runs only when ``NE_FUSEKI_URL`` answers ``ASK {}`` within 2s
    (a CI-service or local server); otherwise it is SKIPPED as LIVE-PARKED — never
    silently absent (ne-brain conftest precedent, no docker on the build host).
    """
    if request.param == "oxigraph":
        yield OxigraphBackend()
        return
    url = os.environ.get("NE_FUSEKI_URL")
    if not url or not _fuseki_reachable(url):
        pytest.skip("LIVE-PARKED: Fuseki unreachable (set NE_FUSEKI_URL to run this lane)")
    live = FusekiBackend(url)
    try:
        yield live
    finally:
        asyncio.run(live.aclose())
