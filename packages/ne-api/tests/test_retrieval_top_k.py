# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-CTXB-6 — configurable retrieval top_k + honest '[showing M of N]' header.

Direct-binding tests exercise :func:`build_chat_bindings` over a small recording
fake ``KnowledgeRead`` (the ``_RecordingRetriever`` shape mirrors ne-brain's
``test_retrieval_pipeline.py``), driving the handler closure straight — the
turn-principal ``ContextVar`` is set/reset exactly like the chat route does.
The settings tests exercise :class:`ApiSettings.retrieval_top_k` in isolation.
The end-to-end test drives a full governed turn through :fixture:`make_app` to
prove settings -> platform -> binding -> pipeline-rerank -> header in one path.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any

import pytest
from pydantic import ValidationError

from ne.api.settings import ApiSettings
from ne.api.tools import TOOL_NAME, TURN_PRINCIPAL, build_chat_bindings
from ne.api.workflows import parse_retrieved_text
from ne.brain.pipeline import RerankedRetrievalResult
from ne.brain.withholding import record_source_health_withholding
from ne.contracts.types import Chunk, Principal, RetrievalResult, ScoredChunk

from .conftest import AppBuilder, chunk, run, text_completion, tool_call_completion

_PRINCIPAL = Principal(id="u1", groups=("eng",))


class _RecordingRetriever:
    """Fake KnowledgeRead: records call kwargs, returns a canned result."""

    def __init__(self, result: RetrievalResult) -> None:
        self._result = result
        self.search_calls: list[dict[str, Any]] = []

    async def search(
        self,
        query: str,
        *,
        principal: Principal,
        top_k: int = 10,
        filters: Mapping[str, Any] | None = None,
    ) -> RetrievalResult:
        self.search_calls.append(
            {"query": query, "principal": principal, "top_k": top_k, "filters": filters}
        )
        return self._result

    async def retrieve(self, chunk_ids: Sequence[str], *, principal: Principal) -> RetrievalResult:
        raise NotImplementedError


def _sc(cid: str, text: str = "text") -> ScoredChunk:
    return ScoredChunk(
        chunk=Chunk(chunk_id=cid, document_id=f"doc-{cid}", text=text, metadata={}),
        score=1.0,
    )


async def _call_search(retriever: Any, *, top_k: int | None = None) -> str:
    bindings = (
        build_chat_bindings(retriever, top_k=top_k, extraction_mode="ingest_review")
        if top_k is not None
        else build_chat_bindings(retriever, extraction_mode="ingest_review")
    )
    (binding,) = bindings
    token = TURN_PRINCIPAL.set(_PRINCIPAL)
    try:
        return await binding.handler({"query": "q"})
    finally:
        TURN_PRINCIPAL.reset(token)


# --- ApiSettings.retrieval_top_k --------------------------------------------


def test_settings_default_top_k_is_five() -> None:
    settings = ApiSettings(
        qdrant_url="http://qdrant.test", qdrant_collection="t", tei_url="http://tei.test"
    )
    assert settings.retrieval_top_k == 5


def test_settings_top_k_rejects_zero() -> None:
    with pytest.raises(ValidationError):
        ApiSettings(
            qdrant_url="http://qdrant.test",
            qdrant_collection="t",
            tei_url="http://tei.test",
            retrieval_top_k=0,
        )


def test_settings_top_k_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NE_API_RETRIEVAL_TOP_K", "9")
    settings = ApiSettings(
        qdrant_url="http://qdrant.test", qdrant_collection="t", tei_url="http://tei.test"
    )
    assert settings.retrieval_top_k == 9


# --- build_chat_bindings threading -------------------------------------------


def test_binding_passes_configured_top_k_to_retriever() -> None:
    retriever = _RecordingRetriever(RetrievalResult(chunks=(), citations=()))

    asyncio.run(_call_search(retriever, top_k=7))

    assert retriever.search_calls[0]["top_k"] == 7


def test_binding_default_top_k_is_five() -> None:
    retriever = _RecordingRetriever(RetrievalResult(chunks=(), citations=()))

    asyncio.run(_call_search(retriever))

    assert retriever.search_calls[0]["top_k"] == 5


# --- header rendering ---------------------------------------------------------


def test_render_header_uses_candidate_count_when_present() -> None:
    result = RerankedRetrievalResult(chunks=(_sc("a"), _sc("b")), citations=(), candidate_count=9)
    retriever = _RecordingRetriever(result)

    rendered = asyncio.run(_call_search(retriever, top_k=2))
    lines = rendered.split("\n")

    assert lines[0] == "[showing 2 of 9 retrieved chunks]"
    assert lines[1] == "a: text"
    assert lines[2] == "b: text"


def test_render_header_falls_back_to_shown_count() -> None:
    result = RetrievalResult(chunks=(_sc("a"), _sc("b"), _sc("c")), citations=())
    retriever = _RecordingRetriever(result)

    rendered = asyncio.run(_call_search(retriever, top_k=3))
    lines = rendered.split("\n")

    assert lines[0] == "[showing 3 of 3 retrieved chunks]"
    assert len(lines) == 4


def test_render_header_empty_result() -> None:
    retriever = _RecordingRetriever(RetrievalResult(chunks=(), citations=()))

    rendered = asyncio.run(_call_search(retriever))

    assert rendered == "[showing 0 of 0 retrieved chunks]"


