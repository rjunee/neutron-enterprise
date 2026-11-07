# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Below-seam unit tests for VllmReranker (NE-SEARCH-LATENCY).

HTTP shape via httpx.MockTransport — the ``test_tei_reranker.py`` discipline.
The wire shape asserted here (``{"model", "query", "documents"}`` request →
``{"results": [{"index", "relevance_score"}]}`` response) was live-verified
against the pinned serve image ``vllm/vllm-openai:v0.24.0`` serving
``BAAI/bge-reranker-v2-m3`` on the demo GPU rig (2026-08-04).

The ONE-request property is load-bearing, not incidental: the attribution that
motivated this adapter measured the CPU TEI stage's sequential batch loop at
~40s per search (40 real ~600-char candidates at ~1s/pair, concurrency
measured useless — the 4-CPU TEI server is compute-saturated either way).
vLLM has no TEI-style ``max_client_batch_size`` request cap and schedules
internally, so this adapter must never grow a client-side batch loop back;
``test_rerank_makes_exactly_one_request_for_forty_documents`` goes RED if it
does.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from ne.brain.reranker import RerankResponseError, TEIReranker, VllmReranker
from ne.brain.sovereignty import UnsafeEndpointError
from ne.contracts.types import Chunk
from ne.net.airgap import AirGapViolationError

#: An obviously synthetic stand-in for customer corpus text. It exists so a leak
#: is unmistakable in an assertion message: nothing in the codebase, in vLLM, or
#: in a real corpus produces this string, so if it appears in an error message
#: the error is echoing the response body it was handed.
_SENTINEL = "SENTINEL-CHUNK-BODY-b3a1f7-DO-NOT-ECHO"


def _payload(request: httpx.Request) -> dict:
    return json.loads(request.read())


def _chunks(n: int) -> list[Chunk]:
    return [Chunk(chunk_id=f"c{i}", document_id="d1", text=f"text {i}") for i in range(n)]


def _overlap_client(recorded: list[httpx.Request]) -> httpx.AsyncClient:
    """Scores each document by token-overlap with the query; vLLM /rerank shape."""

    async def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        body = _payload(request)
        qtokens = set(body["query"].lower().split())
        results = [
            {
                "index": i,
                "relevance_score": len(qtokens & set(text.lower().split())) / max(len(qtokens), 1),
            }
            for i, text in enumerate(body["documents"])
        ]
        return httpx.Response(200, json={"id": "rerank-1", "results": results})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_rerank_posts_expected_request_shape() -> None:
    recorded: list[httpx.Request] = []
    reranker = VllmReranker("http://rerank.local", client=_overlap_client(recorded))

    asyncio.run(reranker.rerank("text 0", _chunks(2), top_k=2))

    assert len(recorded) == 1
    request = recorded[0]
    assert request.url.path == "/rerank"
    payload = _payload(request)
    assert payload["model"] == "rerank"
    assert payload["query"] == "text 0"
    assert payload["documents"] == ["text 0", "text 1"]


def test_rerank_request_carries_configured_served_model_name() -> None:
    recorded: list[httpx.Request] = []
    reranker = VllmReranker(
        "http://rerank.local", served_model_name="my-lane", client=_overlap_client(recorded)
    )

    asyncio.run(reranker.rerank("q", _chunks(1), top_k=1))

    assert _payload(recorded[0])["model"] == "my-lane"


def test_rerank_maps_indices_and_sorts_descending() -> None:
    # Handler returns results OUT OF ORDER with an inverted score ranking —
    # the client must never trust server ordering.
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 1, "relevance_score": 0.9},
                    {"index": 0, "relevance_score": 0.1},
                    {"index": 2, "relevance_score": 0.5},
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    reranker = VllmReranker("http://rerank.local", client=client)

    result = asyncio.run(reranker.rerank("q", _chunks(3), top_k=3))

    assert [sc.chunk.chunk_id for sc in result] == ["c1", "c2", "c0"]
    assert [sc.score for sc in result] == [0.9, 0.5, 0.1]


