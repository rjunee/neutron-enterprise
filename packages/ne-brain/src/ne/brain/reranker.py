# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""TEI (Text Embeddings Inference) cross-encoder reranker.

Implements :class:`ne.contracts.knowledge.Reranker` over TEI's HTTP ``/rerank``
endpoint (Apache-2.0 server; consumed as a separate deployed service — no new
Python dependency beyond ``httpx``).

Reranker-model decision (SPEC §10 risk, re-verified 2026-07-04)
==============================================================
The current TEI release (``v1.9.3`` == the dev-stack pin
``ghcr.io/huggingface/text-embeddings-inference:cpu-1.9.3``; NO drift) does
**not** support ``Qwen3-Reranker-0.6B``: ``Qwen3ForSequenceClassification`` is
unsupported and Qwen3-Reranker is a decoder model needing prompt-initialized
scoring. huggingface/text-embeddings-inference issues #643 and #691 are both
OPEN with no maintainer commitment (referenced PR #835 unmerged). This adapter
therefore defaults to the **SPEC-named fallback** ``BAAI/bge-reranker-v2-m3``
(Apache-2.0), which is exactly the model NE-006's dev stack already serves at
``tei-rerank`` on port ``8086``. Revisit trigger: TEI release notes gaining
Qwen3 reranker support.

C3 note: this is **in-VPC inference**, explicitly exempt from ne-gateway
routing per C3, but it is **registry-tagged** — the ModelEntry data (model_id,
provider, license, origin, lane, capabilities) is recorded in the ne-brain
README for NE-011's registry to register at assembly time.

C2 (sovereignty/egress): the config-supplied ``base_url`` is validated at
CONSTRUCTION time (:func:`ne.brain.sovereignty.validate_in_vpc_endpoint` — SSRF
shape + in-VPC/allowlist, fail-closed). A bad or exfil-shaped endpoint raises
:class:`~ne.brain.sovereignty.UnsafeEndpointError` and this reranker never POSTs
a byte of query/chunk text to it.

AIR_GAP and the ``allowed_hosts`` escape hatch
==============================================
``allowed_hosts`` is the ONE way a config can aim these adapters at a host that
is not provably in-VPC. **An engaged air gap dominates that grant completely** —
a public IP literal and a DNS name alike, per the platform rule in
:func:`ne.net.ssrf.allowlist_revoked_by_air_gap` (the one implementation; origin
:mod:`ne.storage.s3`). Provably in-VPC hosts are untouched: an air gap restricts
egress, not the sealed on-box lane. Both adapters enforce it twice, because the
two moments differ:

* at **construction**, through ``validate_in_vpc_endpoint``; and
* before **every request**, because the platform's air gap is a *runtime*
  posture — a governed ``write.internal.network.egress.restrict`` flip engages it
  in a process whose reranker was constructed hours earlier, and the sealed-lane
  claim is only true if the already-built client stops sending. The per-request
  check raises :class:`~ne.net.airgap.AirGapViolationError` BEFORE the POST, so
  zero bytes of query or chunk text leave.

So an engaged air gap makes an external rerank lane fail CLOSED regardless of how
``NE_API_RERANK_ALLOWED_HOSTS`` spells the host — an IP literal and a hostname
are both refused — while the sealed on-box ``tei-rerank`` lane keeps working.

``scoring_char_limit`` — a smaller view for the SCORER, never for the reader
===========================================================================
Both adapters take an optional ``scoring_char_limit``. It truncates the text
sent to the rerank server and NOTHING else: the returned ``ScoredChunk`` always
carries the full, untouched ``Chunk``, so the reader, the citation and the
context budget are byte-identical either way (``_scoring_view`` is the one place
this happens, and the tests pin the full-text return). **Default ``None`` — off,
full text.**

What it gives up: a cross-encoder scores the query against the text it is given,
so a truncated view discards whatever relevance signal lives in the tail of a
chunk. ``BAAI/bge-reranker-v2-m3`` handles 8192 tokens and the live corpus's
chunks are ~239 tokens, so NOTHING is being truncated today — turning this on is
purely a decision to score less than we have.

Why the default is off, measured rather than argued (2026-08-27, the 16-core
demo box, live corpus, **20 candidates**, mean 701 chars/239 tokens per chunk,
``tei-rerank`` at 12 cpus, median of 3-4 reps, ranking compared against the
untruncated run). ⚠️ The pool label on this run used to read "the Ask path's
``retrieval_top_k=5 x candidate_multiplier=4``" and that equation was FALSE when
it was written: ``CurrentSourceAuthorizedKnowledgeRead`` already sat above the
pipeline over-fetching 8x, so the shipped Ask reranked 160, not 20. Post
NE-RERANK-FANOUT the Ask path reranks **40** (window 10 x multiplier 4, held at
``ne.api.platform.RERANK_CANDIDATE_CEILING``); these per-search times are for a
20-candidate pool and scale roughly linearly, so read them as a shape, not as
the Ask path's latency:

===================  ==========  =======================================
scoring_char_limit   per search  ranking vs. full text
===================  ==========  =======================================
``None`` (full)      31.2 s      —
600                  ~24-28 s    top-1 held; top-5 SET held, ORDER changed
450                  21.5 s      top-1 held; top-5 set 4/5
300                  13.2 s      **top-1 CHANGED**
200                  10.3 s      **top-1 CHANGED**
===================  ==========  =======================================

The 600-char setting that was proposed as the demo rescue buys ~10-25% and still
reorders the top 5, because these chunks are ~700 chars to begin with — cutting
to 600 removes about a fifth of the tokens. The settings that are actually fast
enough to matter (300 and below) change which chunk ranks first, i.e. they change
the answer. That is a quality decision an operator may still want to make, per
deployment, with the numbers in front of them — which is why this exists as a
named setting with a table instead of a hardcoded slice — but it is not a default.

Error messages name the FIELD, never the VALUE (C12). vLLM's ``/rerank`` echoes
the submitted ``document`` text back inside each result entry, so a parse error
that interpolated the entry would put corpus text into an exception message and
the application log. Every malformed-response raise is a
:class:`RerankResponseError` carrying the field name, its position in the
response, and the offending value's TYPE — never its contents.

That holds for the whole exception CHAIN, not just ``str(exc)``. The score
parsers convert with ``float(raw_score)``, whose own ``ValueError`` reads
``could not convert string to float: '<value>'`` — the response value verbatim.
A redacted outer message chained ``from exc`` therefore still discloses the
value through ``traceback.format_exception``, which is precisely the text a
``logger.exception`` / Sentry / OTel ``record_exception`` emits. Both parsers
raise ``from None`` and ``test_parse_failure_chain_never_carries_the_response_value``
sweeps the formatted chain (with a should-fire control) rather than only
``str(exc)``.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.parse import urlparse

import httpx

from ne.brain.sovereignty import (
    air_gap_posture as _air_gap_engaged,
)
from ne.brain.sovereignty import (
    assert_air_gap_permits,
    validate_in_vpc_endpoint,
)
from ne.contracts.types import Chunk, ScoredChunk

_DEFAULT_MODEL_ID = "BAAI/bge-reranker-v2-m3"

#: Deadline for the TCP HANDSHAKE ALONE, in seconds.
#:
#: ``httpx.AsyncClient(timeout=x)`` sets ONE scalar for all four phases —
#: connect, read, write and pool — and this lane's scalar is large ON PURPOSE:
#: the on-box CPU reranker legitimately needs tens of seconds (measured in this
#: repo: 78.1 s at 4 cpus, 31.2 s at 12), which is why the demo overlay sets
#: ``NE_TEI_RERANK_TIMEOUT_S=180``. That number is a READ budget, and handing it
#: to the connect phase as well is what made a preempted rented GPU rig
#: catastrophic rather than merely broken: against a black-holed address no
#: SYN is ever answered, so the deadline that actually fired was the kernel's
#: SYN-retry ceiling (``net.ipv4.tcp_syn_retries=6`` on this box, ~130 s) and
#: every search burned ~133 s to learn a fact TCP settles in one round trip.
#:
#: A reachable peer — in-VPC, or the WireGuard link to a rented rig — completes
#: the handshake in single-digit milliseconds, so 5 s is roughly a thousandfold
#: headroom for a live host while giving a dead one a fast, unambiguous verdict.
#: Splitting the phase costs the slow-but-alive CPU lane nothing: its cost is
#: entirely in the read, which keeps the operator's full budget.
#:
#: DELIBERATELY NOT CONFIGURABLE. Connect latency is a property of the network
#: path, not of the model being served, so no deployment needs its own value —
#: whereas an env knob here is precisely a way to reintroduce this defect by
#: setting it back to the read budget. The read timeout stays configurable
#: because the served model really does change how long an answer takes.
_CONNECT_TIMEOUT_S = 5.0


def _client_timeout(read_timeout: float) -> httpx.Timeout:
    """Per-phase deadlines: the operator's budget everywhere except connect.

    Read/write/pool keep ``read_timeout`` verbatim — see ``_CONNECT_TIMEOUT_S``
    for why only the connect phase is bounded separately.
    """
    return httpx.Timeout(read_timeout, connect=_CONNECT_TIMEOUT_S)


def _endpoint_label(base_url: str) -> str:
    """``scheme://host:port`` for an error message — host and port ONLY.

    Rebuilt from the PARSED url instead of echoing ``base_url``, so userinfo, a
    path or a query string structurally cannot ride along into a log line: a
    credential must never be the price of naming a dead host.
    """
    parsed = urlparse(base_url)
    host = parsed.hostname or "?"
    port = parsed.port
    return f"{parsed.scheme}://{host}:{port}" if port else f"{parsed.scheme}://{host}"


class RerankBackendUnavailableError(httpx.TransportError):
    """A rerank backend could not be reached, and this error NAMES it.

    Raised in place of the bare ``httpx`` transport error, whose message names
    the failure mode (``ConnectTimeout``, ``[Errno 111] Connection refused``)
    but never the endpoint. Three dead connects that say only "ConnectError"
    cost an operator the whole diagnosis — the host that died is the single
    fact the traceback was missing, and the underlying class is preserved in
    the message so the failure mode is not traded away for the address.

    A ``httpx.TransportError`` subclass, so ne-api's ``_BACKEND_ERRORS`` maps it
    to the same 502 as before. That handler renders ``type(exc).__name__`` only,
    so the endpoint reaches the server's log and NEVER the HTTP response body.
    """


def _unreachable(
    exc: httpx.TransportError, base_url: str, lane: str
) -> RerankBackendUnavailableError:
    try:
        request = exc.request
    except RuntimeError:  # httpx raises when the transport never set one
        request = None
    return RerankBackendUnavailableError(
        f"{lane} backend {_endpoint_label(base_url)} unreachable: {type(exc).__name__}: {exc}",
        request=request,
    )


def _scoring_view(chunks: Sequence[Chunk], scoring_char_limit: int | None) -> list[str]:
    """The text the SERVER scores. ``None`` (default) ⇒ the chunk verbatim.

    This is the one place either adapter decides what a cross-encoder sees, and
    it is deliberately separate from what ``rerank`` RETURNS: the ``ScoredChunk``
    always carries the untouched :class:`~ne.contracts.types.Chunk`, so a
    truncated scoring view can never reach the reader, the citation, or the
    context budget as a shortened chunk. See ``scoring_char_limit`` on either
    adapter for what the trade actually buys, measured.
    """
    if scoring_char_limit is None:
        return [c.text for c in chunks]
    return [c.text[:scoring_char_limit] for c in chunks]


def _validate_scoring_char_limit(scoring_char_limit: int | None) -> int | None:
    if scoring_char_limit is not None and scoring_char_limit < 1:
        # Names the FIELD, never a corpus value (C12) — this one is operator config.
        raise ValueError(f"scoring_char_limit must be >= 1 or None, got {scoring_char_limit}")
    return scoring_char_limit


class RerankResponseError(ValueError):
    """A rerank server returned a response this adapter refuses to interpret.

    A ``ValueError`` subclass so the historical ``raise ValueError(...)``
    contract (and every ``pytest.raises(ValueError)`` against it) is preserved
    exactly, and a distinct type so ne-api can map it to the field-only 502
    alongside the other backend faults instead of letting it escape as an opaque
    500 (``ne.api.errors._BACKEND_ERRORS``).

    Its message NEVER contains a response value — only the field name, the entry
    position, and the offending value's type.
    """


class TEIReranker:
    """Cross-encoder reranker client for a TEI server. Implements ``Reranker``."""

    def __init__(
        self,
        base_url: str,
        *,
        model_id: str = _DEFAULT_MODEL_ID,
        client: httpx.AsyncClient | None = None,
        timeout: float = 30.0,
        batch_size: int = 32,
        scoring_char_limit: int | None = None,
        allowed_hosts: Sequence[str] = (),
        air_gap: Callable[[], bool] | None = None,
    ) -> None:
        # C2: validate the config-supplied endpoint at construction (SSRF +
        # in-VPC/allowlist, fail-closed). A bad endpoint raises UnsafeEndpointError
        # and never receives a byte of query/chunk text. An engaged AIR_GAP revokes
        # an `allowed_hosts` grant entirely (IP literal or DNS name), here and
        # again before every request in rerank() (the module docstring says why).
        self._base_url = validate_in_vpc_endpoint(
            base_url, allowed_hosts=allowed_hosts, air_gap=_air_gap_engaged(air_gap)
        )
        self._model_id = model_id
        self._timeout = timeout
        self._batch_size = batch_size
        self._scoring_char_limit = _validate_scoring_char_limit(scoring_char_limit)
        self._air_gap = air_gap
        self._owns_client = client is None
        self._client = client

    @property
    def model_id(self) -> str:
        return self._model_id

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=_client_timeout(self._timeout))
        return self._client

    async def rerank(
        self, query: str, chunks: Sequence[Chunk], *, top_k: int = 10
    ) -> Sequence[ScoredChunk]:
        items = list(chunks)
        if not items:
            return ()
        client = self._get_client()
        pairs: list[tuple[int, float]] = []
        for start in range(0, len(items), self._batch_size):
            # INSIDE the loop: see TEIEmbedder.embed — one rerank is several batches
            # on a CPU lane, and a flip between them must stop the remainder.
            assert_air_gap_permits(self._base_url, self._air_gap, what="query/chunk text")
            batch = items[start : start + self._batch_size]
            try:
                response = await client.post(
                    f"{self._base_url}/rerank",
                    json={
                        "query": query,
                        "texts": _scoring_view(batch, self._scoring_char_limit),
                        "raw_scores": False,
                    },
                )
            except httpx.TransportError as exc:
                raise _unreachable(exc, self._base_url, "rerank (TEI)") from exc
            response.raise_for_status()
            entries = response.json()
            if not isinstance(entries, list):
                raise RerankResponseError(
                    "malformed TEI /rerank response: expected a list of entries, got "
                    f"{type(entries).__name__}"
                )
            batch_pairs = [
                _parse_entry(entry, len(batch), position) for position, entry in enumerate(entries)
            ]
            _reject_duplicate_indices(batch_pairs, "TEI")
            pairs.extend((start + index, score) for index, score in batch_pairs)
        # Never trust TEI's response ordering — re-sort client-side. Descending
        # by score; ties broken by ascending original index (stable/deterministic).
        pairs.sort(key=lambda pair: (-pair[1], pair[0]))
        return tuple(ScoredChunk(chunk=items[i], score=score) for i, score in pairs[:top_k])

    async def aclose(self) -> None:
        """Close the owned client. A no-op when the client was injected."""
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None