# --- end-to-end threading: settings -> platform -> binding -> pipeline ------


def test_settings_top_k_threads_end_to_end(make_app: AppBuilder) -> None:
    h = make_app(
        retrieval_top_k=2,
        completions=[
            tool_call_completion("read.knowledge.search", {"query": "q"}),
            text_completion("answer"),
        ],
        chunks=[chunk(f"c{i}", acl=["eng"], text=f"body{i}") for i in range(5)],
    )

    async def _t() -> None:
        headers = h.bearer("user:ada", groups=["eng"], department="engineering")
        async with h.client() as client:
            created = await client.post(
                "/api/v1/chat/sessions", json={"agent_name": "console-chat"}, headers=headers
            )
            assert created.status_code == 200
            sid = created.json()["session_id"]
            r = await client.post(
                f"/api/v1/chat/sessions/{sid}/messages", json={"prompt": "hi"}, headers=headers
            )
            assert r.status_code == 200

    run(_t())

    tool_messages = [m for req in h.gateway.requests for m in req.messages if m.role == "tool"]
    assert len(tool_messages) == 1
    lines = tool_messages[0].content.split("\n")
    # ne.runtime.model wraps every ToolReturnPart as "{tool_name} returned: {content}"
    # (ne/runtime/model.py:162) — the header is the tail of that first line.
    # Current-source authorization deliberately exposes only the authorized page,
    # never the size of the pre-authorization candidate set.
    assert lines[0] == f"{TOOL_NAME} returned: [showing 2 of 2 retrieved chunks]"
    assert len(lines) == 3


# --- source-health withholding is distinguishable from an empty corpus --------


class _WithholdingRetriever(_RecordingRetriever):
    """A retriever that withholds for connector health, as HybridRetriever does.

    It records into the ambient observation exactly where
    ``HybridRetriever._filter_source_health`` does, so the handler under test
    sees the real production shape rather than a hand-built header string.
    """

    def __init__(self, result: RetrievalResult, *, candidates: int, withheld: int) -> None:
        super().__init__(result)
        self._candidates = candidates
        self._withheld = withheld

    async def search(self, query: str, **kwargs: Any) -> RetrievalResult:
        record_source_health_withholding(candidates=self._candidates, withheld=self._withheld)
        return await super().search(query, **kwargs)


def test_a_total_withholding_does_not_render_as_an_empty_corpus() -> None:
    """The header must say the matches were WITHHELD, not that there were none.

    This is the Run this test exists for: the Agent was scoped to one governed
    source, that source's connector was degraded, every candidate was withheld,
    and "[showing 0 of 0 retrieved chunks]" sent the model off broadening a query
    that was never the problem.
    """
    retriever = _WithholdingRetriever(
        RetrievalResult(chunks=(), citations=()), candidates=52, withheld=52
    )

    rendered = asyncio.run(_call_search(retriever))

    assert rendered.startswith("[showing 0 of 52 retrieved chunks; 52 withheld")
    assert "source-health outage" in rendered
    # It must never read as an empty corpus, which is what sent the model broadening.
    assert "[showing 0 of 0 retrieved chunks]" not in rendered
    # And the envelope stays machine-readable for the workflow record.
    parsed = parse_retrieved_text(rendered)
    assert (parsed.shown, parsed.candidates, parsed.withheld) == (0, 52, 52)
    assert parsed.source_health_blackout is True


def test_a_partial_withholding_is_disclosed_without_hiding_what_was_served() -> None:
    result = RerankedRetrievalResult(chunks=(_sc("a"),), citations=(), candidate_count=1)
    retriever = _WithholdingRetriever(result, candidates=3, withheld=2)

    rendered = asyncio.run(_call_search(retriever, top_k=5))
    lines = rendered.split("\n")

    assert lines[0].startswith("[showing 1 of 3 retrieved chunks; 2 withheld")
    assert lines[1] == "a: text"
    parsed = parse_retrieved_text(rendered)
    assert (parsed.shown, parsed.candidates, parsed.withheld) == (1, 3, 2)
    # Something WAS served, so this is not a blackout — the distinction the
    # no-progress rule's caller needs.
    assert parsed.source_health_blackout is False


def test_a_healthy_empty_corpus_still_renders_the_plain_empty_header() -> None:
    """Nothing withheld means the honest answer really is "nothing matched"."""
    retriever = _WithholdingRetriever(
        RetrievalResult(chunks=(), citations=()), candidates=0, withheld=0
    )

    rendered = asyncio.run(_call_search(retriever))

    assert rendered == "[showing 0 of 0 retrieved chunks]"


def test_one_search_never_inherits_the_previous_search_withholding() -> None:
    """Each call starts a FRESH observation; a stale blackout must not colour a clean read."""
    blackout = _WithholdingRetriever(
        RetrievalResult(chunks=(), citations=()), candidates=4, withheld=4
    )
    clean = _RecordingRetriever(RetrievalResult(chunks=(), citations=()))

    async def _both() -> tuple[str, str]:
        first = await _call_search(blackout)
        second = await _call_search(clean)
        return first, second

    first, second = asyncio.run(_both())

    assert first.startswith("[showing 0 of 4 retrieved chunks; 4 withheld")
    assert second == "[showing 0 of 0 retrieved chunks]"
