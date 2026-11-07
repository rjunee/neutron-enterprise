# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""GatewayJudge parsing/retry + cross-family enforcement tests.

The FakeGateway is a stub BELOW the ModelGateway seam (allowed — the judge's only
model seam is the gateway; plan §1.4). Async is exercised via asyncio.run.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence

import pytest

from ne.contracts.models import Completion, CompletionChunk, CompletionRequest, Usage
from ne.eval.errors import CrossFamilyError, EvalConfigError, JudgeOutputError
from ne.eval.judge import GatewayJudge, StatementsOut, enforce_cross_family
from ne.eval.rubrics import get_rubric


class FakeGateway:
    def __init__(self, texts: Sequence[str]) -> None:
        self._texts = list(texts)
        self.requests: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> Completion:
        self.requests.append(request)
        text = self._texts[len(self.requests) - 1]
        return Completion(
            text=text, model_id="fake", usage=Usage(prompt_tokens=1, completion_tokens=1)
        )

    def stream(self, request: CompletionRequest) -> AsyncIterator[CompletionChunk]:
        raise NotImplementedError


def _judge(gateway: FakeGateway) -> GatewayJudge:
    return GatewayJudge(gateway, judge_model_id="qwen3.5-judge", judge_family="qwen")


def test_valid_json_parses() -> None:
    gw = FakeGateway(['{"statements": ["a", "b"]}'])
    judge = _judge(gw)
    out = asyncio.run(
        judge.judge(
            get_rubric("faithfulness_statements"), {"question": "q", "answer": "a"}, StatementsOut
        )
    )
    assert isinstance(out, StatementsOut)
    assert out.statements == ("a", "b")
    assert gw.requests[0].task == "eval-judge"


def test_fenced_output_parses() -> None:
    gw = FakeGateway(['```json\n{"statements": ["x"]}\n```'])
    judge = _judge(gw)
    out = asyncio.run(
        judge.judge(
            get_rubric("faithfulness_statements"), {"question": "q", "answer": "a"}, StatementsOut
        )
    )
    assert isinstance(out, StatementsOut)
    assert out.statements == ("x",)


def test_invalid_then_valid_retry() -> None:
    gw = FakeGateway(["not json", '{"statements": ["ok"]}'])
    judge = _judge(gw)
    out = asyncio.run(
        judge.judge(
            get_rubric("faithfulness_statements"), {"question": "q", "answer": "a"}, StatementsOut
        )
    )
    assert isinstance(out, StatementsOut)
    assert out.statements == ("ok",)
    assert len(gw.requests) == 2
    last_msg = gw.requests[1].messages[-1]
    assert last_msg.role == "user"
    assert "ONLY the JSON object" in last_msg.content


def test_invalid_twice_raises() -> None:
    gw = FakeGateway(["nope", "still nope"])
    judge = _judge(gw)
    with pytest.raises(JudgeOutputError):
        asyncio.run(
            judge.judge(
                get_rubric("faithfulness_statements"),
                {"question": "q", "answer": "a"},
                StatementsOut,
            )
        )
    assert len(gw.requests) == 2


def test_enforce_cross_family_ok() -> None:
    enforce_cross_family("qwen", "claude")


def test_enforce_cross_family_same_raises() -> None:
    with pytest.raises(CrossFamilyError):
        enforce_cross_family("Qwen", "qwen")


def test_enforce_cross_family_empty_raises() -> None:
    with pytest.raises(EvalConfigError):
        enforce_cross_family("", "claude")
    with pytest.raises(EvalConfigError):
        enforce_cross_family("qwen", "")