def _reject_duplicate_indices(pairs: Sequence[tuple[int, float]], backend: str) -> None:
    """Reject a response that scores the same input document twice.

    Each entry is individually well-formed in that case, but the RESPONSE is not:
    a duplicated ``index`` silently drops one input chunk from the result set and
    duplicates another (observed: indices ``[0, 0]`` for inputs ``[c0, c1]``
    returned ``c0`` twice and never returned ``c1``). Cardinality is deliberately
    NOT asserted — a server returning fewer results than documents is degraded,
    not corrupt — but a duplicate is unambiguously corrupt. Names the count, never
    a value.
    """
    indices = [index for index, _ in pairs]
    if len(set(indices)) != len(indices):
        raise RerankResponseError(
            f"malformed {backend} /rerank response: 'index' is duplicated across "
            f"results ({len(indices)} results, {len(set(indices))} distinct indices); "
            "a duplicate would silently drop an input chunk from the result set"
        )


def _coerce_score(raw_score: Any, backend: str, field: str, position: int) -> float:
    """Convert a rerank score to a FINITE float, or raise the redacted C12 error.

    ONE implementation for both parsers, because the two rules it enforces were
    each missed at one site or the other:

    * ``from None``, never ``from exc``. ``float('<value>')`` raises
      ``ValueError: could not convert string to float: '<value>'`` — the untrusted
      response value verbatim — and a chained cause re-exports it through
      ``traceback.format_exception``, which is what ``logger.exception`` / Sentry /
      OTel ``record_exception`` ship. The redacted outer message does not close
      that on its own.
    * **finite**, not merely float-able. ``float()`` happily accepts ``"NaN"``,
      ``"Infinity"`` and ``"-inf"``, and either value breaks this adapter's
      documented contract. NaN compares unordered, so ``pairs.sort`` no longer
      totally orders the results and the FINAL RANKING becomes dependent on the
      server's response order — the exact thing "never trust the server's
      response ordering" promises it is not. (Observed: scores 0.8/0.7 plus one
      NaN yield ``[c0, c1, c2]`` or ``[c1, c0, c2]`` purely from response order.)
      An infinity dominates every legitimate score, and a NaN reaching the API
      boundary is not JSON-serializable under ``allow_nan=False``.

    Names the field, its position, and the value's TYPE — never its contents.
    """
    try:
        score = float(raw_score)
    except (TypeError, ValueError):
        raise RerankResponseError(
            f"malformed {backend} /rerank response: {field!r} at position {position} is "
            f"not float-able (got {type(raw_score).__name__})"
        ) from None
    if not math.isfinite(score):
        # The value is a float here, so naming it would disclose only "nan"/"inf"
        # — but stay uniform with every other message and name the TYPE only.
        raise RerankResponseError(
            f"malformed {backend} /rerank response: {field!r} at position {position} is "
            f"not finite (got {type(raw_score).__name__}); a non-finite score makes the "
            "result ranking depend on the server's response order"
        )
    return score