def test_rerank_score_ties_break_by_ascending_original_index() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 2, "relevance_score": 0.5},
                    {"index": 0, "relevance_score": 0.5},
                    {"index": 1, "relevance_score": 0.5},
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    reranker = VllmReranker("http://rerank.local", client=client)

    result = asyncio.run(reranker.rerank("q", _chunks(3), top_k=3))

    assert [sc.chunk.chunk_id for sc in result] == ["c0", "c1", "c2"]


def test_rerank_respects_top_k() -> None:
    recorded: list[httpx.Request] = []
    reranker = VllmReranker("http://rerank.local", client=_overlap_client(recorded))

    result = asyncio.run(reranker.rerank("text 0", _chunks(5), top_k=2))

    assert len(result) == 2


def test_rerank_makes_exactly_one_request_for_forty_documents() -> None:
    """THE structural latency property: no client-side batch loop, ever.

    40 documents is exactly the deployed ``top_k=10 * candidate_multiplier=4``
    fan-out whose sequential 32+8 TEI batching the attribution measured. If a
    batch loop is reintroduced (any ``len(recorded) > 1``), this goes RED.
    """
    recorded: list[httpx.Request] = []
    reranker = VllmReranker("http://rerank.local", client=_overlap_client(recorded))

    result = asyncio.run(reranker.rerank("text 39", _chunks(40), top_k=10))

    assert len(recorded) == 1
    assert len(_payload(recorded[0])["documents"]) == 40
    assert len(result) == 10


def test_rerank_empty_chunks_makes_no_http_call() -> None:
    recorded: list[httpx.Request] = []
    reranker = VllmReranker("http://rerank.local", client=_overlap_client(recorded))

    result = asyncio.run(reranker.rerank("q", [], top_k=5))

    assert list(result) == []
    assert recorded == []


@pytest.mark.parametrize(
    "body",
    [
        [{"index": 0, "relevance_score": 0.5}],  # bare list (the TEI shape, not vLLM's)
        {"no_results": []},
        {"results": [{"index": 99, "relevance_score": 0.5}]},  # index out of range
        {"results": [{"index": True, "relevance_score": 0.5}]},  # bool masquerading as int
        {"results": [{"index": 0, "relevance_score": "not-a-number"}]},
        {"results": [{"index": 0}]},  # missing relevance_score
        {"results": ["not-a-mapping"]},
    ],
)
def test_rerank_malformed_response_raises_value_error(body: object) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    reranker = VllmReranker("http://rerank.local", client=client)

    with pytest.raises(ValueError, match="malformed vLLM /rerank response"):
        asyncio.run(reranker.rerank("q", _chunks(2), top_k=2))


