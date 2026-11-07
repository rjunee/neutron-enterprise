# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Scripted test seams for ne-eval (the Judge/EvalTarget seams NE-025 fills live).

These are REAL implementations of the ``Judge`` and ``EvalTarget`` structural
Protocols with canned, deterministic behavior — so suite/metric/calibration
tests exercise real ne-eval code without a live gateway or retrieval stack
(plan §1.4: mock only AT the seam, never past it). Exported and typed for reuse
by NE-025.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from pydantic import BaseModel

from ne.contracts.types import Chunk, Citation, RetrievalResult, ScoredChunk
from ne.eval.errors import JudgeOutputError
from ne.eval.golden import GoldenItem
from ne.eval.rubrics import Rubric

_ScriptEntry = BaseModel | Sequence[BaseModel] | Callable[[Mapping[str, str]], BaseModel]


class ScriptedJudge:
    """A :class:`~ne.eval.judge.Judge` returning canned outputs keyed by rubric id.

    ``outputs`` maps a rubric id to either a single output (reused for every call),
    a sequence consumed FIFO (the last element repeats once exhausted), or a
    callable ``(variables) -> BaseModel``. Rubric ids listed in ``raise_for`` raise
    :class:`JudgeOutputError` (the fail-closed error path). Every call is recorded
    on :attr:`calls`.
    """

    def __init__(
        self,
        outputs: Mapping[str, _ScriptEntry],
        *,
        judge_model_id: str = "scripted-judge",
        judge_family: str = "scripted",
        raise_for: Sequence[str] = (),
    ) -> None:
        self._outputs = dict(outputs)
        self._judge_model_id = judge_model_id
        self._judge_family = judge_family
        self._raise_for = set(raise_for)
        self._index: dict[str, int] = {}
        self.calls: list[tuple[str, dict[str, str]]] = []

    @property
    def judge_model_id(self) -> str:
        return self._judge_model_id

    @property
    def judge_family(self) -> str:
        return self._judge_family

    async def judge(
        self,
        rubric: Rubric,
        variables: Mapping[str, str],
        out_model: type[BaseModel],
    ) -> BaseModel:
        self.calls.append((rubric.id, dict(variables)))
        if rubric.id in self._raise_for:
            raise JudgeOutputError(f"scripted judge configured to fail rubric {rubric.id!r}")
        if rubric.id not in self._outputs:
            raise JudgeOutputError(f"scripted judge has no output for rubric {rubric.id!r}")
        entry = self._outputs[rubric.id]
        if callable(entry) and not isinstance(entry, BaseModel):
            return entry(variables)
        if isinstance(entry, BaseModel):
            return entry
        seq = list(entry)
        idx = self._index.get(rubric.id, 0)
        result = seq[min(idx, len(seq) - 1)]
        self._index[rubric.id] = idx + 1
        return result


class ScriptedTarget:
    """A :class:`~ne.eval.suite.EvalTarget` with canned retrievals and answers."""

    def __init__(
        self,
        retrievals: Mapping[str, RetrievalResult],
        answers: Mapping[str, str] | None = None,
        *,
        default_answer: str = "",
    ) -> None:
        self._retrievals = dict(retrievals)
        self._answers = dict(answers or {})
        self._default_answer = default_answer
        self.retrieve_calls: list[str] = []
        self.answer_calls: list[tuple[str, list[str]]] = []

    async def retrieve(self, question: str) -> RetrievalResult:
        self.retrieve_calls.append(question)
        return self._retrievals[question]

    async def answer(self, question: str, contexts: Sequence[str]) -> str:
        self.answer_calls.append((question, list(contexts)))
        return self._answers.get(question, self._default_answer)


def make_golden_item(
    item_id: str,
    question: str,
    reference_answer: str = "reference",
    *,
    relevant_document_ids: Sequence[str] = (),
    tags: Sequence[str] = (),
) -> GoldenItem:
    """Build a :class:`GoldenItem` for tests."""
    return GoldenItem(
        id=item_id,
        question=question,
        reference_answer=reference_answer,
        relevant_document_ids=tuple(relevant_document_ids),
        tags=tuple(tags),
    )


def make_retrieval_result(
    chunks: Sequence[tuple[str, str, str, float]],
) -> RetrievalResult:
    """Build a :class:`RetrievalResult` from ``(chunk_id, document_id, text, score)`` tuples."""
    scored: list[ScoredChunk] = []
    citations: list[Citation] = []
    for chunk_id, document_id, text, score in chunks:
        scored.append(
            ScoredChunk(
                chunk=Chunk(chunk_id=chunk_id, document_id=document_id, text=text),
                score=score,
            )
        )
        citations.append(
            Citation(
                chunk_id=chunk_id,
                document_id=document_id,
                source_system="test",
                source_ref=f"test://{document_id}/{chunk_id}",
            )
        )
    return RetrievalResult(chunks=tuple(scored), citations=tuple(citations))
