# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Below-seam unit test for TEIEmbedder: HTTP shape via httpx.MockTransport.

This is a below-seam unit test (no real TEI server). Live acceptance against
a real TEI server is a separate test: test_tei_embedder_live.py.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from ne.brain.embedder import TEIEmbedder
from ne.brain.sovereignty import UnsafeEndpointError
from ne.net.airgap import AirGapViolationError

_DIM = 4


def _payload(request: httpx.Request) -> dict:
    return json.loads(request.read())


def _make_client(recorded: list[httpx.Request]) -> httpx.AsyncClient:
    async def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        body = _payload(request)
        vectors = [[float(len(text))] * _DIM for text in body["inputs"]]
        return httpx.Response(200, json=vectors)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_embed_posts_expected_request_shape() -> None:
    recorded: list[httpx.Request] = []
    client = _make_client(recorded)
    embedder = TEIEmbedder("http://tei.local", dimension=_DIM, client=client)

    vectors = asyncio.run(embedder.embed(["hello", "world"]))

    assert len(recorded) == 1
    request = recorded[0]
    assert request.url.path == "/embed"
    payload = _payload(request)
    assert payload["inputs"] == ["hello", "world"]
    assert payload["normalize"] is True
    assert vectors == [[5.0] * _DIM, [5.0] * _DIM]


def test_embed_batches_at_batch_size() -> None:
    recorded: list[httpx.Request] = []
    client = _make_client(recorded)
    embedder = TEIEmbedder("http://tei.local", dimension=_DIM, client=client, batch_size=2)

    texts = ["a", "bb", "ccc", "dddd", "e"]
    asyncio.run(embedder.embed(texts))

    assert len(recorded) == 3  # batches of 2, 2, 1
    sizes = [len(_payload(r)["inputs"]) for r in recorded]
    assert sizes == [2, 2, 1]


def test_model_id_and_dimension_properties() -> None:
    client = _make_client([])
    embedder = TEIEmbedder(
        "http://tei.local", model_id="Qwen/Qwen3-Embedding-0.6B", dimension=1024, client=client
    )
    assert embedder.model_id == "Qwen/Qwen3-Embedding-0.6B"
    assert embedder.dimension == 1024


def test_embed_empty_input_makes_no_http_call() -> None:
    recorded: list[httpx.Request] = []
    client = _make_client(recorded)
    embedder = TEIEmbedder("http://tei.local", dimension=_DIM, client=client)

    vectors = asyncio.run(embedder.embed([]))

    assert vectors == []
    assert recorded == []


def test_a_gap_engaged_after_construction_stops_the_embedder_sending() -> None:
    """The runtime flip, which construction-time validation cannot see.

    An allowlisted public IP literal (the rented-rig shape) is admitted while the
    gap is open. A governed ``write.internal.network.egress.restrict`` flip then
    engages it in a process whose embedder was built earlier. Corpus text must
    stop leaving — the reranker gained this check and this client silently did
    not, which is the defect this pins.
    """
    recorded: list[httpx.Request] = []
    client = _make_client(recorded)
    engaged = False

    embedder = TEIEmbedder(
        "http://8.8.8.8:8085",
        dimension=_DIM,
        client=client,
        allowed_hosts=("8.8.8.8",),
        air_gap=lambda: engaged,
    )

    assert asyncio.run(embedder.embed(["before the flip"]))
    assert len(recorded) == 1, "the open-gap control must actually reach the server"

    engaged = True
    with pytest.raises(AirGapViolationError) as excinfo:
        asyncio.run(embedder.embed(["after the flip"]))

    assert len(recorded) == 1, "corpus text left the process after the air gap engaged"
    assert "8.8.8.8" in str(excinfo.value)
    assert "after the flip" not in str(excinfo.value), (
        "C12: the message names the host, never the payload"
    )


def test_a_posture_that_breaks_after_construction_still_fails_closed() -> None:
    """Fail-closed at the per-batch check, not only at construction.

    Construction with a healthy provider is the realistic path; the posture store
    can become unreadable afterwards. An unreadable posture must never be read as
    "no air gap" — that would open egress precisely when the platform has lost
    track of whether it is sealed.
    """
    recorded: list[httpx.Request] = []
    client = _make_client(recorded)
    healthy = True

    def posture() -> bool:
        if healthy:
            return False
        raise RuntimeError("posture store unreachable")

    embedder = TEIEmbedder(
        "http://8.8.8.8:8085",
        dimension=_DIM,
        client=client,
        allowed_hosts=("8.8.8.8",),
        air_gap=posture,
    )
    assert asyncio.run(embedder.embed(["while readable"]))
    assert len(recorded) == 1

    healthy = False
    with pytest.raises(AirGapViolationError):
        asyncio.run(embedder.embed(["while unreadable"]))
    assert len(recorded) == 1


def test_an_unreadable_posture_at_construction_refuses_the_endpoint() -> None:
    """The same fail-closed rule one moment earlier, and it is the STRONGER
    outcome: the endpoint is never accepted at all, so no client exists to send."""

    def broken() -> bool:
        raise RuntimeError("posture store unreachable")

    with pytest.raises(UnsafeEndpointError):
        TEIEmbedder(
            "http://8.8.8.8:8085",
            dimension=_DIM,
            allowed_hosts=("8.8.8.8",),
            air_gap=broken,
        )


def test_a_flip_between_batches_stops_the_remaining_batches() -> None:
    """The check belongs INSIDE the batch loop.

    A corpus ingest is many batches over many seconds, so the realistic moment
    for a governed flip is *between* two of them. A check placed once before the
    loop stops nothing that has not already been sent — the first batch is
    refused and every later batch still leaves.
    """
    recorded: list[httpx.Request] = []
    engaged = False

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal engaged
        recorded.append(request)
        engaged = True  # the flip lands while batch 1 is in flight
        body = json.loads(request.read())
        return httpx.Response(200, json=[[1.0] * _DIM for _ in body["inputs"]])

    embedder = TEIEmbedder(
        "http://8.8.8.8:8085",
        dimension=_DIM,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        allowed_hosts=("8.8.8.8",),
        batch_size=1,
        air_gap=lambda: engaged,
    )

    with pytest.raises(AirGapViolationError):
        asyncio.run(embedder.embed(["one", "two", "three"]))

    assert len(recorded) == 1, (
        f"{len(recorded)} batches left the process; the air gap engaged after the first"
    )


def test_owned_client_is_built_per_event_loop() -> None:
    """2026-08-28 cron-offload incident: an owned httpx.AsyncClient binds its
    pool to the first loop that uses it, and the composed embedder is shared by
    the worker's DBOS turn loop AND its persistent cron-tick loop. Owned-client
    mode must therefore hand each event loop its OWN client (and reuse it on
    the same loop); injected-client mode keeps the single instance."""
    embedder = TEIEmbedder("http://tei.local", dimension=_DIM)

    async def get_twice() -> tuple[httpx.AsyncClient, httpx.AsyncClient]:
        return embedder._get_client(), embedder._get_client()

    first_a, first_b = asyncio.run(get_twice())
    second_a, second_b = asyncio.run(get_twice())
    assert first_a is first_b, "same loop must reuse its owned client"
    assert second_a is second_b
    assert first_a is not second_a, "a different loop must get its own client"

    injected = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    pinned = TEIEmbedder("http://tei.local", dimension=_DIM, client=injected)

    async def get_injected() -> httpx.AsyncClient:
        return pinned._get_client()

    assert asyncio.run(get_injected()) is injected
    assert asyncio.run(get_injected()) is injected