def test_rerank_wellformed_response_is_the_should_pass_control() -> None:
    """Control for the malformed battery: the exact live-verified shape passes."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "rerank-1",
                "model": "rerank",
                "usage": {"total_tokens": 10},
                "results": [
                    {"index": 0, "relevance_score": 0.99, "document": {"text": "text 0"}},
                    {"index": 1, "relevance_score": 0.01, "document": {"text": "text 1"}},
                ],
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    reranker = VllmReranker("http://rerank.local", client=client)

    result = asyncio.run(reranker.rerank("q", _chunks(2), top_k=2))

    assert [sc.chunk.chunk_id for sc in result] == ["c0", "c1"]


def test_model_id_property_default_is_bge_reranker_v2_m3() -> None:
    reranker = VllmReranker("http://rerank.local")
    assert reranker.model_id == "BAAI/bge-reranker-v2-m3"


# --- C2: construction-time endpoint validation (bidirectional) -----------------


def test_external_host_rejected_without_allowlist() -> None:
    """Fail-closed direction: a public-IP rerank endpoint is refused at
    CONSTRUCTION — no query/chunk byte can ever reach an unlisted host."""
    with pytest.raises(UnsafeEndpointError):
        VllmReranker("http://8.8.8.8:1837")


def test_external_host_accepted_with_allowlist() -> None:
    """Escape-hatch direction (the control): the SAME endpoint constructs when
    its host is explicitly allowlisted — this is the exact seam
    ``ne demo up --gpu-rerank`` rides (NE_API_RERANK_ALLOWED_HOSTS)."""
    reranker = VllmReranker("http://8.8.8.8:1837", allowed_hosts=("8.8.8.8",))
    assert reranker.model_id == "BAAI/bge-reranker-v2-m3"


def test_in_vpc_service_name_needs_no_allowlist() -> None:
    reranker = VllmReranker("http://rerank-gpu:80")
    assert reranker.model_id == "BAAI/bge-reranker-v2-m3"


# --- C2: AIR_GAP dominates the allowlist, at construction AND per request ------
#
# The rule itself is pinned in packages/ne-net/tests/test_ssrf.py. What is pinned
# HERE is the thing the rule exists to prevent: that no request carrying query or
# chunk text leaves this adapter while the gap is engaged — including a gap
# engaged AFTER construction, which is how the platform's governed air-gap flip
# actually works (a DB-backed posture change in a long-lived process).


def test_construction_refused_under_env_air_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIR_GAP", "1")
    with pytest.raises(UnsafeEndpointError, match="AIR_GAP is engaged"):
        VllmReranker("http://8.8.8.8:1837", allowed_hosts=("8.8.8.8",))


def test_construction_refused_under_injected_runtime_air_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The runtime posture seam: ne-api's governed egress register, not the env."""
    monkeypatch.delenv("AIR_GAP", raising=False)
    with pytest.raises(UnsafeEndpointError, match="AIR_GAP is engaged"):
        VllmReranker("http://8.8.8.8:1837", allowed_hosts=("8.8.8.8",), air_gap=lambda: True)


def test_air_gap_engaged_after_construction_emits_no_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE demo beat, below the seam.

    The adapter is built while the gap is OPEN (as it is at ne-api boot), then the
    gap is engaged — exactly what ``write.internal.network.egress.restrict`` does
    to a running process — and a search runs. Construction-time validation alone
    cannot see this; the per-request re-check must, and ZERO bytes may leave.
    """
    monkeypatch.delenv("AIR_GAP", raising=False)
    sealed = False
    recorded: list[httpx.Request] = []
    reranker = VllmReranker(
        "http://8.8.8.8:1837",
        allowed_hosts=("8.8.8.8",),
        client=_overlap_client(recorded),
        air_gap=lambda: sealed,
    )

    # CONTROL first: with the gap open the SAME adapter does send, so the denial
    # below is the air gap and not a broken client or a bad endpoint.
    asyncio.run(reranker.rerank("q", [Chunk(chunk_id="c0", document_id="d1", text=_SENTINEL)]))
    assert len(recorded) == 1

    sealed = True
    with pytest.raises(AirGapViolationError, match="AIR_GAP is engaged"):
        asyncio.run(reranker.rerank("q", [Chunk(chunk_id="c0", document_id="d1", text=_SENTINEL)]))

    assert len(recorded) == 1, "a request left the adapter with the air gap engaged"
    assert _SENTINEL not in "".join(r.read().decode() for r in recorded[1:])


def test_air_gap_engaged_after_construction_emits_no_request_via_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same property, driven by the bare process-global flag rather than a
    provider — the source the reviewer reproduced the P0 with."""
    monkeypatch.delenv("AIR_GAP", raising=False)
    recorded: list[httpx.Request] = []
    reranker = VllmReranker(
        "http://8.8.8.8:1837", allowed_hosts=("8.8.8.8",), client=_overlap_client(recorded)
    )

    monkeypatch.setenv("AIR_GAP", "1")
    with pytest.raises(AirGapViolationError):
        asyncio.run(reranker.rerank("q", _chunks(2), top_k=2))
    assert recorded == []


def test_air_gap_does_not_touch_an_in_vpc_rerank_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """The control that keeps the guard from being a blanket kill: the sealed
    on-box lane (a dotless compose service) reranks normally under AIR_GAP —
    that is the whole point of an air gap, and of the documented CPU revert."""
    monkeypatch.setenv("AIR_GAP", "1")
    recorded: list[httpx.Request] = []
    reranker = VllmReranker("http://rerank-gpu:80", client=_overlap_client(recorded))

    result = asyncio.run(reranker.rerank("text 0", _chunks(2), top_k=2))

    assert len(recorded) == 1
    assert [sc.chunk.chunk_id for sc in result] == ["c0", "c1"]


