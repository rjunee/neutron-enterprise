# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""FusekiBackend MRSW retry + GSP wiring via httpx.MockTransport (NE-085a).

These ALWAYS run (no server): they pin the de-naased operational knowledge —
transient 500/503 retry-with-backoff, 4xx non-retry, and the Graph Store Protocol
verbs/params — without a live Fuseki (that lane is LIVE-PARKED, §9).
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from ne.brain_ontology.store.fuseki import FusekiBackend, FusekiError
from ne.brain_ontology.store.protocols import DatasetSpec

_BASE = "http://fuseki.test/ne"


def _backend(handler: object, **kwargs: object) -> FusekiBackend:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
    return FusekiBackend(_BASE, client=client, retry_base_delay_s=0.0, **kwargs)  # type: ignore[arg-type]


def test_write_recovers_after_two_503() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(503, text="lock held")
        return httpx.Response(200)

    async def scenario() -> None:
        backend = _backend(handler, retry_attempts=5)
        await backend.insert_nt(b"<urn:s> <urn:p> <urn:o> .\n", graph_iri="urn:g")
        assert len(calls) == 3  # 503, 503, 200

    asyncio.run(scenario())


def test_write_raises_after_retries_exhausted() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async def scenario() -> None:
        backend = _backend(handler, retry_attempts=2)
        with pytest.raises(FusekiError) as excinfo:
            await backend.insert_nt(b"<urn:s> <urn:p> <urn:o> .\n", graph_iri="urn:g")
        assert excinfo.value.status_code == 503
        assert excinfo.value.attempts == 3  # initial + 2 retries

    asyncio.run(scenario())


def test_4xx_raises_immediately_without_retry() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(400, text="bad request")

    async def scenario() -> None:
        backend = _backend(handler, retry_attempts=5)
        with pytest.raises(FusekiError) as excinfo:
            await backend.load_turtle(b'<urn:s> <urn:p> "x" .', graph_iri="urn:g")
        assert excinfo.value.status_code == 400
        assert len(calls) == 1  # no retry on 4xx

    asyncio.run(scenario())


def test_gsp_verbs_hit_expected_urls_and_params() -> None:
    seen: list[tuple[str, str, dict[str, list[str]]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, str(request.url.copy_with(query=None)), _params(request)))
        return httpx.Response(200)

    async def scenario() -> None:
        backend = _backend(handler)
        await backend.insert_nt(b"<urn:s> <urn:p> <urn:o> .\n", graph_iri="urn:g")
        await backend.replace_graph_turtle(b'<urn:s> <urn:p> "x" .', graph_iri="urn:g2")
        await backend.delete_graph("urn:g3")
        methods = [m for m, _, _ in seen]
        assert methods == ["POST", "PUT", "DELETE"]
        for _method, url, params in seen:
            assert url == f"{_BASE}/data"
            assert "graph" in params  # every GSP call targets ?graph=<iri>

    asyncio.run(scenario())


def test_select_uses_default_graph_uri_param_and_parses_json() -> None:
    seen: dict[str, list[str]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(_params(request))
        body = {
            "head": {"vars": ["s"]},
            "results": {"bindings": [{"s": {"type": "uri", "value": "http://ex/a"}}]},
        }
        return httpx.Response(
            200, text=json.dumps(body), headers={"Content-Type": "application/sparql-results+json"}
        )

    async def scenario() -> None:
        backend = _backend(handler)
        rows = await backend.select(
            "SELECT ?s WHERE { ?s ?p ?o }", dataset=DatasetSpec(default_graphs=("urn:d",))
        )
        assert rows == [{"s": "http://ex/a"}]
        assert seen.get("default-graph-uri") == ["urn:d"]

    asyncio.run(scenario())


def test_no_load_or_service_ever_issued() -> None:
    bodies: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content.decode("utf-8"))
        if "query" in str(request.url):
            return httpx.Response(
                200,
                text=json.dumps({"boolean": True}),
                headers={"Content-Type": "application/sparql-results+json"},
            )
        return httpx.Response(200)

    async def scenario() -> None:
        backend = _backend(handler)
        await backend.insert_nt(b"<urn:s> <urn:p> <urn:o> .\n", graph_iri="urn:g")
        await backend.health()
        for body in bodies:
            assert "LOAD" not in body.upper()
            assert "SERVICE" not in body.upper()

    asyncio.run(scenario())


def _params(request: httpx.Request) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for key, value in request.url.params.multi_items():
        out.setdefault(key, []).append(value)
    return out