def _parse_entry(entry: Any, batch_len: int, position: int) -> tuple[int, float]:
    """Validate one TEI /rerank entry -> (index, score); fail loud on malformed.

    C12: every message names the FIELD and the offending value's TYPE, never the
    value — see the module docstring.
    """
    if not isinstance(entry, Mapping):
        raise RerankResponseError(
            f"malformed TEI /rerank response: result at position {position} is "
            f"{type(entry).__name__}, expected a mapping"
        )
    index = entry.get("index")
    if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < batch_len:
        raise RerankResponseError(
            f"malformed TEI /rerank response: 'index' at position {position} is not an "
            f"int in range(0,{batch_len}) (got {type(index).__name__})"
        )
    return index, _coerce_score(entry.get("score"), "TEI", "score", position)


class VllmReranker:
    """Cross-encoder reranker client for a vLLM ``/rerank`` server (NE-SEARCH-LATENCY).

    Implements ``ne.contracts.knowledge.Reranker`` over vLLM's Jina-compatible
    ``POST /rerank`` endpoint (``{"model", "query", "documents"}`` →
    ``{"results": [{"index", "relevance_score"}, ...]}`` — live-verified against
    the pinned serve image ``vllm/vllm-openai:v0.24.0`` serving
    ``BAAI/bge-reranker-v2-m3``).

    Why this adapter exists: the NE-SEARCH-LATENCY attribution measured the CPU
    TEI rerank stage at ~1s per query/chunk pair on the demo box's real ~600-char
    chunks — 40 candidates ⇒ ~40s of a ~40s ``/v1/brain/search`` (the graph tier
    measured 0.02s). The SAME model served by vLLM on a GPU scored the SAME 40
    pairs in ~0.12s, and in that one side-by-side run (demo rig, 2026-08-04) the
    top-10 ordering was identical. This adapter changes the SERVER, never the
    model. What CI holds is narrower than that measurement and is stated where it
    belongs: ``test_reranker_parity.py`` pins that both adapters, given equivalent
    backend responses, produce identical ordering, tie-break and ``top_k``. Score
    agreement between two servers is a property of the servers, not of this code,
    and is re-measured — not assumed — whenever the lane is stood up.

    Unlike :class:`TEIReranker` there is NO client-side batch loop: vLLM has no
    TEI-style ``max_client_batch_size`` request cap and schedules internally, so
    ONE request carries every candidate (the sequential-batch cost structure is
    gone by construction, not tuned away). ``test_vllm_reranker.py`` pins the
    one-request property.

    C2 (sovereignty/egress): the config-supplied ``base_url`` is validated at
    CONSTRUCTION time (:func:`ne.brain.sovereignty.validate_in_vpc_endpoint` —
    SSRF shape + in-VPC/allowlist, fail-closed) and the air-gap rule is
    re-asserted before EVERY request (module docstring). An external GPU rerank
    host is a per-deployment, explicitly-allowlisted decision, never a default,
    and an engaged air gap revokes that grant entirely — however it is spelled.

    **Operational trap — there is NO automatic fallback.** A rerank failure is a
    HARD failure of ``/v1/brain/search``: :class:`ne.brain.pipeline.
    RetrievalPipeline` holds exactly one ``Reranker`` and does not catch, retry,
    or degrade. If the GPU rig dies — and a rented preemptible box dies without
    notice — every search raises, after up to ``NE_TEI_RERANK_TIMEOUT_S`` (the
    demo overlay sets **180 s**), and surfaces as a 502. This is deliberate: the
    only degradation available in that composition is *no rerank at all*, i.e.
    silently returning fused-order candidates as if they had been scored, and a
    quiet quality downgrade of the retrieval stage is worse than a loud failure.
    The remedy is a config flip, not a code path: set ``NE_API_RERANK_BACKEND=tei``
    (or re-run ``ne demo up`` WITHOUT ``--gpu-rerank``) and restart ne-api to
    return to the sealed on-box CPU lane.

    **Cleartext, unauthenticated egress.** The demo rig lane runs over plain
    ``http://`` to a vLLM server started with no ``--api-key``: the in-VPC
    validator deliberately runs ``require_https=False``, so query text and chunk
    text cross the public internet in the clear, to an endpoint anyone who learns
    the address can call. That is acceptable ONLY for the synthetic demo corpus it
    is documented for. A real corpus needs TLS and an authenticated endpoint
    before this backend is pointed anywhere off-box.

    C3 note: in-VPC (or explicitly allowlisted) inference, exempt from ne-gateway
    routing per C3 but registry-tagged — see the ne-brain README's reranker
    ModelEntry table (``provider`` = ``vllm`` for this adapter).
    """

    def __init__(
        self,
        base_url: str,
        *,
        model_id: str = _DEFAULT_MODEL_ID,
        served_model_name: str = "rerank",
        client: httpx.AsyncClient | None = None,
        timeout: float = 30.0,
        scoring_char_limit: int | None = None,
        allowed_hosts: Sequence[str] = (),
        air_gap: Callable[[], bool] | None = None,
    ) -> None:
        # C2: identical construction-time validation to TEIReranker — a bad or
        # exfil-shaped endpoint raises UnsafeEndpointError and never receives a
        # byte of query/chunk text, and an engaged air gap dominates an allowlisted
        # public IP literal here AND again before every request.
        self._base_url = validate_in_vpc_endpoint(
            base_url, allowed_hosts=allowed_hosts, air_gap=_air_gap_engaged(air_gap)
        )
        self._model_id = model_id
        self._served_model_name = served_model_name
        self._timeout = timeout
        self._scoring_char_limit = _validate_scoring_char_limit(scoring_char_limit)
        self._air_gap = air_gap
        self._owns_client = client is None
        self._client = client

    @property
    def model_id(self) -> str:
        return self._model_id

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=_client_timeout(self._timeout))
        return self._client

    async def rerank(
        self, query: str, chunks: Sequence[Chunk], *, top_k: int = 10
    ) -> Sequence[ScoredChunk]:
        items = list(chunks)
        if not items:
            return ()
        assert_air_gap_permits(self._base_url, self._air_gap, what="query/chunk text")
        client = self._get_client()
        try:
            response = await client.post(
                f"{self._base_url}/rerank",
                json={
                    "model": self._served_model_name,
                    "query": query,
                    "documents": _scoring_view(items, self._scoring_char_limit),
                },
            )
        except httpx.TransportError as exc:
            raise _unreachable(exc, self._base_url, "rerank (vLLM)") from exc
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, Mapping) or not isinstance(body.get("results"), list):
            # C12: name the SHAPE, never the body — vLLM echoes the submitted
            # documents back, so `{body!r}` here would log the corpus.
            raise RerankResponseError(
                "malformed vLLM /rerank response: expected a mapping with a 'results' "
                f"list, got {type(body).__name__}"
            )
        pairs = [
            _parse_vllm_result(entry, len(items), position)
            for position, entry in enumerate(body["results"])
        ]
        _reject_duplicate_indices(pairs, "vLLM")
        # Never trust the server's response ordering — re-sort client-side.
        # Descending by score; ties broken by ascending original index
        # (stable/deterministic, identical to TEIReranker).
        pairs.sort(key=lambda pair: (-pair[1], pair[0]))
        return tuple(ScoredChunk(chunk=items[i], score=score) for i, score in pairs[:top_k])

    async def aclose(self) -> None:
        """Close the owned client. A no-op when the client was injected."""
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None


def _parse_vllm_result(entry: Any, n_documents: int, position: int) -> tuple[int, float]:
    """Validate one vLLM /rerank result -> (index, score); fail loud on malformed.

    C12, and load-bearing here specifically: a vLLM ``/rerank`` result entry
    carries a ``document`` key holding the SUBMITTED CHUNK TEXT back verbatim.
    Interpolating ``entry`` into any of these messages puts customer corpus text
    into an exception message and the application log. Every message below names
    the field, its position, and the offending value's TYPE — never its contents.
    """
    if not isinstance(entry, Mapping):
        raise RerankResponseError(
            f"malformed vLLM /rerank response: result at position {position} is "
            f"{type(entry).__name__}, expected a mapping"
        )
    index = entry.get("index")
    if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < n_documents:
        raise RerankResponseError(
            f"malformed vLLM /rerank response: 'index' at position {position} is not an "
            f"int in range(0,{n_documents}) (got {type(index).__name__})"
        )
    return index, _coerce_score(entry.get("relevance_score"), "vLLM", "relevance_score", position)