def test_air_gap_provider_that_raises_is_treated_as_engaged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail-closed: an unreadable posture must never open egress."""
    monkeypatch.delenv("AIR_GAP", raising=False)

    def _broken() -> bool:
        raise RuntimeError("posture store unreadable")

    with pytest.raises(UnsafeEndpointError, match="AIR_GAP is engaged"):
        VllmReranker("http://8.8.8.8:1837", allowed_hosts=("8.8.8.8",), air_gap=_broken)


# --- C12: the error message names the FIELD, never the VALUE -------------------


@pytest.mark.parametrize(
    "body",
    [
        # vLLM echoes the submitted documents back inside every result entry, so
        # each of these malformed shapes carries corpus text alongside the fault.
        {"results": [{"index": 0, "relevance_score": "NaNsense", "document": {"text": _SENTINEL}}]},
        {"results": [{"index": 99, "relevance_score": 0.5, "document": {"text": _SENTINEL}}]},
        {"results": [{"index": 0, "document": {"text": _SENTINEL}}]},
        {"results": [_SENTINEL]},
        {"no_results": [{"document": {"text": _SENTINEL}}]},
        [{"index": 0, "relevance_score": 0.5, "document": {"text": _SENTINEL}}],
    ],
)
def test_malformed_response_error_never_echoes_the_document_text(body: object) -> None:
    """The observed C12 leak: ``relevance_score not float-able: {..., 'document':
    {'text': 'CONFIDENTIAL-CHUNK-BODY'}}``. Corpus text reached an exception
    message and therefore the application log. Every malformed shape below hides
    a sentinel inside the echoed document; none of them may surface it."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    reranker = VllmReranker("http://rerank.local", client=client)

    with pytest.raises(RerankResponseError) as exc:
        asyncio.run(reranker.rerank("q", _chunks(2), top_k=2))

    message = str(exc.value)
    assert _SENTINEL not in message, f"corpus text leaked into the error message: {message!r}"
    assert "malformed" in message


def test_malformed_response_error_still_names_the_field_and_position() -> None:
    """The control for the battery above: hiding the value must not have cost the
    message its diagnostic content. A message that named nothing would pass the
    no-leak assertions vacuously."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 0, "relevance_score": 0.9, "document": {"text": _SENTINEL}},
                    {"index": 1, "relevance_score": "NaNsense", "document": {"text": _SENTINEL}},
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    reranker = VllmReranker("http://rerank.local", client=client)

    with pytest.raises(RerankResponseError) as exc:
        asyncio.run(reranker.rerank("q", _chunks(2), top_k=2))

    message = str(exc.value)
    assert "relevance_score" in message  # the FIELD
    assert "position 1" in message  # WHERE
    assert "str" in message  # the value's TYPE
    assert "NaNsense" not in message  # never the VALUE


def test_malformed_response_error_is_a_value_error_subclass() -> None:
    """``RerankResponseError`` keeps the historical ``ValueError`` contract (so
    every existing ``pytest.raises(ValueError)`` still holds) while being a
    distinct type ne-api can map to the field-only 502 instead of an opaque 500."""
    assert issubclass(RerankResponseError, ValueError)


def test_air_gap_stops_a_send_to_an_allowlisted_name_that_resolves_publicly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The spelling-independence property, at the wire.

    ``test_air_gap_engaged_after_construction_emits_no_request_via_env`` is this
    exact scenario with the IP literal ``8.8.8.8`` allowlisted. This is the same
    destination allowlisted as the NAME it resolves to (``dns.google`` — public
    A/AAAA only, resolved 2026-08-05), and it must behave identically. It did not:
    before the rule was fixed, this adapter POSTed the chunk text, so an engaged
    air gap stopped or did not stop the identical egress path purely on how the
    operator spelled the host.

    Asserted behaviourally — zero recorded requests, not merely "it raised" — and
    the adapter is built with the gap OPEN so this exercises the per-request
    re-check rather than construction. No real bytes leave the test box: the
    transport is a MockTransport recorder.
    """
    monkeypatch.delenv("AIR_GAP", raising=False)
    recorded: list[httpx.Request] = []
    reranker = VllmReranker(
        "http://dns.google:1837",
        allowed_hosts=("dns.google",),
        client=_overlap_client(recorded),
    )

    monkeypatch.setenv("AIR_GAP", "1")
    with pytest.raises(AirGapViolationError, match="AIR_GAP is engaged"):
        asyncio.run(
            reranker.rerank("q", [Chunk(chunk_id="c0", document_id="d1", text=_SENTINEL)], top_k=1)
        )

    assert recorded == [], "chunk text left the adapter for an allowlisted NAME under an air gap"


