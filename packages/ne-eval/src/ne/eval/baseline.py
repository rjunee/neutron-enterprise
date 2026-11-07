# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Deterministic baseline components for the CI retrieval threshold proof (NE-025).

These are REAL implementations of the pluggable ne-eval/ne-contracts seams —
:class:`~ne.eval.judge.Judge`, :class:`ne.contracts.knowledge.Reranker`, and
:class:`~ne.eval.suite.EvalTarget` — with fully deterministic, purely lexical
behaviour and NO model, network, or randomness. They let the Checkpoint-C
full-pipeline threshold proof run green in CI without a live TEI/vLLM/remote
server (the live counterparts are LIVE-PARKED; see the README table).

* :class:`LexicalJudge` — a calibrated heuristic judge (family ``heuristic-lexical``)
  that scores the RAGAS-methodology rubrics by content-token containment. It is
  the CI stand-in for the real cross-family gateway judge (NE-013 vLLM), and it
  passes the fail-closed calibration gate against committed human labels.
* :class:`LexicalReranker` — an IDF-weighted lexical cross-encoder stand-in
  satisfying the FROZEN ``ne.contracts.knowledge.Reranker`` Protocol.
* :class:`KnowledgeReadEvalTarget` — adapts a ``KnowledgeRead`` (the retrieval
  pipeline) plus an extractive answerer to the suite's ``EvalTarget`` seam.

This module imports ONLY stdlib + pydantic + ne-contracts + ne-eval — never
``ne.brain`` and never a provider SDK (C3/C5 stay green).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from math import log

from pydantic import BaseModel

from ne.contracts.types import Chunk, Principal, RetrievalResult, ScoredChunk
from ne.eval.errors import JudgeOutputError
from ne.eval.judge import (
    PrecisionOut,
    RecallClassificationItem,
    RecallClassificationsOut,
    StatementsOut,
    VerdictItem,
    VerdictsOut,
)
from ne.eval.rubrics import Rubric

#: Fraction of a statement's content tokens that must appear in a context for the
#: judge to call it supported/attributed (faithfulness + context_recall).
_SUPPORT_THRESHOLD = 0.6

#: Fraction of a reference answer's content tokens that must appear in a context
#: for the judge to call that context useful (context_precision).
_PRECISION_THRESHOLD = 0.8

#: Separator the metrics use to join contexts into one blob (mirrors ne.eval.metrics).
_CONTEXT_SEP = "\n---\n"

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_TOKEN_RE = re.compile(r"[a-z0-9]+")

#: Small English stopword set — high-frequency function words carry no lexical signal.
_STOPWORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "but",
        "by",
        "did",
        "do",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "he",
        "her",
        "his",
        "how",
        "in",
        "into",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "that",
        "the",
        "their",
        "them",
        "there",
        "these",
        "they",
        "this",
        "to",
        "was",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "whom",
        "why",
        "will",
        "with",
        "would",
    }
)


def _content_tokens(text: str) -> list[str]:
    """Casefold ``text``, extract ``[a-z0-9]+`` tokens, drop stopwords (order kept)."""
    return [tok for tok in _TOKEN_RE.findall(text.casefold()) if tok not in _STOPWORDS]


def _split_sentences(text: str) -> list[str]:
    """Split ``text`` into sentences on ``.!?`` boundaries (mirrors the chunker regex)."""
    stripped = text.strip()
    if not stripped:
        return []
    parts = [s.strip() for s in _SENTENCE_SPLIT_RE.split(stripped)]
    return [s for s in parts if s]


def _containment(tokens: Sequence[str], haystack_tokens: set[str]) -> float:
    """Fraction of ``tokens`` present in ``haystack_tokens`` (1.0 for empty tokens)."""
    if not tokens:
        return 1.0
    matched = sum(1 for tok in tokens if tok in haystack_tokens)
    return matched / len(tokens)


