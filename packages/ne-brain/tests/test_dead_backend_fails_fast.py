# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""A dead backend must cost one round trip and must NAME itself.

The trigger was a rented GPU rig at ``10.77.0.2`` that died and stayed in the
deployment's config; the mechanism these tests pin is what turned that into a
catastrophe rather than an outage. ``httpx.AsyncClient(timeout=x)`` sets ONE
scalar for connect/read/write/pool, and this lane's scalar is deliberately huge
(``NE_TEI_RERANK_TIMEOUT_S=180``) because the on-box CPU reranker legitimately
takes tens of seconds. Against a black-holed address no SYN is answered, so the
deadline that actually fired was the kernel's SYN-retry ceiling
(``net.ipv4.tcp_syn_retries=6``, ~130 s): every search burned ~133 s and then
raised a bare ``ConnectError`` that did not say WHICH host was dead.

Both halves are pinned here, for all three outbound clients in ne-brain, because
the fix is worthless if either half regresses: a long connect deadline is slow,
and an anonymous error is undiagnosable.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from ne.brain import embedder as embedder_module
from ne.brain import reranker as reranker_module
from ne.brain.embedder import TEIEmbedder
from ne.brain.reranker import TEIReranker, VllmReranker
from ne.contracts.types import Chunk

#: The demo overlay's real rerank budget, and the value that made the defect
#: expensive. It is a READ budget and must survive the fix intact.
_OPERATOR_TIMEOUT_S = 180.0

#: The rig that died. Private, so it passes the C2 in-VPC check unaided.
_DEAD_BASE_URL = "http://10.77.0.2:10100"


def _chunks() -> list[Chunk]:
    return [Chunk(chunk_id="c0", document_id="d1", text="text")]


def _dead_transport() -> httpx.MockTransport:
    """A black-holed peer, as httpx reports it once its connect deadline fires."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    return httpx.MockTransport(handler)


# --------------------------------------------------------------------------
# Half 1: the connect phase is bounded separately, and the read budget survives.
# --------------------------------------------------------------------------


def test_every_owned_client_bounds_connect_without_shortening_the_read() -> None:
    """All three owned clients: connect 5 s, read/write/pool the operator's 180 s.

    Before the fix every one of these was ``connect == 180.0`` — one scalar for
    four phases — which is the whole defect. The read assertion is the other
    half of the contract and is NOT incidental: shortening it would break the
    slow-but-alive CPU rerank lane (78.1 s at 4 cpus, measured in this repo),
    which is a worse outcome than the one being fixed.
    """
    tei_rerank = TEIReranker(_DEAD_BASE_URL, timeout=_OPERATOR_TIMEOUT_S)
    vllm_rerank = VllmReranker(_DEAD_BASE_URL, timeout=_OPERATOR_TIMEOUT_S)
    embed = TEIEmbedder(_DEAD_BASE_URL, timeout=_OPERATOR_TIMEOUT_S)

    async def owned() -> list[httpx.Timeout]:
        # The embedder's factory is per-event-loop, so it needs a running loop.
        return [
            tei_rerank._get_client().timeout,
            vllm_rerank._get_client().timeout,
            embed._get_client().timeout,
        ]

    for timeout in asyncio.run(owned()):
        assert timeout.connect == 5.0
        assert timeout.read == _OPERATOR_TIMEOUT_S
        assert timeout.write == _OPERATOR_TIMEOUT_S
        assert timeout.pool == _OPERATOR_TIMEOUT_S


def test_the_connect_deadline_is_well_under_the_kernel_syn_ceiling() -> None:
    """The value has to beat ~130 s of SYN retries, or splitting the phase buys nothing.

    The kernel ceiling is what fired before; a connect deadline anywhere near it
    would simply reinstate the defect with extra ceremony.
    """
    assert reranker_module._CONNECT_TIMEOUT_S < 30.0


def test_the_two_adapter_modules_cannot_drift_apart() -> None:
    """reranker.py and embedder.py each own a copy; they must stay one value.

    Sibling adapters, one network property. If a future edit tunes one lane's
    connect deadline and forgets the other, the deployment gets two different
    answers to "how long is a dead host allowed to take", which is exactly the
    kind of split that made this defect hard to see in the first place.
    """
    assert embedder_module._CONNECT_TIMEOUT_S == reranker_module._CONNECT_TIMEOUT_S


# --------------------------------------------------------------------------
# Half 2: the error names the endpoint, and never a credential.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("build", "call", "lane"),
    [
        pytest.param(
            lambda client: TEIReranker(_DEAD_BASE_URL, client=client),
            lambda adapter: adapter.rerank("q", _chunks()),
            "rerank (TEI)",
            id="tei-rerank",
        ),
        pytest.param(
            lambda client: VllmReranker(_DEAD_BASE_URL, client=client),
            lambda adapter: adapter.rerank("q", _chunks()),
            "rerank (vLLM)",
            id="vllm-rerank",
        ),
        pytest.param(
            lambda client: TEIEmbedder(_DEAD_BASE_URL, client=client),
            lambda adapter: adapter.embed(["text"]),
            "embed (TEI)",
            id="tei-embed",
        ),
    ],
)
def test_a_dead_backend_error_names_the_host_the_port_and_the_failure_mode(
    build: object, call: object, lane: str
) -> None:
    """Before the fix this raised a bare ``ConnectTimeout`` naming nothing.

    An operator reading the traceback needs three facts and got none of them:
    which lane, which host, and what went wrong. Diagnosing the real incident
    took kernel-timing arithmetic because the exception itself was anonymous.
    """
    adapter = build(httpx.AsyncClient(transport=_dead_transport()))  # type: ignore[operator]

    with pytest.raises(httpx.TransportError) as excinfo:
        asyncio.run(call(adapter))  # type: ignore[operator]

    message = str(excinfo.value)
    assert lane in message
    assert "10.77.0.2" in message
    assert "10100" in message
    # The failure mode is not traded away for the address.
    assert "ConnectTimeout" in message
    # Still a transport error, so ne-api's _BACKEND_ERRORS keeps mapping it to
    # the same 502 — and its class-name-only detail keeps the host off the wire.
    assert isinstance(excinfo.value, httpx.TransportError)
    assert excinfo.value.__cause__ is not None


@pytest.mark.parametrize(
    "label", [reranker_module._endpoint_label, embedder_module._endpoint_label]
)
def test_the_endpoint_label_can_never_carry_a_credential(label: object) -> None:
    """Naming the host must not become a way to log a password.

    The label is rebuilt from the parsed URL rather than sliced out of the
    configured ``base_url``, so userinfo, path and query cannot ride along even
    if a deployment ever puts them there.
    """
    rendered = label("http://svc:hunter2@10.77.0.2:10100/v1?token=abc")  # type: ignore[operator]
    assert rendered == "http://10.77.0.2:10100"
    assert "hunter2" not in rendered
    assert "svc" not in rendered
    assert "abc" not in rendered