@pytest.mark.parametrize("raw", ["NaN", "Infinity", "-Infinity", "inf", "-inf", "nan"])
@pytest.mark.parametrize("backend", ["vllm", "tei"])
def test_non_finite_scores_are_rejected_not_ranked(backend: str, raw: str) -> None:
    """``float()`` accepts NaN and the infinities; this adapter must not.

    A non-finite score is not a parse curiosity, it breaks the ranking contract
    this class advertises. NaN compares unordered, so ``pairs.sort`` stops being
    a total order and the final ranking falls back to the server's response
    order — precisely what "never trust the server's response ordering" says it
    does not do (that divergence is pinned by the sibling test below). An
    infinity dominates every legitimate score, and a NaN reaching the API
    boundary is not JSON-serializable under ``allow_nan=False``.

    Both backends, all six spellings ``float()`` accepts.
    """
    body: object = (
        {"results": [{"index": 0, "relevance_score": raw}]}
        if backend == "vllm"
        else [{"index": 0, "score": raw}]
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    reranker: VllmReranker | TEIReranker = (
        VllmReranker("http://rerank.local", client=client)
        if backend == "vllm"
        else TEIReranker("http://rerank.local", client=client)
    )

    with pytest.raises(RerankResponseError, match="not finite") as exc:
        asyncio.run(reranker.rerank("q", _chunks(1), top_k=1))
    # C12 holds on the new arm too: the field and TYPE, never the value.
    assert "position 0" in str(exc.value)
    assert raw not in str(exc.value)


def test_a_non_finite_score_would_make_ranking_depend_on_response_order() -> None:
    """The CONTROL that gives the guard above its reason, and the should-fire
    demonstration that the defect was real rather than theoretical.

    Same three documents, same three scores, the ONLY difference being the order
    the server listed its results in. With the guard removed both calls return a
    ranking; they are DIFFERENT rankings. Here the guard converts both into the
    same refusal, so this asserts the refusal is order-independent — and the
    docstring records what it is protecting against.
    """

    def rank(order: list[int]) -> list[str]:
        """Return the resulting chunk-id ranking, or raise if the response is refused."""

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"index": i, "relevance_score": ("NaN" if i == 0 else 0.9 - 0.1 * i)}
                        for i in order
                    ]
                },
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        reranker = VllmReranker("http://rerank.local", client=client)
        return [sc.chunk.chunk_id for sc in asyncio.run(reranker.rerank("q", _chunks(3), top_k=3))]

    # Both response orderings are refused, so neither can yield a ranking at all.
    # Deliberately NOT asserting the two messages are byte-identical: they name the
    # offending entry's POSITION IN THE RESPONSE, which legitimately differs when
    # the server reorders — that is a response locus, not a ranking.
    for order in ([0, 1, 2], [2, 1, 0]):
        with pytest.raises(RerankResponseError, match="not finite"):
            rank(order)

    # Historical record of what is being prevented: with the guard removed these
    # two calls returned ['c0','c1','c2'] and ['c1','c0','c2'] respectively —
    # same documents, same scores, ranking decided by the server's response order.


