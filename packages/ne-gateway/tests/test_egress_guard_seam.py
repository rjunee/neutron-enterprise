# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The NE-012 egress-guard seam: a per-attempt hook, no-op when None."""

from __future__ import annotations

import asyncio

import pytest

from ne.contracts import ChatMessage, CompletionRequest
from ne.gateway import LiteLLMGateway, ModelDef, TaskRoute

from .conftest import PASSTHROUGH_REDACTOR, RecordingCostSink, make_model, make_settings

_MSGS = (ChatMessage(role="user", content="hi"),)


def _request(task: str) -> CompletionRequest:
    return CompletionRequest(messages=_MSGS, task=task)


def test_guard_called_once_per_attempt_with_modeldef(
    sink: RecordingCostSink, frozen_config: str
) -> None:
    settings = make_settings(
        [
            make_model("primary", mock_response=Exception("boom")),
            make_model("backup", mock_response="ok"),
        ],
        {"draft": TaskRoute(model="primary", fallbacks=("backup",))},
        num_retries=0,
    )
    seen: list[ModelDef] = []
    gw = LiteLLMGateway(settings, sink, egress_guard=seen.append, redactor=PASSTHROUGH_REDACTOR)
    asyncio.run(gw.complete(_request("draft")))
    assert [m.name for m in seen] == ["primary", "backup"]


def test_guard_exception_propagates_unwrapped(sink: RecordingCostSink) -> None:
    settings = make_settings(
        [make_model("primary", mock_response="ok")],
        {"draft": TaskRoute(model="primary")},
    )

    def deny(_model: ModelDef) -> None:
        raise RuntimeError("policy denied")

    gw = LiteLLMGateway(settings, sink, egress_guard=deny, redactor=PASSTHROUGH_REDACTOR)
    with pytest.raises(RuntimeError, match="policy denied"):
        asyncio.run(gw.complete(_request("draft")))
    # A policy denial is not a provider failure: no CostEvent emitted.
    assert sink.events == []


def test_none_guard_is_noop(sink: RecordingCostSink, frozen_config: str) -> None:
    settings = make_settings(
        [make_model("primary", mock_response="ok")],
        {"draft": TaskRoute(model="primary")},
    )
    gw = LiteLLMGateway(settings, sink, egress_guard=None, redactor=PASSTHROUGH_REDACTOR)
    result = asyncio.run(gw.complete(_request("draft")))
    assert result.text == "ok"
