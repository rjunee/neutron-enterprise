# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the deterministic baseline components (ne.eval.baseline)."""

from __future__ import annotations

import asyncio
import json

import pytest

from ne.contracts.types import Chunk, Principal, RetrievalResult, ScoredChunk
from ne.eval.baseline import KnowledgeReadEvalTarget, LexicalJudge, LexicalReranker
from ne.eval.errors import JudgeOutputError
from ne.eval.judge import (
    PrecisionOut,
    RecallClassificationsOut,
    StatementsOut,
    VerdictsOut,
)
from ne.eval.rubrics import get_rubric


def _judge(rubric_id: str, variables: dict[str, str]):
    judge = LexicalJudge()
    return asyncio.run(judge.judge(get_rubric(rubric_id), variables, StatementsOut))


def test_judge_identity() -> None:
    judge = LexicalJudge()
    assert judge.judge_model_id == "ne-lexical-judge-v1"
    assert judge.judge_family == "heuristic-lexical"


def test_statements_splits_sentences_and_empty_answer() -> None:
    out = _judge("faithfulness_statements", {"question": "q", "answer": "Alpha beta. Gamma delta!"})
    assert isinstance(out, StatementsOut)
    assert out.statements == ("Alpha beta.", "Gamma delta!")
    empty = _judge("faithfulness_statements", {"question": "q", "answer": "   "})
    assert isinstance(empty, StatementsOut)
    assert empty.statements == ()


def test_faithfulness_verdicts_support_threshold() -> None:
    contexts = "oxygen is a chemical element\n---\nsteam engines power locomotives"
    statements = json.dumps(["Oxygen is a chemical element."])
    out = _judge("faithfulness_verdicts", {"contexts": contexts, "statements": statements})
    assert isinstance(out, VerdictsOut)
    assert out.verdicts[0].verdict == 1

    unsupported = _judge(
        "faithfulness_verdicts",
        {"contexts": contexts, "statements": json.dumps(["Victoria is an Australian state."])},
    )
    assert isinstance(unsupported, VerdictsOut)
    assert unsupported.verdicts[0].verdict == 0


def test_context_precision_verdict() -> None:
    yes = _judge(
        "context_precision",
        {"question": "q", "reference_answer": "France", "contexts": "a region in France"},
    )
    assert isinstance(yes, PrecisionOut)
    assert yes.verdict == 1
    no = _judge(
        "context_precision",
        {"question": "q", "reference_answer": "Denmark", "contexts": "oxygen is an element"},
    )
    assert isinstance(no, PrecisionOut)
    assert no.verdict == 0


def test_context_recall_classifications() -> None:
    out = _judge(
        "context_recall",
        {
            "contexts": "victoria is a state in southeastern australia",
            "statements": json.dumps(["Victoria is a state.", "Oxygen has atomic number 8."]),
        },
    )
    assert isinstance(out, RecallClassificationsOut)
    assert out.classifications[0].attributed == 1
    assert out.classifications[1].attributed == 0


def test_unknown_rubric_raises() -> None:
    judge = LexicalJudge()
    # A structurally-valid Rubric with an id the judge does not score fails closed.
    rubric = get_rubric("faithfulness_statements").model_copy(update={"id": "totally_unknown"})
    with pytest.raises(JudgeOutputError):
        asyncio.run(judge.judge(rubric, {"answer": "x"}, StatementsOut))


def _chunk(chunk_id: str, text: str) -> Chunk:
    return Chunk(chunk_id=chunk_id, document_id=chunk_id, text=text)


def test_reranker_orders_by_idf_overlap() -> None:
    reranker = LexicalReranker()
    chunks = [
        _chunk("c1", "the weather is mild today"),
        _chunk("c2", "oxygen is a chemical element with atomic number eight"),
        _chunk("c3", "a chemical plant processes materials"),
    ]
    ranked = asyncio.run(reranker.rerank("what is oxygen chemical element", chunks, top_k=3))
    assert [sc.chunk.chunk_id for sc in ranked] == ["c2", "c3", "c1"]
    # rarer query token 'oxygen' (df=1) outweighs the common 'chemical' (df=2).
    assert ranked[0].score > ranked[1].score > ranked[2].score


def test_reranker_tie_break_and_truncation() -> None:
    reranker = LexicalReranker()
    chunks = [_chunk("c2", "alpha"), _chunk("c1", "alpha"), _chunk("c3", "beta")]
    ranked = asyncio.run(reranker.rerank("alpha", chunks, top_k=2))
    assert [sc.chunk.chunk_id for sc in ranked] == ["c1", "c2"]  # tie -> chunk_id asc
    assert len(ranked) == 2  # truncated to top_k


def test_reranker_empty() -> None:
    assert asyncio.run(LexicalReranker().rerank("q", [], top_k=5)) == ()


class _StubReader:
    def __init__(self, result: RetrievalResult) -> None:
        self._result = result
        self.calls: list[tuple[str, int]] = []

    async def search(self, query: str, *, principal: Principal, top_k: int = 10):
        self.calls.append((query, top_k))
        return self._result


def test_target_retrieve_delegates() -> None:
    result = RetrievalResult(
        chunks=(ScoredChunk(chunk=_chunk("c1", "text"), score=1.0),), citations=()
    )
    reader = _StubReader(result)
    target = KnowledgeReadEvalTarget(reader, Principal(id="p"), top_k=7)
    got = asyncio.run(target.retrieve("hello"))
    assert got is result
    assert reader.calls == [("hello", 7)]


def test_target_answer_extractive_selection_and_cap() -> None:
    target = KnowledgeReadEvalTarget(
        _StubReader(RetrievalResult(chunks=(), citations=())), Principal(id="p")
    )
    contexts = [
        "The weather is mild. It might rain later.",
        "Oxygen is a chemical element. It has atomic number 8. Extra sentence ignored.",
    ]
    answer = asyncio.run(target.answer("what is oxygen chemical element", contexts))
    assert answer == "Oxygen is a chemical element. It has atomic number 8."


def test_target_answer_empty_contexts() -> None:
    target = KnowledgeReadEvalTarget(
        _StubReader(RetrievalResult(chunks=(), citations=())), Principal(id="p")
    )
    assert asyncio.run(target.answer("q", [])) == ""