def test_finite_edge_scores_are_still_accepted_control() -> None:
    """CONTROL for the finite guard: it must reject non-finite values WITHOUT
    rejecting legitimate extremes. Zero, negatives, and very large-but-finite
    magnitudes are all real cross-encoder outputs (raw logits are unbounded and
    frequently negative)."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 0, "relevance_score": 0.0},
                    {"index": 1, "relevance_score": -12.5},
                    {"index": 2, "relevance_score": 1.7976931348623157e308},
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    reranker = VllmReranker("http://rerank.local", client=client)

    result = asyncio.run(reranker.rerank("q", _chunks(3), top_k=3))

    assert [sc.chunk.chunk_id for sc in result] == ["c2", "c0", "c1"]


def test_parse_failure_chain_never_carries_the_response_value() -> None:
    """C12 chain sweep over BOTH score parsers — ``str(exc)`` is not the surface.

    The redacted outer message was already pinned by the battery above, but the
    conversion is ``float(raw_score)`` and CPython's own ``ValueError`` reads
    ``could not convert string to float: '<value>'`` — the untrusted response
    value verbatim. While the raise chained ``from exc`` that value survived in
    ``exc.__cause__`` and therefore in ``traceback.format_exception``, which is
    exactly the text a ``logger.exception`` / Sentry / OTel ``record_exception``
    ships. The redaction was one ordinary logging call away from being undone.

    The sweep asserts over the FORMATTED chain, and the per-site
    ``__cause__``/``__suppress_context__`` asserts make each severed link
    individually mutation-visible (restoring ``from exc`` at one site reds this
    test at that site's assert, not at the other's).

    CONTROL (should-fire): the bare ``float()`` call's own formatted chain DOES
    carry the sentinel. The identical assertion shape catches a leak where one
    exists, so a green sweep is evidence of absence rather than a blind sweep.
    """
    import traceback

    #: Planted in the response's SCORE field — the value ``float()`` will name in
    #: its own ValueError. A compromised rerank server controls this field, and
    #: this adapter's whole C12 premise is that response values are corpus-bearing.
    sentinel = "SENTINEL-SCORE-VALUE-9f3a-DO-NOT-ECHO"

    def formatted_chain(exc: BaseException) -> str:
        return "".join(traceback.format_exception(exc))

    # CONTROL: plant the sentinel where it SHOULD appear (float's own error).
    with pytest.raises(ValueError) as control:
        float(sentinel)
    assert sentinel in formatted_chain(control.value), "the sweep cannot see a leak"

    def _client_for(body: object) -> httpx.AsyncClient:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=body)

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    # Site 1 — the vLLM parser (``relevance_score``).
    vllm = VllmReranker(
        "http://rerank.local",
        client=_client_for({"results": [{"index": 0, "relevance_score": sentinel}]}),
    )
    with pytest.raises(RerankResponseError) as vllm_exc:
        asyncio.run(vllm.rerank("q", _chunks(1), top_k=1))
    assert vllm_exc.value.__cause__ is None  # from None: the chain is severed
    assert vllm_exc.value.__suppress_context__
    assert sentinel not in formatted_chain(vllm_exc.value)

    # Site 2 — the TEI parser (``score``). Same defect, same fix, own assert.
    tei = TEIReranker(
        "http://rerank.local",
        client=_client_for([{"index": 0, "score": sentinel}]),
    )
    with pytest.raises(RerankResponseError) as tei_exc:
        asyncio.run(tei.rerank("q", _chunks(1), top_k=1))
    assert tei_exc.value.__cause__ is None  # from None: the chain is severed
    assert tei_exc.value.__suppress_context__
    assert sentinel not in formatted_chain(tei_exc.value)

    # CONTROL: severing the chain must not have cost the messages their content.
    for exc in (vllm_exc.value, tei_exc.value):
        assert "malformed" in str(exc) and "position 0" in str(exc) and "str" in str(exc)


def test_duplicate_result_indices_are_rejected_not_silently_corrupting() -> None:
    """Observed: indices ``[0, 0]`` for inputs ``[c0, c1]`` returned ``c0`` twice
    and dropped ``c1`` — every entry individually valid, the response corrupt."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 0, "relevance_score": 0.9},
                    {"index": 0, "relevance_score": 0.8},
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    reranker = VllmReranker("http://rerank.local", client=client)

    with pytest.raises(RerankResponseError, match="duplicated"):
        asyncio.run(reranker.rerank("q", _chunks(2), top_k=2))


