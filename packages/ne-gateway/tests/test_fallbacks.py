# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Fallback chains and all-fail behavior — Exception injection via the real
Router's ``mock_response``."""

from __future__ import annotations

import asyncio

import pytest

from ne.contracts import ChatMessage, CompletionRequest
from ne.gateway import LiteLLMGateway, ModelCallFailedError, TaskRoute

from .conftest import PASSTHROUGH_REDACTOR, RecordingCostSink, make_model, make_settings

_MSGS = (ChatMessage(role="user", content="hi"),)


def _request(task: str) -> CompletionRequest:
    return CompletionRequest(messages=_MSGS, task=task)


def test_fallback_chain_on_primary_error(sink: RecordingCostSink, frozen_config: str) -> None:
    settings = make_settings(
        [
            make_model("primary", mock_response=Exception("boom")),
            make_model("backup", mock_response="recovered"),
        ],
        {"draft": TaskRoute(model="primary", fallbacks=("backup",))},
        num_retries=0,
    )
    gw = LiteLLMGateway(settings, sink, redactor=PASSTHROUGH_REDACTOR)
    result = asyncio.run(gw.complete(_request("draft")))
    assert result.model_id == "backup"
    assert result.text == "recovered"


def test_all_candidates_fail_raises_model_call_failed(sink: RecordingCostSink) -> None:
    settings = make_settings(
        [
            make_model("primary", mock_response=Exception("boom-1")),
            make_model("backup", mock_response=Exception("boom-2")),
        ],
        {"draft": TaskRoute(model="primary", fallbacks=("backup",))},
        num_retries=0,
    )
    gw = LiteLLMGateway(settings, sink, redactor=PASSTHROUGH_REDACTOR)
    with pytest.raises(ModelCallFailedError) as excinfo:
        asyncio.run(gw.complete(_request("draft")))
    err = excinfo.value
    assert [name for name, _ in err.attempts] == ["primary", "backup"]
    msg = str(err)
    assert "primary" in msg
    assert "backup" in msg
    # No CostEvent for a fully-failed request.
    assert sink.events == []


def test_router_configured_with_retries_and_timeout(sink: RecordingCostSink) -> None:
    settings = make_settings(
        [make_model("a")],
        {"draft": TaskRoute(model="a")},
        num_retries=4,
        timeout_seconds=33.0,
    )
    gw = LiteLLMGateway(settings, sink, redactor=PASSTHROUGH_REDACTOR)
    assert gw._router.num_retries == 4  # pyright: ignore[reportAttributeAccessIssue]
    assert gw._router.timeout == 33.0  # pyright: ignore[reportAttributeAccessIssue]