class LexicalJudge:
    """Deterministic heuristic :class:`~ne.eval.judge.Judge` for the CI proof.

    Dispatches on ``rubric.id`` and scores by content-token containment; raises
    :class:`~ne.eval.errors.JudgeOutputError` (fail-closed) for an unknown rubric.
    Cross-family against the ``extractive-baseline`` system under test.
    """

    judge_model_id = "ne-lexical-judge-v1"
    judge_family = "heuristic-lexical"

    async def judge(
        self,
        rubric: Rubric,
        variables: Mapping[str, str],
        out_model: type[BaseModel],
    ) -> BaseModel:
        if rubric.id == "faithfulness_statements":
            return self._statements(variables)
        if rubric.id == "faithfulness_verdicts":
            return self._verdicts(variables)
        if rubric.id == "context_precision":
            return self._precision(variables)
        if rubric.id == "context_recall":
            return self._recall(variables)
        raise JudgeOutputError(f"LexicalJudge has no scoring for rubric {rubric.id!r}")

    def _statements(self, variables: Mapping[str, str]) -> StatementsOut:
        return StatementsOut(statements=tuple(_split_sentences(variables.get("answer", ""))))

    def _verdicts(self, variables: Mapping[str, str]) -> VerdictsOut:
        statements: list[str] = list(json.loads(variables["statements"]))
        contexts = variables["contexts"].split(_CONTEXT_SEP)
        context_token_sets = [set(_content_tokens(ctx)) for ctx in contexts]
        items: list[VerdictItem] = []
        for statement in statements:
            tokens = _content_tokens(statement)
            supported = any(
                _containment(tokens, ctx_tokens) >= _SUPPORT_THRESHOLD
                for ctx_tokens in context_token_sets
            )
            items.append(VerdictItem(statement=statement, verdict=1 if supported else 0))
        return VerdictsOut(verdicts=tuple(items))

    def _precision(self, variables: Mapping[str, str]) -> PrecisionOut:
        tokens = _content_tokens(variables["reference_answer"])
        ctx_tokens = set(_content_tokens(variables["contexts"]))
        useful = _containment(tokens, ctx_tokens) >= _PRECISION_THRESHOLD
        return PrecisionOut(verdict=1 if useful else 0)

    def _recall(self, variables: Mapping[str, str]) -> RecallClassificationsOut:
        statements: list[str] = list(json.loads(variables["statements"]))
        joined_tokens = set(_content_tokens(variables["contexts"]))
        items: list[RecallClassificationItem] = []
        for statement in statements:
            tokens = _content_tokens(statement)
            attributed = _containment(tokens, joined_tokens) >= _SUPPORT_THRESHOLD
            items.append(
                RecallClassificationItem(statement=statement, attributed=1 if attributed else 0)
            )
        return RecallClassificationsOut(classifications=tuple(items))


class LexicalReranker:
    """IDF-weighted lexical reranker satisfying ``ne.contracts.knowledge.Reranker``.

    Scores each candidate chunk by the summed IDF of the query's content tokens
    that occur in the chunk, with IDF computed over the PASSED candidate list
    (``ln((N+1)/(df+1)) + 1``). Deterministic: ties break by ascending chunk id.
    """

    model_id = "ne-lexical-reranker-v1"

    async def rerank(
        self, query: str, chunks: Sequence[Chunk], *, top_k: int = 10
    ) -> Sequence[ScoredChunk]:
        items = list(chunks)
        if not items:
            return ()
        query_tokens = list(dict.fromkeys(_content_tokens(query)))
        chunk_token_sets = [set(_content_tokens(c.text)) for c in items]
        n = len(items)
        idf: dict[str, float] = {}
        for token in query_tokens:
            df = sum(1 for token_set in chunk_token_sets if token in token_set)
            idf[token] = log((n + 1) / (df + 1)) + 1.0
        scored: list[ScoredChunk] = []
        for chunk, token_set in zip(items, chunk_token_sets, strict=True):
            score = sum(idf[token] for token in query_tokens if token in token_set)
            scored.append(ScoredChunk(chunk=chunk, score=score))
        scored.sort(key=lambda sc: (-sc.score, sc.chunk.chunk_id))
        return tuple(scored[:top_k])


class KnowledgeReadEvalTarget:
    """Adapts a ``KnowledgeRead`` + extractive answerer to the suite ``EvalTarget`` seam.

    ``retrieve`` delegates to the pipeline's principal-scoped ``search``; ``answer``
    is extractive — it returns the first (<=2 sentences, <=400 chars) of whichever
    retrieved context shares the most query content tokens (``''`` when empty). No
    generation, no randomness — a deterministic floor system under test.
    """

    _ANSWER_CHAR_CAP = 400
    _ANSWER_MAX_SENTENCES = 2

    def __init__(
        self,
        knowledge_read: object,
        principal: Principal,
        top_k: int = 10,
    ) -> None:
        self._knowledge_read = knowledge_read
        self._principal = principal
        self._top_k = top_k

    async def retrieve(self, question: str) -> RetrievalResult:
        search = self._knowledge_read.search  # type: ignore[attr-defined]
        return await search(question, principal=self._principal, top_k=self._top_k)

    async def answer(self, question: str, contexts: Sequence[str]) -> str:
        if not contexts:
            return ""
        query_tokens = set(_content_tokens(question))
        best_index = 0
        best_overlap = -1
        for index, context in enumerate(contexts):
            overlap = len(query_tokens & set(_content_tokens(context)))
            if overlap > best_overlap:
                best_overlap = overlap
                best_index = index
        sentences = _split_sentences(contexts[best_index])[: self._ANSWER_MAX_SENTENCES]
        return " ".join(sentences)[: self._ANSWER_CHAR_CAP]