def test_fewer_results_than_documents_is_accepted() -> None:
    """The control for the duplicate guard: cardinality is deliberately NOT
    asserted — a server returning fewer results is degraded, not corrupt — so the
    guard must reject duplicates WITHOUT rejecting a short response."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [{"index": 1, "relevance_score": 0.7}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    reranker = VllmReranker("http://rerank.local", client=client)

    result = asyncio.run(reranker.rerank("q", _chunks(2), top_k=2))

    assert [sc.chunk.chunk_id for sc in result] == ["c1"]


# --- NE-RERANK-CPU: scoring_char_limit parity ------------------------------
#
# The setting is a property of the rerank STAGE, so it must behave IDENTICALLY
# on both adapters — otherwise flipping NE_API_RERANK_BACKEND to the GPU lane
# would silently change what gets scored. Same pairing as the TEI test: a
# truncated view on the wire <-> the full chunk still returned.

_LIMIT_LONG = "alpha beta gamma delta epsilon zeta eta theta " * 40  # ~1760 chars


def _limit_chunks(n: int) -> list[Chunk]:
    return [Chunk(chunk_id=f"c{i}", document_id="d1", text=f"{i} {_LIMIT_LONG}") for i in range(n)]


def test_scoring_char_limit_defaults_to_off_and_posts_the_chunk_verbatim() -> None:
    recorded: list[httpx.Request] = []
    reranker = VllmReranker("http://vllm.local", client=_overlap_client(recorded))
    chunks = _limit_chunks(2)

    asyncio.run(reranker.rerank("alpha", chunks, top_k=2))

    assert _payload(recorded[0])["documents"] == [c.text for c in chunks]


def test_scoring_char_limit_truncates_only_the_text_sent_to_the_server() -> None:
    recorded: list[httpx.Request] = []
    reranker = VllmReranker(
        "http://vllm.local", client=_overlap_client(recorded), scoring_char_limit=600
    )
    chunks = _limit_chunks(2)

    result = asyncio.run(reranker.rerank("alpha", chunks, top_k=2))

    posted = _payload(recorded[0])["documents"]
    assert [len(t) for t in posted] == [600, 600]
    assert posted == [c.text[:600] for c in chunks]
    by_id = {sc.chunk.chunk_id: sc.chunk for sc in result}
    for chunk in chunks:
        assert by_id[chunk.chunk_id] is chunk
        assert len(by_id[chunk.chunk_id].text) > 600


def test_scoring_char_limit_matches_the_tei_adapter_exactly() -> None:
    """Both adapters must put the SAME bytes on the wire for the same setting."""
    tei_recorded: list[httpx.Request] = []
    vllm_recorded: list[httpx.Request] = []
    chunks = _limit_chunks(3)

    async def _tei_handler(request: httpx.Request) -> httpx.Response:
        tei_recorded.append(request)
        return httpx.Response(
            200, json=[{"index": i, "score": 1.0} for i in range(len(_payload(request)["texts"]))]
        )

    tei = TEIReranker(
        "http://tei.local",
        client=httpx.AsyncClient(transport=httpx.MockTransport(_tei_handler)),
        scoring_char_limit=450,
    )
    vllm = VllmReranker(
        "http://vllm.local", client=_overlap_client(vllm_recorded), scoring_char_limit=450
    )
    asyncio.run(tei.rerank("alpha", chunks, top_k=3))
    asyncio.run(vllm.rerank("alpha", chunks, top_k=3))

    assert _payload(tei_recorded[0])["texts"] == _payload(vllm_recorded[0])["documents"]


def test_scoring_char_limit_rejects_a_non_positive_limit_at_construction() -> None:
    for bad in (0, -1):
        with pytest.raises(ValueError, match="scoring_char_limit"):
            VllmReranker("http://vllm.local", scoring_char_limit=bad)
